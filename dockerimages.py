"""Dockerfile discovery, FROM-line parsing, and image pull/save."""

import re
import subprocess
import sys
from pathlib import Path

ARG_RE = re.compile(r"^\s*ARG\s+(\w+)(?:=(\S+))?", re.MULTILINE)
FROM_RE = re.compile(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.MULTILINE | re.IGNORECASE)
SKIP_DIRS = {"node_modules", ".git"}


def find_dockerfiles(project_path: Path) -> list[Path]:
    found = []
    for p in project_path.rglob("Dockerfile*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(project_path).parts):
            continue
        if p.name == "Dockerfile" or p.name.startswith("Dockerfile.") or p.name.endswith(".Dockerfile"):
            found.append(p)
    return found


def _substitute_args(image_ref: str, args: dict) -> str:
    def repl(m):
        name = m.group(1) or m.group(2)
        return args.get(name, m.group(0))

    return re.sub(r"\$\{(\w+)\}|\$(\w+)", repl, image_ref)


def extract_base_images(dockerfile_path: Path) -> list[str]:
    text = dockerfile_path.read_text(errors="ignore")

    args = {}
    for m in ARG_RE.finditer(text):
        args[m.group(1)] = m.group(2) or ""

    stage_names = set()
    images = []
    for m in FROM_RE.finditer(text):
        image_ref, stage_name = m.group(1), m.group(2)
        resolved = _substitute_args(image_ref, args)

        if resolved in stage_names or resolved.lower() == "scratch":
            if stage_name:
                stage_names.add(stage_name)
            continue

        images.append(resolved)
        if stage_name:
            stage_names.add(stage_name)

    return list(dict.fromkeys(images))


def _docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def package_images(project_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)

    dockerfiles = find_dockerfiles(project_path)
    if not dockerfiles:
        return

    images = []
    for df in dockerfiles:
        for img in extract_base_images(df):
            if img not in images:
                images.append(img)

    if not images:
        return

    if not _docker_available():
        print("docker not available (not installed or daemon not running), skipping images", file=sys.stderr)
        return

    for image in images:
        inspect = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
        if inspect.returncode != 0:
            pull = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
            if pull.returncode != 0:
                print(f"failed to pull {image}, skipping: {pull.stderr.strip()}", file=sys.stderr)
                continue

        sanitized = image.replace("/", "_").replace(":", "_")
        out_path = dest / f"{sanitized}.tar"
        save = subprocess.run(["docker", "save", image, "-o", str(out_path)], capture_output=True, text=True)
        if save.returncode != 0:
            print(f"failed to save {image}, skipping: {save.stderr.strip()}", file=sys.stderr)
