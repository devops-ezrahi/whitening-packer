"""Dependency-ecosystem table. Add a new ecosystem by appending one entry."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEPENDENCY_ECOSYSTEMS = [
    {
        "name": "npm/yarn/pnpm",
        "detect": "package.json",
        "commands": [["npm", "install"]],
        "copy_folder": "node_modules",
        # ponytail: packed projects typically deploy to Linux even when developed on
        # Windows/Mac, so also pull in linux-x64 native optional deps (rollup/esbuild/etc.)
        # for the packed copy. Done via an isolated temp install (not --os/--cpu in-place,
        # which *replaces* the current platform's binaries instead of adding to them) so the
        # real project node_modules is never touched. Add more entries for other targets.
        "extra_platforms": [{"os": "linux", "cpu": "x64"}],
        "manifest_files": ["package.json", "package-lock.json", ".npmrc"],
    },
    {
        "name": "maven",
        "detect": "pom.xml",
        "commands": [["mvn", "-q", "dependency:copy-dependencies"]],
        "copy_folder": "target/dependency",
    },
]


def changed_npm_packages(old_lock_text: str, new_lock_text: str) -> set:
    """node_modules-relative paths whose lockfile entry is new or changed.

    Compares the `packages` map of two npm lockfiles (v2/v3, keys like
    `node_modules/foo`). A package counts as changed if it's new or its
    version/resolved/integrity differs. The root package (key "") is ignored.
    """
    def packages(text):
        try:
            return json.loads(text).get("packages", {})
        except (json.JSONDecodeError, TypeError):
            return {}

    old, new = packages(old_lock_text), packages(new_lock_text)
    fields = ("version", "resolved", "integrity")
    changed = set()
    for path, meta in new.items():
        if not path:  # root package, not a node_modules dir
            continue
        prev = old.get(path)
        if prev is None or any(meta.get(f) != prev.get(f) for f in fields):
            changed.add(path)
    return changed


def _copy_selected(src_root: Path, dst_root: Path, rel_paths) -> None:
    for rel in rel_paths:
        src = src_root / rel
        if not src.exists():
            continue
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def _run(command, cwd, label):
    # ponytail: shutil.which resolves shims like npm.cmd on Windows, which subprocess
    # otherwise fails to find without shell=True.
    resolved = shutil.which(command[0])
    command = [resolved, *command[1:]] if resolved else command
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"[{label}] command not found, skipping", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"[{label}] command failed, skipping: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _copy_extra_platform(project_path: Path, eco: dict, platform: dict, dest: Path,
                         include_paths=None) -> None:
    label = f"{eco['name']} ({platform['os']}/{platform['cpu']})"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for name in eco.get("manifest_files", []):
            src_file = project_path / name
            if src_file.exists():
                shutil.copy2(src_file, tmp_path / name)

        command = ["npm", "install", f"--os={platform['os']}", f"--cpu={platform['cpu']}"]
        if not _run(command, tmp_path, label):
            return

        if not (tmp_path / eco["copy_folder"]).exists():
            return
        if include_paths is not None:
            _copy_selected(tmp_path, dest, include_paths)
        else:
            shutil.copytree(tmp_path / eco["copy_folder"],
                            dest / Path(eco["copy_folder"]).name, dirs_exist_ok=True)


def copy_dependencies(project_path: Path, dest: Path, include_paths=None) -> None:
    """Copy each detected ecosystem's deps into `dest`.

    `include_paths` (a set of node_modules-relative paths) restricts the npm
    copy to just those packages — the delta path. Non-node_modules ecosystems
    (maven) ignore it and full-copy. `# ponytail:` delta is lockfile-based, npm-only.
    """
    for eco in DEPENDENCY_ECOSYSTEMS:
        if not (project_path / eco["detect"]).exists():
            continue

        if not all(_run(cmd, project_path, eco["name"]) for cmd in eco.get("commands", [])):
            continue

        src = project_path / eco["copy_folder"]
        if not src.exists():
            print(f"[{eco['name']}] {eco['copy_folder']} not found, skipping", file=sys.stderr)
            continue

        selective = include_paths is not None and eco["copy_folder"] == "node_modules"
        if selective:
            _copy_selected(project_path, dest, include_paths)
        else:
            shutil.copytree(src, dest / Path(eco["copy_folder"]).name, dirs_exist_ok=True)

        for platform in eco.get("extra_platforms", []):
            _copy_extra_platform(project_path, eco, platform, dest,
                                 include_paths if selective else None)
