# whitening packer

Python CLI that packs a git project into one zip: `[<team>-]<name>-<version>.zip`
containing `source/` (git-tracked files), `dependencies/` (installed deps), `images/`
(Docker base images referenced by any Dockerfile). Built for handing off/archiving a
reproducible snapshot of a project.

`<name>` comes from the git remote (`git remote get-url origin`, basename minus `.git`),
not the local directory name — so a folder renamed/cloned under a different name still
produces a zip named after the actual repo. Falls back to the directory name if there's
no `origin` remote (local-only repos).

## Usage

```
python pack.py <project-path> [--team <name>] [--no-deps] [--all-deps] [--no-images] [-o <output>]
```

`--team` prefixes the output filename (whitespace in the name becomes hyphens). The
`whitening-packer` skill asks the user for this interactively before running the script —
see `~/.claude/skills/whitening-packer/SKILL.md`.

Requires the target path to contain `.git` (hard requirement, not optional).

## Pack tags + delta dependencies

Every successful pack creates a lightweight git tag on HEAD:
`pack/<version>-<UTC-timestamp>` (e.g. `pack/1.0.2-20260724T161500Z`). This marks
"what was last shipped". The devops-portal Stop hook pushes `pack/*` tags to both
remotes; elsewhere push them yourself if you want them shared.

On the next pack, if **not** `--all-deps`, the packer finds the last `pack/*` tag
reachable from HEAD (`git describe --tags --abbrev=0 --match 'pack/*'`) and packs
**only the dependencies whose lockfile entry changed since then** — a *delta*
bundle. Delta zips are meant to be unzipped **on top of** the previous bundle.

- `--all-deps` — force a full dependency copy (no delta).
- No prior `pack/*` tag, or `package-lock.json` not tracked at that tag → full copy.
- **npm-only.** The signal is `package-lock.json` (node_modules is gitignored, so
  `git diff` can't see dep changes). The linux-x64 extra-platform merge is filtered
  to the same changed set, so changed linux-native binaries come along. Maven has no
  resolved lockfile here and always full-copies — `# ponytail:` scope limit.

## Files

- `pack.py` — CLI entry point, orchestration, version detection, zip assembly.
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
- No new pip dependencies — stdlib only (`zipfile`, `shutil`, `subprocess`, `pathlib`,
  `json`, `re`, `tempfile`, `xml.etree.ElementTree`).
