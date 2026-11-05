# Concepts and code map

xcraft builds an RPM distribution from a separate packaging project, such as xcp-ng-dist. Python on the host
coordinates Git, caches, storage, and containers; RPM and DNF run inside containers. Start with the command you
want to change in `xcraft/commands/`, then follow its calls into the modules below.

## Core concepts

- A **project** is a directory with `xcraft.toml`. `project.py` defines configuration, discovery, platform views,
  paths, and the project lock. Package directories live under configured repositories, such as `base/xen/`.
  Generated state, logs, and caches live under `.xcraft/`.
- A **package** is a source package defined by a spec; it can produce several binary RPMs. `packages.py` defines
  `Package` and `Binary`. `specs.py` parses specs using RPM in the build environment. Source archives are listed
  by checksum in each package's `sources` manifest and stored separately by `sources.py`.
- The **upstream lock**, `xcraft.lock`, pins upstream source versions and architecture-specific binary packages,
  including their checksums. It records what the image, build roots, published repositories, and ISO tools need.
  `lock.py` owns its model and persistence; `resolution.py` coordinates DNF resolution and lock preparation.
- The **build lock**, `xcraft.builds`, records build IDs and facts discovered while building: dynamic
  BuildRequires and generated binary requirements/provides. `builds.py` owns this file. These findings feed back
  into dependency resolution.
- A **build key** identifies build inputs across configured architectures. `buildkey.py` computes it from package
  contents, the recipe, locks, and dependency build IDs. `buildid.py` assigns build numbers and handles accepted
  reuse; `devbuilds.py` adds development-build identities. `cache.py` locates cached outputs using keys that also
  account for release suffixes.
- A **snapshot** contains distribution repositories, with optional build repositories and installer ISOs.
  `publish.py` assembles repositories; `upload.py` publishes or reuses snapshots in remote storage. Package
  signatures protect whoever installs the distribution; signed tree manifests protect what xcraft reads back from
  the remote storage, like the builds CI pushed. See `signing.py` and `remote.py`.

## Follow a build

1. `__main__.py` registers Typer commands. `commands/__init__.py::unprepared_project()` discovers a project without
   preparing metadata; `prepared_project()` also calls `resolution.prepare()`. Directory-only commands can avoid
   container work.
2. Preparation resolves upstream packages and records the lock. `graph.py` computes dependencies among project
   packages, including runtime dependencies of the binaries installed into build roots, and our packages replacing
   upstream ones of the image or of a build root, like `kernel-headers`, for the builds installing them.
3. `build.py::build()` selects an order and runs `_Run`, which coordinates cache reuse, remote fetches, execution,
   and results. `rpmbuild.py` stages inputs and invokes the build; `container.py` owns image construction and runs.
4. `checks.py` validates outputs and records findings. Changed findings can refresh dependencies and keys and cause
   bounded replanning. Source-directory hashes stay fixed for the run; edits during execution fail the build.

`Project.parsed` caches specs by platform, shared across platform views. `Project.packages` enriches them with build
findings; `spec_deps` and `deps` cache dependency maps. These properties can perform I/O. See
[During a command](lock-design.md#during-a-command) for what stays fixed during a command, what is computed again
after the locks change, and where the caches are cleared, before changing this lifecycle, especially inside `_Run`.

## Where to implement a feature

Paths below are relative to `xcraft/`.

| Feature | Start here |
| --- | --- |
| CLI option, selection, reporting | Matching `commands/*.py`; registration in `__main__.py`; output helpers in `display.py` |
| Project setting or architecture selection | `project.py`; consumers of `Config` and platform views |
| Package metadata or spec parsing | `packages.py`, `specs.py`, `buildenv/parse.py` |
| Dependency rules, upstream versions, pins | `graph.py`, `resolution.py`, `lock.py`, `pinned.py`, `upstream.py`, `commands/lock.py`; DNF logic in `buildenv/resolve.py` |
| Build identity, invalidation, reuse | `buildkey.py`, `buildid.py`, `devbuilds.py`, `cache.py`, `commands/reuse.py` |
| Build scheduling, execution, validation | `build.py`, `rpmbuild.py`, `checks.py`, `container.py`; in `buildenv/`, `build.sh` and the recording scripts `record.py`, `made.py`, `rpmdeps.py`, `requires.py` |
| Source downloads, imports, version bumps, patches | `sources.py`, `importer.py`, `commands/bump.py`, `release.py`, `patchset.py` |
| Local RPM repositories or metadata | `localrepo.py`, `repodata.py`, `commands/repo.py` |
| Snapshot assembly or publication | `publish.py` and `_Assembler`, `upload.py` and `Uploader`; orchestration in `commands/publish.py` |
| Remote transfers, verified trees, upstream mirror | `remote.py`, `upstream_copy.py`, `commands/push.py`; directory HTML in `remote_pages.py` |
| Signing or compiler-cache sharing | `signing.py`, `ccache.py` |
| Installer images or local VMs | `iso.py`, `buildenv/iso.py`, `vm.py`, `commands/boot.py` |
| Host prerequisites, subprocesses, cleanup | `host.py`, `process.py`, `clean.py` |

`buildenv/` contains scripts executed inside containers, with the container's RPM/DNF Python libraries. Do not
assume host dependencies are installed there. Resolve foreign architectures natively with `run_natively()` or
`run_dnf()`; emulate only operations that require target execution. Run architecture-independent tools on the
host platform through `project.on_host()`.

## Before changing behavior

Read [build keys](build-keys.md) for identity changes, with [identical rebuilds](identical-rebuilds-design.md) for
reuse, and [lock design](lock-design.md) for preparation and findings. For storage changes, read
[remote storage security](remote-storage-security-design.md), [upstream mirroring](upstream-mirror-design.md) and
[compiler cache syncing](ccache-sync-design.md). For CI, read [pull request CI](pr-ci-design.md),
[CI secrets](ci-secret-management-design.md) and [rebuild releases](rebuild-release-workflow-design.md). Preserve
`--locked`, checksum/signature checks, and existing lock boundaries. The `Containerfile`, `build.sh`, `offline.py`,
`record.py`, `made.py` and `rpmdeps.py` of `buildenv/` are in [the recipe](build-keys.md#the-recipe) of every build
key. Changes to these files normally invalidate every build key. For equivalent changes to the hashed build
scripts, `buildkey._SAME_BUILDS` can explicitly preserve the earlier script hash. This exception does not cover
`Containerfile` changes.

Tests use `tests/project/` and toy upstream packages, built once from the specs of `tests/upstream/` into
`tests/upstream/repos/`. The checked-in project has no prepared lock;
`tests/conftest.py::build_environment` prepares a template with containers. Extend the matching `tests/test_*.py`
using real packages where possible. See [Development](../README.md#development) for checks and test commands, and
follow `AGENTS.md` for formatting, typing, and test constraints.
