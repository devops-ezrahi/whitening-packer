"""Plain-assert self-checks. Run directly: python test_pack.py"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import dockerimages
import ecosystems
import pack


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _init_repo(tmp_path: Path):
    _git(["init"], tmp_path)
    _git(["config", "user.email", "test@test.com"], tmp_path)
    _git(["config", "user.name", "test"], tmp_path)


def test_extract_base_images():
    dockerfile = """\
FROM python:3.12-slim
FROM node:18 AS builder
FROM builder
ARG BASE_TAG=3.12
FROM python:${BASE_TAG}
FROM nginx:alpine
FROM scratch
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Dockerfile"
        path.write_text(dockerfile)
        images = dockerimages.extract_base_images(path)
        assert images == ["python:3.12-slim", "node:18", "python:3.12", "nginx:alpine"], images


def test_detect_project_name_from_git_remote():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / "local-folder-name"
        tmp_path.mkdir()
        _init_repo(tmp_path)
        _git(["remote", "add", "origin", "git@github.com:someorg/real-repo-name.git"], tmp_path)
        assert pack.detect_project_name(tmp_path) == "real-repo-name"


def test_detect_project_name_falls_back_to_dir_when_no_remote():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / "my-local-dir"
        tmp_path.mkdir()
        _init_repo(tmp_path)
        assert pack.detect_project_name(tmp_path) == "my-local-dir"


def test_team_prefix_in_output_zip():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("hi")
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = subprocess.run(
            [sys.executable, str(Path(pack.__file__).resolve()), str(tmp_path),
             "--no-deps", "--no-images", "--team", "Team One", "-o", str(out_dir)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        produced = list(out_dir.glob("Team-One-*.zip"))
        assert len(produced) == 1, list(out_dir.iterdir())


def test_whitening_json_drives_the_pack():
    """Team, repo and excludes all come from whitening.json — no flags."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({"version": "9.9.9"}))
        (tmp_path / "whitening.json").write_text(json.dumps({
            "team": "dvps", "repo": "inner-widget", "images": False,
            "exclude": [".github/**", "*.md"],
        }))
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        result = subprocess.run(
            [sys.executable, str(Path(pack.__file__).resolve()), str(tmp_path),
             "--no-deps", "-o", str(out_dir)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        produced = next(out_dir.glob("*.zip"))
        assert produced.name.startswith("dvps-"), produced.name
        with zipfile.ZipFile(produced) as zf:
            config = json.loads(zf.read("config.json"))
            # "images": false stood in for --no-images.
            assert not [n for n in zf.namelist() if n.startswith("images/")], zf.namelist()
        assert config == {
            "project": tmp_path.name,
            "version": "9.9.9",
            "team": "dvps",
            "repo": "inner-widget",
            "exclude": [".github/**", "*.md"],
        }, config


def test_pack_config_defaults_and_empty_exclude_dropped():
    # "" as a git pathspec matches everything — it must never reach config.json.
    config = pack.build_pack_config({"exclude": ["docs/**", ""]}, "widget", "1.0.0", "")
    assert config == {
        "project": "widget", "version": "1.0.0", "team": "",
        "repo": "widget", "exclude": ["docs/**"],
    }, config
    assert pack.build_pack_config({}, "widget", "1.0.0", "dvps")["exclude"] == []


def test_load_settings_missing_and_broken():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        assert pack.load_settings(tmp_path) == {}
        (tmp_path / "whitening.json").write_text("{ not json")
        assert pack.load_settings(tmp_path) == {}
        (tmp_path / "whitening.json").write_text(json.dumps({"team": "dvps"}))
        assert pack.load_settings(tmp_path) == {"team": "dvps"}


def test_detect_version_package_json():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "package.json").write_text(json.dumps({"version": "2.3.1"}))
        assert pack.detect_version(tmp_path) == "2.3.1"


def test_detect_version_pom_xml():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "pom.xml").write_text("""\
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <version>1.0.0-SNAPSHOT</version>
  <dependencies>
    <dependency>
      <version>9.9.9</version>
    </dependency>
  </dependencies>
</project>
""")
        assert pack.detect_version(tmp_path) == "1.0.0-SNAPSHOT"


def test_detect_version_git_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("hi")
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)
        version = pack.detect_version(tmp_path)
        assert version and version != "unknown"


def test_collect_source_files():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested.txt").write_text("nested")
        (tmp_path / "tracked.txt").write_text("tracked")
        (tmp_path / ".gitignore").write_text("ignored.txt\n")
        (tmp_path / "ignored.txt").write_text("ignored")
        _git(["add", "tracked.txt", "sub/nested.txt", ".gitignore"], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)

        dest = tmp_path / "dest"
        dest.mkdir()
        pack.collect_source_files(tmp_path, dest)

        assert (dest / "tracked.txt").exists()
        assert (dest / "sub" / "nested.txt").exists()
        assert not (dest / "ignored.txt").exists()


def test_build_zip():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        (staging / "source").mkdir(parents=True)
        (staging / "source" / "a.txt").write_text("a")
        (staging / "dependencies").mkdir(parents=True)
        (staging / "dependencies" / "b.txt").write_text("b")
        (staging / "images").mkdir(parents=True)
        (staging / "images" / "c.tar").write_text("c")

        output = Path(tmp) / "out.zip"
        pack.build_zip(staging, output)

        names = zipfile.ZipFile(output).namelist()
        assert any(n.startswith("source") for n in names)
        assert any(n.startswith("dependencies") for n in names)
        assert any(n.startswith("images") for n in names)


def test_dependency_ecosystems_table():
    names = {eco["detect"] for eco in ecosystems.DEPENDENCY_ECOSYSTEMS}
    assert "package.json" in names
    assert "pom.xml" in names


def test_changed_npm_packages():
    old = json.dumps({"packages": {
        "": {"name": "root"},
        "node_modules/keep": {"version": "1.0.0", "integrity": "sha-a"},
        "node_modules/bump": {"version": "1.0.0", "integrity": "sha-b"},
    }})
    new = json.dumps({"packages": {
        "": {"name": "root"},
        "node_modules/keep": {"version": "1.0.0", "integrity": "sha-a"},   # unchanged
        "node_modules/bump": {"version": "2.0.0", "integrity": "sha-c"},   # version bump
        "node_modules/added": {"version": "0.1.0", "integrity": "sha-d"},  # new
    }})
    assert ecosystems.changed_npm_packages(old, new) == {"node_modules/bump", "node_modules/added"}
    # No baseline text -> everything in new is "changed" (root excluded).
    assert ecosystems.changed_npm_packages("", new) == {
        "node_modules/keep", "node_modules/bump", "node_modules/added"}


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
