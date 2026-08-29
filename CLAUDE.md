# whitening packer

Python CLI that packs a git project into one gzipped tar,
`<team>-<repository>-<version>.tgz`, with this exact layout (see `tar/` for the
reference tree):

```
images/                        Docker base images referenced by any Dockerfile
node_modules/                  installed deps
to_delete/
  <release>                    paths that release deleted, one per line (all releases)
repository/
  config.json
  <repository>/                git-tracked source
```

Built for handing off/archiving a reproducible snapshot of a project.

## Usage

```
python pack.py <project-path> --department <d> --team <t> --repository <r> \
               [--no-deps] [--all-deps] [--no-images] [-o <output>]
```

**department / team / repository come from the CI job that runs the script** — each flag
falls back to `$WHITENING_DEPARTMENT` / `$WHITENING_TEAM` / `$WHITENING_REPOSITORY`, and
all three are required (whitespace in a value becomes hyphens). `<repository>` is the repo
name on the closed-network git; it names the source folder inside `repository/`.

Requires the target path to contain `.git` (hard requirement, not optional).

## config.json

`repository/config.json` is what the consumer reads:

```json
{
  "version": "1.0.1",
  "repos": {
    "devops-portal": {
      "department": "ultra",
      "team": "optimus",
      "repository": "ultra-supporting-services"
    }
  }
}
```

The `repos` key is the **project name** from the git remote (`git remote get-url origin`,
basename minus `.git`), not the local directory name — so a folder renamed/cloned under a
different name still keys on the actual repo. Falls back to the directory name if there's
no `origin` remote (local-only repos).

## to_delete

A pack is extracted **on top of** the previous one, so a file dropped from the repo would
otherwise live on forever in the unpacked tree. `to_delete/` is the delete list: paths
repo-relative, exactly as they sit under `repository/<repository>/`.

**Every pack carries the whole history, not just its own deletions** — one file per release
tag reachable from HEAD (`git diff --name-only --diff-filter=D <prev tag> <tag>`, oldest
first), plus one named for the version being packed covering the last tag → working tree.
So a consumer extracting onto a tree several releases old still learns about every path
that has gone since, and re-extracting an old pack can't resurrect one.

Releases that deleted nothing get no file; no tags at all → the folder ships empty. A file
added *and* deleted between two releases never appears — neither end of that diff has it,
which is what we want, not a gap. Tag-per-file, snapshot diffs: not a walk of every commit.

## whitening.json

Optional, at the **project** root (`load_settings`). Only `images: false` is read from it
now — "never pack base images", same as `--no-images`:

```json
{ "images": false }
```

## Delta dependencies

The packer creates no tags of its own. It used to tag every pack
`pack/<version>-<UTC-timestamp>`; that stamp only existed so two packs of the same
version wouldn't collide, and it made for ugly tags and duplicate releases. The
release tag is now the baseline — one tag, one release, one pack per version.

If **not** `--all-deps`, `last_release_tag` finds the last tag reachable from HEAD
(`git describe --tags --abbrev=0`, excluding `pack/*` and the version being packed)
and packs **only the dependencies whose lockfile entry changed since then** — a
*delta* bundle. Delta tarballs are meant to be extracted **on top of** the previous
bundle.

The version excludes matter: CI runs semantic-release first, so `v<version>` is
already on HEAD when the packer runs, and without them the baseline would be the
release being packed — every delta would come out empty.

- `--all-deps` — force a full dependency copy (no delta).
- No prior tag, or `package-lock.json` not tracked at that tag → full copy.
- Old `pack/*` tags still exist in repos packed before this change; they're excluded
  everywhere they'd otherwise be mistaken for a release tag.
- **npm-only.** The signal is `package-lock.json` (node_modules is gitignored, so
  `git diff` can't see dep changes). The linux-x64 extra-platform merge is filtered
  to the same changed set, so changed linux-native binaries come along. Maven has no
  resolved lockfile here and always full-copies — `# ponytail:` scope limit.

## Files

- `pack.py` — CLI entry point, orchestration, version detection, tgz assembly.
- `tar/` — reference tree for the output layout. Match it, don't re-derive it.
- `ecosystems.py` — dependency-ecosystem table (npm, maven) + copy logic. Add a new
  ecosystem by appending one entry to `DEPENDENCY_ECOSYSTEMS`; no plugin system, it's a
  flat list on purpose.
- `dockerimages.py` — Dockerfile discovery, `FROM`-line parsing (multi-stage, ARG
  substitution, `scratch` filtering), `docker pull`/`docker save`.
- `test_pack.py` — plain-assert self-checks, `python test_pack.py`.

## Version detection order

`package.json` version → `pom.xml` top-level `<version>` →
`git describe --tags --always --exclude 'pack/*'` → `git rev-parse --short HEAD` →
`"unknown"`. (`pack/*` is excluded so the packer's own tags don't become the version.)

## Non-obvious gotchas (hit these already, don't re-derive)

- **npm/mvn subprocess calls need `shutil.which()` first.** On Windows, `npm` is a
  `.cmd` shim, not a `.exe`. `subprocess.run(["npm", ...])` raises `FileNotFoundError`
  without `shell=True` unless you resolve the real path via `shutil.which(command[0])`
  first. `ecosystems.py`'s `_run()` does this — don't remove it or Windows breaks silently.

- **`npm install --os=X --cpu=Y` replaces, not adds.** It does not layer a second
  platform's optional native binaries (rollup/esbuild/sharp/etc.) onto an existing
  `node_modules` — npm reconciles the tree to match the *last* `--os`/`--cpu` given,
  deleting the previous platform's binaries. Running it in-place on a dev machine breaks
  local dev (confirmed: a Windows `node_modules` lost its `@rollup/rollup-win32-*`
  packages after an in-place `--os=linux --cpu=x64` pass, restored only by a plain
  `npm install`).

  Fix used in `ecosystems.py`: the extra-platform install (`extra_platforms` entry, e.g.
  linux-x64 for projects normally developed on Windows/Mac but deployed to Linux) runs in
  an **isolated temp copy** (just `package.json`/lockfile copied over, fresh
  `npm install --os=... --cpu=...` there), then only that temp `node_modules` is merged
  (`dirs_exist_ok=True`) into the *packed output*. The real project directory's
  `node_modules` is never touched by the extra-platform step — only by the plain
  `npm install` that runs first, in place, as normal.

  If you ever add another `extra_platforms` target, keep it going through
  `_copy_extra_platform` (isolated temp dir), never as an in-place command on
  `project_path` itself.

## Deliberate scope limits (marked `# ponytail:` in code)

- Git submodules aren't recursively packed.
- Multi-module Maven (parent pom aggregating child poms) isn't handled, single module only.
- Dockerfile ARG resolution only covers top-of-file default values, not `--build-arg`
  overrides or per-stage redeclaration.
- No filename sanitization on weird `git describe` output.
- No new pip dependencies — stdlib only (`tarfile`, `shutil`, `subprocess`, `pathlib`,
  `json`, `re`, `tempfile`, `xml.etree.ElementTree`).
