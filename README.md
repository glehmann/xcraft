# xcraft — XCP-ng Distribution Build Tool

Build the RPM packages of [XCP-ng](https://xcp-ng.org/) from the specs in this repository, in a container made only
of AlmaLinux 10 packages, in the versions of a committed lock, and of the packages built here.

## Prerequisites

- Python >= 3.13 and [uv](https://docs.astral.sh/uv/)
- podman, rootless is enough
- git and git-lfs, to import packages
- ssh and rsync, to copy repositories of test builds to test hosts
- gpg, to sign packages

Everything else, rpm and dnf included, runs in the build environment.

## Setup

```bash
uv sync
xcraft --install-completion  # optional
```

## Usage

| Command | Description |
|---|---|
| `xcraft build [<pkg>...]` | Build packages, all by default, and the packages they need, skipping what is already built |
| `xcraft build <pkg> --no-deps` | Build only the given packages |
| `xcraft build <pkg> --dependents` | Also build the packages built against them |
| `xcraft build <pkg> --dry-run` (`-n`) | Show what would be built, and how long it took last time |
| `xcraft build [<pkg>...] --no-fetch` | Build what is not cached locally, rather than fetch it from the remote storage |
| `xcraft build <pkg> -v` | Show the build log while building |
| `xcraft build <pkg> --locked` | Fail the builds that would change the lock: new upstream packages, or another build root, for CI |
| `xcraft build <pkg>... --test ID` / `--pre ID` | Test builds, to install on a test host (see [Test builds](#test-builds)) |
| `xcraft repo <pkg>... --test ID` / `--pre ID` | A repository of new test builds, for test hosts |
| `xcraft repo <pkg>... --test ID --host h [--upgrade]` | The same, copied to test hosts over ssh, and upgraded there |
| `xcraft publish [--locked] [--local] [--unsigned] [--dev-key] [--arch a]` | Publish a snapshot of the distribution, for all the architectures or `--arch`, as the latest one; a `--local` one is of the host's architecture by default |
| `xcraft iso [--netinstall] [--output f] [-V id] [--locked] [--unsigned]` | Make the installation ISO, from a local snapshot (see [Installation ISO](#installation-iso)) |
| `xcraft key create` / `xcraft key show [--export]` | The developer's key, which signs the test builds |
| `xcraft push` | Upload everything the remote storage misses: the source archives and the builds of all packages, then make all the `index.html` pages |
| `xcraft push sources [<pkg>...] [--check]` | Upload the source archives missing from the remote storage, or only check |
| `xcraft push builds [<pkg>...]` | Upload the current builds missing from the remote storage, for CI |
| `xcraft push index` | Make the `index.html` pages of all the directories of the remote storage |
| `xcraft fetch` | Download everything the caches miss: the current builds of all packages, then their source archives |
| `xcraft fetch sources [<pkg>...]` | Download the source archives missing from the source cache, from the remote storage, or else from the URL of Source0 |
| `xcraft fetch builds [<pkg>...]` | Download the current builds missing from the build cache |
| `xcraft lock` | Show the lock of upstream packages |
| `xcraft lock --update [<pkg>...]` | Update locked packages, all or some (binary or source names), to their latest versions |
| `xcraft impacts` | Show the pending updates: the upstream packages updated since the release of the packages built with them |
| `xcraft ack <pkg>... [--dep <upstream>]` | Clear pending updates without a new release |
| `xcraft repo [<pkg>...] [--output dir]` | Build packages, all by default, and what they need, put them in a repository, and check that everything they require is available |
| `xcraft shell <pkg>` | Open a shell in the build tree of a package, with its build dependencies installed and its sources prepared, where `rpmbuild -ba SPECS/*.spec` builds it |
| `xcraft log <pkg> [--step build] [--grep regex] [--last/-n n]` | Show the log of the last build of a package, in `$PAGER` on a terminal |
| `xcraft log <pkg> -f` | Follow the log of a running build, until it ends |
| `xcraft graph <pkg> [--dependents] [--upstream] [--flat]` | Show the packages a package is built against as a tree, or the packages built against it; `--upstream` adds the upstream binary packages; `--flat` only lists them, ours in build order |
| `xcraft status [<pkg>...]`, or `st` | Show the version of each package, or of the given ones, whether it is built, or queued by a running build, and its latest test builds |
| `xcraft bump <pkg>... [--dep <upstream>] [-m message]` | Bump the release of packages, with a changelog entry, empty to fill in without `-m`, and clear their pending updates; `--dep` selects the packages with a pending update of an upstream package |
| `xcraft bump <pkg> --version v [-m message]` | Update a package to a new version, with the archive of its `Source0`, and the packages of the same version; the changelog entry is `Update to <v>` by default |
| `xcraft clean [<pkg>...] [--builds] [--ccache]` | Remove the build trees and outdated builds, and optionally all builds and compiler caches, of the given packages or all; the source archives are kept |
| `xcraft patchset [--package p] [--base ref] [--first n]` | Update the patches of a package with the commits of the current git branch, from the developer's repository |
| `xcraft import <pkg>... [--branch b] [--url u] [--repo r]` | Copy packages from their packaging repositories, new ones into `specs/base/`, or `specs/<r>/` |
| `xcraft doctor [--fix]` | Check that the host has what xcraft needs, and with `--fix`, configure the emulation of the other architectures, with sudo, after showing the commands and asking |
| `xcraft import --all` | Re-import all packages from where they were imported |
| `xcraft --trace <command>` | Show stack traces on errors |

## How it works

**Project.** xcraft works on the specs that come with its code: the project is the checkout of xcraft containing
the code run, wherever the command is run from. `XCRAFT_PROJECT` can point to another one, like in the tests.

**Packages.** The packages are grouped by the repository they are published in: `specs/<repo>/<pkg>/`, like
`specs/base/xen/`. Their names are unique across the repositories. Each package directory holds a spec and its
sources, except the source archives: they are listed with their SHA-256 in `specs/<repo>/<pkg>/sources`, and stored in
`.xcraft/cache/sources/<sha256>/<filename>`. `xcraft import` fills a package directory from a packaging repository (by
default `https://github.com/xcp-ng-rpms/<pkg>`, on `master`), in `specs/base/` for a new package, or another
repository with `--repo`, and records where it comes from in `<pkg>/origin`. It
takes the archives out of git LFS for repositories with the `SPECS/` and `SOURCES/` layout, and downloads them from
the AlmaLinux lookaside cache for dist-git repositories. With `--url` ending with `.src.rpm`, it imports a source
RPM instead, for packages only published this way, like CentOS 7's. The spec of a package is `<pkg>.spec`, or its
only spec; other specs are sources of the main one.

**Metadata.** All specs are parsed by rpm in the build environment, so that macros and conditionals are evaluated as
in a build. The result is cached until a package directory or the image changes.

**Dependencies**: see [Dependencies](#dependencies).

**Build environment.** xcraft builds a container image from a base image pinned by digest, AlmaLinux 10, with only
the configured repositories, AlmaLinux 10 and its EPEL by default, the configured RPM macros (`%dist`...) and the
packages builds expect, in their locked versions. The image is tagged with a hash of its definition, so it is only
built again when the configuration or the locked versions of its packages change.

**Builds.** Each build runs in a fresh container: `dnf builddep`, then the dynamic `BuildRequires` if any, then
`rpmbuild`, with stdin from `/dev/null`. It runs against a local repository of what was built here, with a priority
over the other repositories, so a package built locally always wins. A build result is cached in
`.xcraft/cache/builds/<arch>/<pkg>/<key>`, under its [build key](#build-keys). A build whose log gets larger than 256 MiB,
like a test suite looping forever, is stopped.

**Locking.** The commands changing the project (`build`, `repo`, `shell`, `import`, `bump`, `clean`) cannot run at the
same time; the others, like `log -f` or `status`, can run during a build.

**Releases.** `xcraft bump` increments the last number of the `Release` outside of macros, which is the XCP-ng part
of releases like `1%{?xsrel}.3%{?dist}`, and adds a changelog entry for the resulting version, as rpm evaluates it.
With `--version`, it sets the `Version`, or the macro it is made of, like `%{usver}`, sets the `Release` to 1, with
the macros it ends with, like `%{?dist}`, downloads the archive of the new `Source0`, which must be its URL, and
replaces the old one in `sources`. The packages of the same archive, listed together in `same_version` in
`xcraft.toml`, like `kernel` and `kernel-headers`, get the same version.

**Configuration** is in `xcraft.toml`, which also marks the root of the project. All its settings are optional:

```toml
base_image = "ghcr.io/almalinux/10-base:10.2@sha256:..."   # image the build environment is made from
platforms = ["linux/amd64/v2", "linux/arm64"]   # the platforms of the builds, one at a time
packages = ["rpm-build", "gcc", ...]   # installed in the build environment
same_version = [["kernel", "kernel-headers"]]   # updated together by xcraft bump --version
[macros]                               # RPM macros of the build environment
dist = "%{?autorev}.xcpng8.99"
[repos.baseos]                         # the only repositories of the build environment, {arch} being its RPM arch
baseurl = "https://repo.almalinux.org/almalinux/10/BaseOS/{arch}/os/"
gpgkey = "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10"
includepkgs = ["foo"]                  # optional: the only packages taken from this repository
arches = ["x86_64_v2"]                 # optional: the only architectures it is for, like AlmaLinux's EPEL
[extra_upstream]                       # RPMs not available in these repositories, by URL
foo = "https://.../foo-1.0-1.noarch.rpm"
```

The defaults are in `xcraft/project.py`; a setting given in `xcraft.toml` replaces its default, `repos` included.

**Architectures.** xcraft works on one platform at a time: the one of the RPM architecture given with `--arch`, like
`aarch64`, or in `$XCRAFT_ARCH`, or else the host's, `x86_64_v2` on an x86_64 host. So `xcraft build foo` builds `foo`
for one architecture only. Another architecture is built under emulation, with podman and qemu-user-static, whose
registration needs the `C` flag for `sudo` in the containers, which Fedora's has not: `xcraft doctor` checks it, and
`xcraft doctor --fix` registers it in `/etc/binfmt.d/`. The emulation is fine for the small packages, while the large
ones are for native builders, which share their builds through the [remote storage](#remote-storage). xcraft solves the
metadata of the repositories of an emulated architecture natively, in its dnf cache: under emulation, that takes most of
the time of dnf. Each architecture has its lock, its build environment image, and its builds, in
`.xcraft/cache/builds/<arch>/`. AlmaLinux rebuilds EPEL for `x86_64_v2` only: the other architectures get Fedora's.
`xcraft bump` clears the pending updates of all the architectures.

## Build keys

Each build of a package has a **key**: a SHA-256 of everything that goes into it. xcraft never builds a package whose
build with the same key it already has, locally or in the [remote storage](#remote-storage), unless `--no-fetch`; it
builds it again as soon as one of its inputs changes. The key is the name of the build in
`.xcraft/cache/builds/<arch>/<pkg>/<key>/`, and in the storage, `builds/<arch>/<pkg>/<key>/`.

**What it covers.** The key of a package is made of:

| Part | What it is | Changed by |
|---|---|---|
| The build environment image | A hash of the base image, pinned by digest, the platform, `packages` of `xcraft.toml`, the repositories, the RPM macros (`%dist`...), and the locked versions of the image's packages | Any change to these settings, or an update of a package of the image |
| The platform | The one xcraft works on, see [Architectures](#configuration) | `--arch` |
| The extra upstream RPMs | The names and URLs of `extra_upstream` | `extra_upstream` |
| The recipe | The build script, the `.repo` file of the local repository, and the script recording the build roots | A change to how xcraft builds |
| The package | Its name, and all the files of `specs/<repo>/<pkg>/`, but `origin`: their paths, contents, and whether they are executable. The `sources` manifest covers the archives by their SHA-256 | Any change to the spec, a patch, a source file, or an archive |
| Its build root | The upstream packages its last build installed besides the image's, as recorded in the lock, with their locked versions | `xcraft lock --update` of one of them, or a build installing other ones |
| Its test build suffix | `.0.ID.<n>` or `~ID.<n>`, for `--test` and `--pre` only | Each test build |
| Its dependencies | The keys of the packages of ours its build installs (see [Dependencies](#dependencies)), like `kernel-headers`, in all the build roots | Any change to one of them, recursively |

The first four are common to all the packages: changing one of them rebuilds everything. Packages depending on each
other, a cycle, share the hash of them all, from which each one gets its own key.

**What it does not cover.** The path of the checkout, the machine, the user and the time; `origin`, and the files out of
`specs/<repo>/<pkg>/`, but those above; the SHA-256 and architecture of the upstream packages, only their versions. So the
same commit gives the same keys everywhere: a build pushed by CI, or by a colleague, is fetched by any checkout instead
of being built again. The local repository of the builds gets its own name in each checkout, `xcraft-<project id>`,
for its own metadata in the dnf cache the containers share, but the recipe the key covers has a placeholder instead.

**What a change rebuilds.**

- A spec or a patch: the package, and those built against it, directly or not (`xcraft graph --dependents <pkg>`);
- a version or a release bump: the same, as it changes the spec;
- an update of the lock: the packages with the updated package in their build root, and those built against them, or
  everything when the package is in the image;
- the configuration of the build environment, or a new version of xcraft changing the recipe: everything.

`xcraft build --dry-run` shows what would be built, fetched, or is cached, before building anything.

**The build root comes from the last build.** The key is known before building: it covers the build root of the last
build, from the lock. When a change makes a build install other upstream packages, a new `BuildRequires` for instance,
the lock gets its new build root, and the build is cached under the key computed with it, which the next commands find.
With `--locked`, for CI, such a build fails instead, as its key would not be the one of the committed lock.

## Test builds

While working on a package, a developer installs several builds of it on the same test host. Each one must upgrade
the previous one, and stay below the next real release. As with koji's `--test-build` and `--pre-build`:

- `xcraft build <pkg> --test ID` gives the release `<release>.0.ID.<n>`, above the current release and below the
  next one: `2.0.fix123.1.xcpng8.99` is between `2.xcpng8.99` and `3.xcpng8.99`;
- `xcraft build <pkg> --pre ID` gives `<release>~ID.<n>`, below the release, when the spec is already bumped to the
  coming one: `3~fix123.1.xcpng8.99` is below `3.xcpng8.99`.

`<n>` is the next number of the package for that ID and release, kept in `.xcraft/test-builds.json` (which
`xcraft clean` keeps), so it is specific to the checkout. The suffix goes into `%{autorev}`, part of `%{dist}`, so
the specs are not changed. With several packages, like `xcraft build --test ID xen qemu`, all of them are test builds,
built against each other. The build prints the paths of their RPMs. Test builds are cached apart from the normal
builds, and do not change the lock's build roots.

`xcraft repo <pkg>... --test ID` (or `--pre ID`) makes the next test builds, and puts them alone in a repository,
`repo-ID/` by default, with a `xcraft-ID.repo` file for the hosts, which keep their usual repositories for
everything else. Their dependencies are checked with our other current builds.

With `--host`, as given to ssh and repeatable, xcraft copies the repository to each host with rsync, in
`/var/local/xcraft/ID/`, and installs its `.repo` file, and with `--upgrade`, it also upgrades the test packages the
host has installed:

```
xcraft repo xen --test fix123 --host root@testhost --upgrade
```

Each `xcraft repo --test` makes new builds, which the upgrade takes over the previous ones. Without `--host`,
`--baseurl` gives where the hosts find the repository, for instance over http; by default, it is its directory, to
copy at the same place on the hosts.

## Patchsets

Developers maintain the changes of XCP-ng to a project in a branch of its git repository, or of a fork. `xcraft
patchset`, run in that repository on that branch, turns the commits of the branch into the patches of the package:

```bash
cd ~/src/xen-api   # on the branch of the XCP-ng changes
xcraft patchset --package xapi --base v26.4.0
```

- the package is the one named after the repository (its `origin` remote, or its directory): a package directory, a
  spec `Name`, or a `Source0` named `<repository>-<version>...`; `--package` gives it otherwise;
- the base of the branch is the tag of the version of the package, `v<version>` or `<version>`; `--base` gives it
  otherwise;
- the commits from the base are exported with `git format-patch --zero-commit`, so that the patches only change when
  the commits do, not when they are rebased;
- the spec lists them in a block managed by xcraft, numbered from 1000 (`--first`), and recording where they come
  from; an update replaces the block and removes the patches not used anymore. Other patches are kept. The first
  time, the patches of the spec with the same subject as exported commits are replaced, as previous exports of the
  patchset.

The patches are applied by `%autosetup` or `%autopatch`, in the order of their numbers. Then `xcraft build` checks
them, and `xcraft bump` makes a release of them.

## Installation ISO

`xcraft iso` makes the installation ISO of XCP-ng, like [create-install-image](https://github.com/xcp-ng/create-install-image)
does, from a snapshot like `xcraft publish --local` makes, of its architecture only, with nothing else to install:

- `install.img`, the installer's root filesystem, is installed from the snapshot: its packages, `[iso] installer` in
  `xcraft.toml`, are published with the others. The firmware files no driver names are removed, and the files of
  `iso/installimg/` are added, like its systemd units;
- the boot files come from our packages: the kernel, Xen's debug builds, grub's image for CDs and host-installer's
  grub configurations, for UEFI, with isolinux, for BIOS, configured by the files of `iso/tree/`;
- its repository has `[iso] packages`, `xcp-ng-deps` by default, and what they need, signed like the snapshot, unless
  `--netinstall`, where the installer gets the packages from the network;
- it is hybrid: it boots with BIOS and UEFI, from a CD or a USB stick.

The tools making it, like xorriso and syslinux, are installed in the containers making it, in the versions of the
lock, which records them in `iso`. The ISO is `.xcraft/iso/xcp-ng-<version>[-netinstall].iso` by default.

## Remote storage

The source archives are not in git, and builds take time: both can be shared with an S3 storage, read publicly over
http(s) and written with credentials:

```toml
[remote]
url = "https://xcraft.example/"           # public reads, like the website endpoint of a Garage bucket
s3_endpoint = "https://s3.example/"       # authenticated writes
bucket = "xcraft"
region = "garage"
```

The writes use the credentials of `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The storage has
`sources/<sha256>/<filename>` and `builds/<arch>/<package>/<key>/`, with a `manifest.json` of the build's files.

- When an archive of a manifest is not in the source cache, the build, or `xcraft fetch sources`, downloads it, from
  the storage, or else from the URL of the spec's Source0 when it has the name of the archive, and checks it against the
  manifest. So anyone can push sources: a bad archive cannot match the sha256 of the manifest, which is in git.
  `xcraft import` does not push: with `xcraft push sources`, the developer shares the archives with their change, and
  CI can check none is missing with `xcraft push sources --check`, which needs no credentials.
- When a package is not built locally, the build downloads its build with the same key, when the storage has it: the
  key covers everything the build depends on, so it is the same everywhere for the same commit. A build is a binary
  people install, so only CI should push builds, after `xcraft build --locked`, which fails when a build would
  change the lock, and so its key: `xcraft push builds`. Test builds are never pushed.

Without network, or when the storage is unavailable, everything still works with what is local.

The storage does not list its directories: xcraft writes the `index.html` page of each directory it changes, and of
their parents, to browse it. `xcraft push index` makes all of them, for what was not written by xcraft.

For development, `local-s3.sh` runs such a storage locally, with Garage in a container, and `xcraft.toml` uses it:

```
./local-s3.sh up                  # set it up, or start it again, and show the credentials to export
eval "$(./local-s3.sh env)"       # export them, to push
./local-s3.sh status              # whether it runs, and what it stores
./local-s3.sh down                # destroy it, with its data
```

It keeps its data in a podman volume, and its configuration in a podman secret, until `down`. Its public reads use
the name `xcraft.web.garage.localhost`, which resolves to the local host on most systems.

## Publishing

`xcraft publish` makes a snapshot of the distribution, for all the architectures: all our packages, and the upstream
ones they need at runtime, in the remote storage, laid out like the repositories of XCP-ng 8.3:

```
repos/<release>/<date>[.<n>]/base/<arch>/    our RPMs and the upstream ones, with their repodata, for each architecture
repos/<release>/<date>[.<n>]/base/Source/    the source RPMs of all of them
repos/<release>/<date>[.<n>]/build/<arch>/   the upstream RPMs our builds need, but those of base/<arch>/
repos/<release>/<date>[.<n>]/build/Source/   their source RPMs
repos/<release>/latest/...                   a copy of the latest snapshot
```

- Our packages are built or fetched, but only fetched for the architectures the host emulates: those are built by
  native builders, which push them. The publish fails when the remote storage misses one of their builds.

- What our packages need at runtime is resolved by dnf, for each of them alone, on an empty system, without weak
  dependencies, and goes in the lock, in `runtime`: a snapshot only changes with the lock, and `xcraft lock --update`
  updates these packages too, for security fixes. `[publish] extra` in `xcraft.toml` adds upstream packages for
  convenience, like `mc`, with what they need.
- The source RPMs of the upstream packages come from the AlmaLinux vaults, which keep all versions (the
  `source_baseurl` of the repositories): those of the binaries' metadata, as some binaries do not have the versions
  of their sources.
- The upstream RPMs and source RPMs must be signed with the keys of the repositories, those of the build
  environment, as dnf checks them when installing but not when downloading. The RPMs must also match the sha256 of
  the lock.
- Each package of the snapshot must install from the packages of its architecture alone: a self-contained repository.
- The build repository has what rebuilding our packages needs, with `base`: the packages of the build environment
  image and of the build roots, as locked, but the upstream ones ours replace, like `kernel-headers`. Each of its
  packages must install from it and `base`. `[publish] build = false` in `xcraft.toml` leaves it out, and so does
  `xcraft iso`, which does not need it.
- Our source RPMs are those of the first architecture: built from the same sources, those of the others differ by
  their build times only.
- All snapshots are kept. The storage stores identical files once: our packages are copies of the pushed builds, and
  the upstream ones and the sources of the previous snapshot, so a snapshot only costs what changed.

`xcraft publish --local` only makes the snapshot in `.xcraft/publish/`, of one architecture, like the other commands,
unless `--arch all` is given. A snapshot published to the storage is of all the architectures, unless `--arch` is
given: it replaces the latest one, whose other architectures would be gone. In CI, after `xcraft push builds`,
`xcraft publish --locked` fails rather than change the lock.

Only one `xcraft publish` must run at a time on a storage, like in a CI job of its own: Garage has no conditional
writes, so two publishes at the same time could take the same snapshot name, and mix their files.

## Signing

Our packages are signed with the host's gpg, which holds the key, by rpmsign in a container of its own: it uses the
restricted socket of the host's gpg agent, made for that, so the key never leaves the host, nor its HSM, and is never
available to the builds, which run the code of the specs.

- **Test builds** are signed with the developer's key, made with `xcraft key create`: an RSA key without passphrase,
  in its own keyring, `.xcraft/key/`, apart from the developer's own keys, as it is only for test hosts. The
  repositories of test builds then have its public key, and their `.repo` file checks the packages and the
  repository's metadata (`gpgcheck`, `repo_gpgcheck`).
- **Published packages** are signed with the key of `[sign]` in `xcraft.toml`:

  ```toml
  [sign]
  key = "<fingerprint>"   # used through gpg, so it can be in an HSM, like with gnupg-pkcs11-scd
  gnupghome = ""          # gpg's keyring, the default one when empty
  ```

  Each build is signed once, and kept in `.xcraft/cache/signed/<fingerprint>/` and in the remote storage, in
  `signed/<fingerprint>/`: signatures have a date, so signing again would not give the same files, which the storage
  could not store once. The snapshot's packages are checked against the public key, its `repomd.xml` are signed, and
  it has the key, as `RPM-GPG-KEY`. Without `[sign]`, `xcraft publish --local` signs with the developer's key, with a
  warning, and so does `xcraft publish` to a storage on this machine, whose URLs resolve to loopback addresses only,
  like the one of `local-s3.sh`; to another storage, it needs `--unsigned`, or `--dev-key` for a test storage.

The upstream packages keep their signatures, which are checked against the keys of their repositories.

## Dependencies

A package depends on two kinds of packages, handled differently.

**Our packages**, the ones in `specs/`, are known to xcraft from the parsed specs. A package depends on one of ours
when:

- one of its `BuildRequires` is the name, or an explicit `Provides`, of a binary package of ours;
- or, transitively, one of those binaries requires at runtime another binary of ours, since dnf installs it too in
  the build root.

Only the runtime requirements of the binaries actually needed count: `qemu` needs `xen` for `xen-libs-devel`, even
though another binary of `xen`, `xen-dom0-tools`, requires `qemu`. `xcraft graph <pkg>` shows these dependencies.

xcraft only sees the names in the specs: not the files our packages install, their automatic provides (sonames,
`pkgconfig()`...), nor versions. So after each build, it checks that the build root matches these dependencies: the
build fails when dnf installed a binary of ours from a package not among them, which the cache key and the build order
would miss, or an upstream package with the name of one of ours, instead of ours. A `BuildRequires` on the name of
the package fixes both.

They decide:

- the build order: our dependencies are built first, and a failed build skips the packages needing it;
- the cache keys: the key of a package includes the keys of its dependencies, so changing a package rebuilds the
  packages built against it;
- what the builds get: the builds of our packages are in a local repository with `priority=0`, above the other
  repositories, so dnf takes our package whenever we built one, even when a repository has a newer version (like
  AlmaLinux's ocaml 5.2, while we build 4.14).

**Everything else** comes from the repositories of the build environment: by default AlmaLinux 10 (BaseOS, AppStream,
CRB, extras, devel) and its EPEL, which get updates, the security fixes in particular. xcraft doesn't model these
dependencies: in each build, `dnf builddep` resolves and installs the `BuildRequires`, with its own choice among
providers. But dnf only sees the locked version of the packages in the lock (see below). A build environment is
therefore made of the locked packages and of `specs/`: a package of ours that is not built, with `--no-deps` for
instance, is not available.

To see them anyway, `xcraft graph --upstream` resolves the requirements between the locked packages, in the build
environment, and shows those our builds need: the providers of our `BuildRequires`, and of the runtime requirements
of our binaries installed in the build roots, with what they require. `xcraft graph --dependents python3` thus
shows our packages built against `python3`, through the upstream packages requiring it.

## The lock

`locks/<arch>.toml` is committed with the specs. It has:

- `[packages]`: one version of each upstream package installed in the build environment image or in a build root,
  for the whole tree, with its source package, repository and SHA-256, on one line, for readable diffs;
- `image`: the upstream packages of the build environment image;
- `[buildroots]`: for each of our packages, the upstream packages its last build installed besides the image's;
- `[pending.<pkg>]`: for each of our packages, the upstream packages of its build root updated since its release, with
  the versions it was built with.

In the build environment, dnf only sees the locked version of a locked package. A build installing packages not
locked yet, for a new `BuildRequires` for instance, takes their latest versions and adds them to the lock, which is
then committed with the spec change; with `--locked`, for CI, such a build fails instead. The lock only changes
otherwise with `xcraft lock --update`, which takes the latest versions of all the locked packages, or of some of them,
given by binary or source package name, to get updates and security fixes.

Since the cache key of a package covers the locked versions of its build root, an update rebuilds the packages whose
build root has an updated package, and the packages built against them.

A rebuild is not a release: rebuilt packages keep their version-release, so hosts keep the one published. For each
update, the packages with the updated package in their build root, or in the image, get pending entries, which
`xcraft impacts` shows, grouped by upstream package, with whether each package builds with the current lock. They
stay until decided: `xcraft bump`, when the update requires shipping a rebuild (static linking, OCaml module hashes,
soname changes...), or `xcraft ack`, when it does not, like most ABI compatible updates, with the reason in the
commit. Both work on packages, or on all the packages with a pending update of an upstream package, with `--dep`.

**Limits.** Only the dependencies written in the specs are known before building. Those rpm generates while building,
like libraries (`libfoo.so.1()(64bit)`) or OCaml modules (`ocaml(Stdlib) = <hash>`), are not, so a package requiring
one of ours only that way is not ordered after it.

**Checking.** `xcraft repo` runs `dnf repoclosure` on the built packages, with all their requirements, generated
ones included, against the local repository and the build environment's ones: it reports what nothing provides.

## Development

```bash
make install    # the dependencies, with the dev ones, and the pre-commit hooks
make check      # ruff, ruff format, ty, typos and zizmor, like the GitHub workflows, without changing anything
make fix        # ruff's fixes, like the order of the imports, the formatting, and the typos
make test       # pytest; the build tests need podman, and the network the first time, to build the image
```

Each check also runs alone, by the id of its hook in `.pre-commit-config.yaml`, like `make ty`. The workflows also
run for the pull requests of forks.
