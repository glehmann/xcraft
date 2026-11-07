# xcraft — XCP-ng Distribution Build Tool

Build the RPM packages of [XCP-ng](https://xcp-ng.org/) from the specs of a project, xcp-ng-dist for XCP-ng, in a
container made only of AlmaLinux 10 packages, in the versions of a committed lock, and of the packages built there.

## Core principles

- **The packages are in the project**: each `<repo>/<pkg>/`, like `base/xen/`, has its spec, its patches and small
  sources, imported with their history. The source archives are not in git: they are listed with their SHA-256, and
  shared through a remote storage.
- **A reproducible build environment**: a container made only of upstream packages, from a base image pinned by
  digest, with the RPM macros of the distribution. Each build runs in a fresh one, without the network.
- **Committed locks**: `xcraft.lock` pins every upstream package, by its SHA-256, for all the architectures, resolved
  on any machine: an upstream update changes nothing until the lock is updated, on purpose. `xcraft.builds` has the
  build ID of each package of ours, and what its builds found that its spec does not say.
- **Our packages build against each other**: a local repository of our builds wins over upstream, so a change of a
  package is used by those built against it.
- **Builds are cached by content**: the key of a build covers the package, the keys of the packages it needs, the
  locked upstream packages and the build environment. Nothing is built twice, and a build of CI is reused as is
  everywhere.
- **Build numbers are committed**: a new key gets a new number, in the release as `+b<n>`, so that no number is given
  to two different builds. The other builds of a developer are development builds, `+b<n>~<c>.dev`, which upgrade each
  other and stay below the final build.
- **Signed and verified**: builds, caches and snapshots shared through the remote storage are signed, and verified
  before use; the signing key never reaches the builds.

## Getting started

Prerequisites:

- Python >= 3.13 and [uv](https://docs.astral.sh/uv/)
- podman, rootless is enough
- git and git-lfs, to import packages
- ssh and rsync, to copy repositories of development builds to test hosts
- gpg, to sign packages
- qemu-system-x86_64, qemu-img, and OVMF firmware, to boot installer VMs with `xcraft boot`

Everything else, rpm and dnf included, runs in the build environment. xcp-ng-dist pins the version of xcraft it uses:

```bash
cd xcp-ng-dist
uv sync && source .venv/bin/activate   # or uv run xcraft
xcraft --install-completion            # optional
xcraft doctor                          # what the host needs: --fix installs and configures it
xcraft status                          # the packages, with their build numbers, and whether they are built
```

Then, to change a package:

```bash
xcraft bump xen -m "a fix"             # its release, with a changelog entry, after editing its spec or patches
xcraft build --dry-run                 # what it rebuilds: the package, and those built against it
xcraft build xen --dependents          # development builds, +b<n>~1.dev
xcraft repo xen --host root@testhost --upgrade   # installed on a test host
xcraft log xen                         # the log of its last build
xcraft shell xen                       # a shell in its build tree, to debug it
```

and commit the change with `xcraft.builds`, and `xcraft.lock` when it changed. `xcraft --help` lists the commands,
and [DEMO.md](DEMO.md) is a tour of the main features.

## Documentation

- [Commands](docs/commands.md): all the commands and their main options.
- [How it works](docs/how-it-works.md): the project, its packages, the build environment, the configuration in
  `xcraft.toml`, and the architectures.
- [Builds](docs/builds.md): the build keys, the build IDs, reusing builds, and the development builds.
- [Dependencies and the lock](docs/dependencies.md): our packages and the upstream ones, `xcraft.lock`,
  `xcraft.builds`, and upstream updates.
- [Import and patchsets](docs/import.md): the history of the packages, source RPMs, and patches from git branches.
- [Installation ISO](docs/iso.md): `xcraft iso`, and booting the installer in a local VM with `xcraft boot`.
- [Remote storage](docs/remote-storage.md): sharing sources, builds, compiler caches and resolutions, and the signing
  key in CI.
- [Publishing and signing](docs/publishing.md): the snapshots of the repositories and ISOs, and their signatures.
- [Development](docs/development.md): the checks and tests of xcraft itself, and the
  [concepts and code map](docs/code-overview.md).

Design notes: [build keys](docs/build-keys.md), [the locks](docs/lock-design.md),
[rebuilds and releases](docs/rebuild-release-workflow-design.md),
[identical rebuilds](docs/identical-rebuilds-design.md), [CI of pull requests](docs/pr-ci-design.md),
[layout of the published repositories](docs/publishing-layout-design.md),
[the upstream mirror](docs/upstream-mirror-design.md), [compiler caches](docs/ccache-sync-design.md),
[the remote storage's security](docs/remote-storage-security-design.md),
[CI secrets](docs/ci-secret-management-design.md), and [faster development iterations](docs/dev-iteration.md).
