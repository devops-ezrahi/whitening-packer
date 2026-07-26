#!/usr/bin/env python3
"""Pack a git project's source, dependencies, and Docker base images into one zip."""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import dockerimages
import ecosystems


def detect_version(project_path: Path) -> str:
    package_json = project_path / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text())
            version = data.get("version", "").strip()
            if version:
                return version
        except (json.JSONDecodeError, OSError):
            pass

    pom_xml = project_path / "pom.xml"
    if pom_xml.exists():
        try:
            root = ET.parse(pom_xml).getroot()
            ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
            version_el = root.find(f"{ns}version")
            if version_el is None:
                parent_el = root.find(f"{ns}parent")
                if parent_el is not None:
                    version_el = parent_el.find(f"{ns}version")
            if version_el is not None and version_el.text and version_el.text.strip():
                return version_el.text.strip()
        except (ET.ParseError, OSError):
            pass

    # --exclude pack/*: our own pack tags must not become the detected version.
    for cmd in (
        ["git", "describe", "--tags", "--always", "--exclude", "pack/*"],
        ["git", "rev-parse", "--short", "HEAD"],
    ):
        result = subprocess.run(cmd, cwd=project_path, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

    return "unknown"


def last_pack_tag(project_path: Path) -> str:
    """Most recent pack/* tag reachable from HEAD, or '' if none."""
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "pack/*"],
        cwd=project_path, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def changed_dep_paths(project_path: Path, base_tag: str):
    """node_modules paths changed since base_tag, or None to signal 'pack all'.

    None when there's no baseline lockfile to diff against (first pack, or the
    lockfile wasn't tracked at that tag) — callers fall back to a full copy.
    """
    show = subprocess.run(
        ["git", "show", f"{base_tag}:package-lock.json"],
        cwd=project_path, capture_output=True, text=True,
    )
    if show.returncode != 0:
        return None
    new_lock = project_path / "package-lock.json"
    if not new_lock.exists():
        return None
    return ecosystems.changed_npm_packages(show.stdout, new_lock.read_text())


def create_pack_tag(project_path: Path, version: str) -> str:
    """Lightweight tag on HEAD marking this pack; returns the tag name or ''."""
    safe = re.sub(r"[\s/]+", "-", version.strip())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"pack/{safe}-{stamp}"
    result = subprocess.run(
        ["git", "-C", str(project_path), "tag", tag], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"warning: could not create tag {tag}: {result.stderr.strip()}", file=sys.stderr)
        return ""
    return tag


TEAM_RE = re.compile(r"^\s*TEAM:\s*['\"]?([A-Za-z0-9._-]+)", re.MULTILINE)


def detect_team(project_path: Path) -> str:
    """Team name declared in a CI config, or '' if none.

    Lets CI and local packs produce the same filename without anyone passing
    --team. ponytail: a regex over the workflow files, not a YAML parse — no
    new dependency for reading one scalar.
    """
    for workflows in (".github/workflows", ".gitea/workflows"):
        for cfg in sorted((project_path / workflows).glob("*.y*ml")):
            match = TEAM_RE.search(cfg.read_text(errors="ignore"))
            if match:
                return match.group(1)
    return ""


def detect_project_name(project_path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(project_path), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        name = result.stdout.strip().rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name

    # ponytail: no origin remote (local-only repo) — fall back to the directory name.
    return project_path.name


def load_pack_config(project_path: Path, project: str, version: str, team: str) -> dict:
    """The config.json written at the zip root — what the unpacker reads.

    Project name/version/team are detected; the closed-network repo name and
    the PR exclude globs can only come from the project, so an optional
    `whitening.json` at its root supplies them.
    """
    overrides = {}
    settings = project_path / "whitening.json"
    if settings.exists():
        try:
            overrides = json.loads(settings.read_text())
        except (json.JSONDecodeError, OSError) as err:
            print(f"warning: ignoring unreadable whitening.json: {err}", file=sys.stderr)

    return {
        "project": project,
        "version": version,
        "team": team,
        "repo": overrides.get("repo") or project,
        "exclude": overrides.get("exclude") or [],
    }


def collect_source_files(project_path: Path, dest: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(project_path), "ls-files", "-z"],
        capture_output=True,
    )
    for rel in result.stdout.decode().split("\0"):
        if not rel:
            continue
        src = project_path / rel
        # ponytail: submodule gitlinks aren't regular files; nested checkouts out of scope for v1.
        if not src.is_file():
            continue
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def build_zip(staging_dir: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in staging_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(staging_dir))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_path", help="path to the git project to pack")
    parser.add_argument("--no-deps", action="store_true", help="skip the dependencies folder")
    parser.add_argument("--all-deps", action="store_true",
                        help="pack all dependencies (skip the delta-since-last-pack-tag optimization)")
    parser.add_argument("--no-images", action="store_true", help="skip the Docker base images folder")
    parser.add_argument("-o", "--output", help="output zip path or directory (default: current directory)")
    parser.add_argument("--team",
                        help="team name to prefix the output zip filename with "
                             "(default: TEAM from the project's CI config, if any)")
    args = parser.parse_args()

    project_path = Path(args.project_path).resolve()
    if not project_path.is_dir():
        print(f"error: {project_path} is not a directory", file=sys.stderr)
        sys.exit(1)
    if not (project_path / ".git").exists():
        print(f"error: {project_path} is not a git project (.git not found)", file=sys.stderr)
        sys.exit(1)

    project_name = detect_project_name(project_path)
    version = detect_version(project_path)
    team = re.sub(r"\s+", "-", (args.team or detect_team(project_path)).strip())
    zip_name = f"{team}-{project_name}-{version}.zip" if team else f"{project_name}-{version}.zip"

    if args.output:
        output_path = Path(args.output).resolve()
        if output_path.is_dir():
            output_path = output_path / zip_name
    else:
        output_path = Path.cwd() / zip_name

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)

        collect_source_files(project_path, staging / "source")

        config = load_pack_config(project_path, project_name, version, team)
        (staging / "config.json").write_text(json.dumps(config, indent=2))

        if not args.no_deps:
            deps_dir = staging / "dependencies"
            deps_dir.mkdir(parents=True, exist_ok=True)

            include_paths = None
            if not args.all_deps:
                base_tag = last_pack_tag(project_path)
                if base_tag:
                    include_paths = changed_dep_paths(project_path, base_tag)
                    if include_paths is not None:
                        print(f"delta: {len(include_paths)} changed dep(s) since {base_tag}",
                              file=sys.stderr)
            ecosystems.copy_dependencies(project_path, deps_dir, include_paths)

        if not args.no_images:
            dockerimages.package_images(project_path, staging / "images")

        build_zip(staging, output_path)

    tag = create_pack_tag(project_path, version)
    print(output_path)
    if tag:
        print(f"tagged {tag}", file=sys.stderr)


if __name__ == "__main__":
    main()
