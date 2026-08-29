#!/usr/bin/env python3
"""Pack a git project's source, dependencies, and Docker base images into one tar."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
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


def last_release_tag(project_path: Path, version: str) -> str:
    """Last release tag reachable from HEAD, excluding the one being packed.

    The version excludes are load-bearing: CI tags HEAD (semantic-release) before
    running the packer, so without them the delta baseline would be the release
    being packed and every delta bundle would come out empty.
    """
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--exclude", "pack/*",
         "--exclude", f"v{version}", "--exclude", version],
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


def load_settings(project_path: Path) -> dict:
    """Optional `whitening.json` at the project root — the project's pack settings.

    Everything a pack needs that can't be detected (team, closed-network repo
    name, PR exclude globs, whether to pack base images) lives here, so CI and
    local packs produce identical zips with nobody passing flags.
    """
    settings = project_path / "whitening.json"
    if not settings.exists():
        return {}
    try:
        return json.loads(settings.read_text())
    except (json.JSONDecodeError, OSError) as err:
        print(f"warning: ignoring unreadable whitening.json: {err}", file=sys.stderr)
        return {}


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


def deleted_paths(project_path: Path, base_tag: str) -> list[str]:
    """Source files deleted since base_tag — repo-relative, as they sit under repository/.

    A pack is extracted on top of the previous one, so a file dropped from the
    repo would otherwise live forever in the unpacked tree; this is the list the
    consumer deletes.
    """
    result = subprocess.run(
        ["git", "-C", str(project_path), "diff", "--name-only", "--diff-filter=D", base_tag],
        capture_output=True, text=True,
    )
    return sorted(p for p in result.stdout.splitlines() if p)


def build_pack_config(project: str, version: str,
                      department: str, team: str, repository: str) -> dict:
    """repository/config.json — what the unpacker reads."""
    return {
        "version": version,
        "repos": {
            project: {
                "department": department,
                "team": team,
                "repository": repository,
            }
        },
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


def build_tgz(staging_dir: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, "w:gz") as tf:
        # tf.add recurses, so empty dirs (images/, node_modules/) survive too.
        for entry in sorted(staging_dir.iterdir()):
            tf.add(entry, arcname=entry.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_path", help="path to the git project to pack")
    parser.add_argument("--no-deps", action="store_true", help="skip the dependencies folder")
    parser.add_argument("--all-deps", action="store_true",
                        help="pack all dependencies (skip the delta-since-last-pack-tag optimization)")
    parser.add_argument("--no-images", action="store_true", help="skip the Docker base images folder")
    parser.add_argument("-o", "--output", help="output tgz path or directory (default: current directory)")
    # The CI running the pack owns these three — flag, else env var.
    for name in ("department", "team", "repository"):
        parser.add_argument(f"--{name}", default=os.environ.get(f"WHITENING_{name.upper()}", ""),
                            help=f"{name} (default: $WHITENING_{name.upper()})")
    args = parser.parse_args()

    project_path = Path(args.project_path).resolve()
    if not project_path.is_dir():
        print(f"error: {project_path} is not a directory", file=sys.stderr)
        sys.exit(1)
    if not (project_path / ".git").exists():
        print(f"error: {project_path} is not a git project (.git not found)", file=sys.stderr)
        sys.exit(1)

    department, team, repository = (re.sub(r"\s+", "-", v.strip()) for v in
                                    (args.department, args.team, args.repository))
    missing = [n for n, v in (("department", department), ("team", team),
                              ("repository", repository)) if not v]
    if missing:
        print(f"error: missing {', '.join(missing)} — pass --<name> or set "
              f"WHITENING_<NAME> in the CI job", file=sys.stderr)
        sys.exit(1)

    settings = load_settings(project_path)
    project_name = detect_project_name(project_path)
    version = detect_version(project_path)
    # "images": false in whitening.json is the project saying "never pack base
    # images"; --no-images still wins when it's absent or true.
    no_images = args.no_images or settings.get("images") is False
    tgz_name = f"{team}-{repository}-{version}.tgz"

    if args.output:
        output_path = Path(args.output).resolve()
        if output_path.is_dir():
            output_path = output_path / tgz_name
    else:
        output_path = Path.cwd() / tgz_name

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        repo_dir = staging / "repository"
        (staging / "images").mkdir()
        (staging / "node_modules").mkdir()
        (repo_dir / repository).mkdir(parents=True)

        collect_source_files(project_path, repo_dir / repository)

        config = build_pack_config(project_name, version, department, team, repository)
        # newline="\n": these are read on Linux, not on whatever packed them.
        (repo_dir / "config.json").write_text(json.dumps(config, indent=2), newline="\n")

        base_tag = last_release_tag(project_path, version)

        # One file per pack, named for the version: extracting packs in order
        # accumulates the folder instead of overwriting a single list.
        (staging / "to_delete").mkdir()
        deleted = deleted_paths(project_path, base_tag) if base_tag else []
        if deleted:
            (staging / "to_delete" / version).write_text(
                "".join(f"{p}\n" for p in deleted), newline="\n")

        if not args.no_deps:
            # Dest is the tar root: npm lands in node_modules/ as the layout wants.
            # ponytail: maven lands in dependency/ — no slot for it in this layout yet.
            deps_dir = staging

            include_paths = None
            if not args.all_deps and base_tag:
                include_paths = changed_dep_paths(project_path, base_tag)
                if include_paths is not None:
                    print(f"delta: {len(include_paths)} changed dep(s) since {base_tag}",
                          file=sys.stderr)
            ecosystems.copy_dependencies(project_path, deps_dir, include_paths)

        if not no_images:
            dockerimages.package_images(project_path, staging / "images")

        build_tgz(staging, output_path)

    print(output_path)


if __name__ == "__main__":
    main()
