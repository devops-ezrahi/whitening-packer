"""Plain-assert self-checks. Run directly: python test_pack.py"""

import json
import os
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


def test_pack_layout_and_config_from_ci():
    """department/team/repository come from the CI (env vars here)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({"version": "1.0.1"}))
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)
        _git(["tag", "1.0.0"], tmp_path)
        _git(["tag", "1.0.1"], tmp_path)
        _git(["tag", "pack/1.0.0-20260101T000000Z"], tmp_path)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        env = {**os.environ, "WHITENING_DEPARTMENT": "ultra", "WHITENING_TEAM": "optimus",
               "WHITENING_REPOSITORY": "ultra-supporting-services"}
        result = subprocess.run(
            [sys.executable, str(Path(pack.__file__).resolve()), str(tmp_path),
             "--no-deps", "--no-images", "-o", str(out_dir)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, result.stderr
        produced = next(out_dir.glob("*.zip"))
        assert produced.name == "ultra-supporting-services-1.0.1.zip", produced.name

        with zipfile.ZipFile(produced) as zf:
            names = [n.rstrip("/") for n in zf.namelist()]
            config = json.loads(zf.read("repository/config.json"))
        assert "images" in names and "node_modules" in names and "to_delete" in names, names
        assert "repository/tags" not in names, names
        assert "repository/ultra-supporting-services/package.json" in names, names
        assert config == {
            "version": "1.0.1",
            "repos": {tmp_path.name: {"department": "ultra", "team": "optimus",
                                      "repository": "ultra-supporting-services"}},
        }, config


def test_missing_ci_values_fail():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)
        env = {k: v for k, v in os.environ.items() if not k.startswith("WHITENING_")}
        result = subprocess.run(
            [sys.executable, str(Path(pack.__file__).resolve()), str(tmp_path),
             "--department", "ultra"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 1
        assert "missing team, repository" in result.stderr, result.stderr


def test_build_pack_config():
    assert pack.build_pack_config("widget", "1.0.0", "ultra", "optimus", "inner-widget") == {
        "version": "1.0.0",
        "repos": {"widget": {"department": "ultra", "team": "optimus",
                             "repository": "inner-widget"}},
    }


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


def test_detect_version_chart_yaml():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "Chart.yaml").write_text(
            'apiVersion: v2\nname: c\nversion: "0.2.0-dev.1"\nappVersion: "9.9.9"\n')
        assert pack.detect_version(tmp_path) == "0.2.0-dev.1"


def test_detect_version_git_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("hi")
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)
        version = pack.detect_version(tmp_path)
        assert version and version != "unknown"


def test_last_release_tag_skips_the_version_being_packed():
    """CI tags HEAD before packing — that tag must not become its own baseline."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)
        (tmp_path / "file.txt").write_text("hi")
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)
        _git(["tag", "v1.0.0"], tmp_path)
        (tmp_path / "file.txt").write_text("bye")
        _git(["commit", "-am", "next"], tmp_path)
        _git(["tag", "v1.0.1"], tmp_path)
        _git(["tag", "pack/1.0.1-20260101T000000Z"], tmp_path)

        assert pack.last_release_tag(tmp_path, "1.0.1") == "v1.0.0"
        assert pack.last_release_tag(tmp_path, "1.0.2") == "v1.0.1"


def test_delete_lists_cover_every_release():
    """One file per release, all of history — not just this pack's deletions."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _init_repo(tmp_path)
        for name in ("keep.txt", "gone-in-2.txt", "sub/gone-in-3.txt"):
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "init"], tmp_path)
        _git(["tag", "v1.0.0"], tmp_path)

        _git(["rm", "-q", "gone-in-2.txt"], tmp_path)
        _git(["commit", "-m", "drop one"], tmp_path)
        _git(["tag", "v1.0.1"], tmp_path)

        # Added and deleted inside one interval: never in either snapshot, so it
        # is fine for it to be missing from the lists.
        (tmp_path / "transient.txt").write_text("x")
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", "add transient"], tmp_path)
        _git(["rm", "-q", "transient.txt", "sub/gone-in-3.txt"], tmp_path)
        _git(["commit", "-m", "drop more"], tmp_path)
        _git(["tag", "v1.0.2"], tmp_path)   # the version being packed

        dest = tmp_path / "to_delete"
        pack.write_delete_lists(tmp_path, dest, "1.0.2")

        assert sorted(f.name for f in dest.iterdir()) == ["1.0.2", "v1.0.1"]
        assert (dest / "v1.0.1").read_text() == "gone-in-2.txt\n"
        # v1.0.2 is this pack: its list is named for the version, not the tag.
        assert (dest / "1.0.2").read_text() == "sub/gone-in-3.txt\n"
        assert not (dest / "v1.0.0").exists()   # nothing was deleted by the first release


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


def test_build_zip_keeps_empty_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "staging"
        (staging / "repository" / "widget").mkdir(parents=True)
        (staging / "repository" / "widget" / "a.txt").write_text("a")
        (staging / "node_modules").mkdir(parents=True)
        (staging / "images").mkdir(parents=True)

        output = Path(tmp) / "out.zip"
        pack.build_zip(staging, output)

        with zipfile.ZipFile(output) as zf:
            names = [n.rstrip("/") for n in zf.namelist()]
        assert "repository/widget/a.txt" in names, names
        assert "images" in names and "node_modules" in names, names


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
