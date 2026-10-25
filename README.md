# xcraft — XCP-ng Distribution Build Tool

Build the RPM packages of [XCP-ng](https://xcp-ng.org/) from the specs of a project, xcp-ng-dist for XCP-ng, in a
container made only of AlmaLinux 10 packages, in the versions of a committed lock, and of the packages built there.

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
| `xcraft build [<pkg>...] --fetch=needed\|none` | Only fetch from the remote storage the builds the packages to build need, like CI, which then only builds what the storage misses; or fetch nothing, and build what is not cached locally |
| `xcraft build <pkg> -v` | Show the build log while building |
| `xcraft build <pkg> --locked` | Fail the builds that would change the lock: new upstream packages, or what a build found, for CI |
| `xcraft build <pkg>... --test ID` / `--pre ID` | Test builds, to install on a test host (see [Test builds](#test-builds)) |
| `xcraft repo <pkg>... --test ID` / `--pre ID` | A repository of new test builds, for test hosts |
| `xcraft repo <pkg>... --test ID --host h [--upgrade]` | The same, copied to test hosts over ssh, and upgraded there |
| `xcraft publish [repos\|isos] [--locked] [--local] [--unsigned] [--dev-key] [--arch a]` | Publish a snapshot of the distribution, its repositories and its ISOs, or only those given, for all the architectures or `--arch`, as the latest one; a `--local` one is of the host's architecture by default |
| `xcraft iso [--netinstall] [--output f] [-V id] [--locked] [--unsigned]` | Make the installation ISO, from a local snapshot (see [Installation ISO](#installation-iso)) |
| `xcraft key create` / `xcraft key show [--export]` | The developer's key, which signs the test builds |
| `xcraft push` | Upload everything the remote storage misses: the source archives and the builds of all packages, with the `index.html` pages of their directories |
| `xcraft push sources [<pkg>...] [--check]` | Upload the source archives missing from the remote storage, or only check |
| `xcraft push builds [<pkg>...]` | Upload the current builds missing from the remote storage, for CI |
| `xcraft push index` | Make the `index.html` pages of all the directories of the remote storage |
| `xcraft push ccache [<pkg>...]` | Upload the compiler caches of the packages, replacing those of the remote storage, for CI |
| `xcraft fetch` | Download everything the caches miss: the current builds of all packages, then their source archives |
| `xcraft fetch sources [<pkg>...]` | Download the source archives missing from the source cache, from the remote storage, or else from the source of the spec with their URL |
| `xcraft fetch builds [<pkg>...]` | Download the current builds missing from the build cache |
| `xcraft fetch ccache [<pkg>...]` | Download the compiler caches of the packages, replacing the local ones |
| `xcraft lock` | Show the lock of upstream packages, the same for all the architectures |
| `xcraft lock --outdated [<pkg>...]` | Show what `--update` would change, without changing it, and fail then, for CI |
| `xcraft lock --update [<pkg>...]` | Update locked packages, all or some (binary or source names), to the latest version all the architectures have, looked for natively, on any machine |
| `xcraft impacts [--markdown]` | Show the pending updates: the upstream source packages updated since the release of the packages built with them; `--markdown` as a Markdown table, like for a pull request |
| `xcraft ack <pkg>... [--dep <upstream>]` | Clear pending updates without a new release |
| `xcraft repo [<pkg>...] [--output dir]` | Build packages, all by default, and what they need, put them in a repository, and check that everything they require is available |
| `xcraft shell <pkg>` | Open a shell in the build tree of a package, with its build dependencies installed and its sources prepared, where `rpmbuild -ba SPECS/<spec>` builds it, without the network like the builds, the exact command shown before the prompt |
| `xcraft log <pkg> [--step build] [--grep regex] [--last/-n n]` | Show the log of the last build of a package, in `$PAGER` on a terminal |
| `xcraft log <pkg> -f` | Follow the log of a running build, until it ends |
| `xcraft graph <pkg> [--dependents] [--upstream] [--flat]` | Show the packages a package is built against as a tree, or the packages built against it; `--upstream` adds the upstream binary packages; `--flat` only lists them, ours in build order |
| `xcraft status [<pkg>...]`, or `st` | Show the version of each package, or of the given ones, whether it is built, or queued by a running build, and its latest test builds |
| `xcraft bump <pkg>... [--dep <upstream>] [-m message]` | Bump the release of packages, with a changelog entry, empty to fill in without `-m`, and clear their pending updates; `--dep` selects the packages with a pending update of an upstream package |
| `xcraft bump <pkg> --version v [-m message]` | Update a package to a new version, with the archive of its `Source0`, and the packages of the same version; the changelog entry is `Update to <v>` by default |
| `xcraft clean [<pkg>...] [--builds] [--ccache]` | Remove the build trees and outdated builds, and optionally all builds and compiler caches, of the given packages or all; the source archives are kept |
| `xcraft patchset [--package p] [--base ref] [--first n]` | Update the patches of a package with the commits of the current git branch, from the developer's repository |
| `xcraft import <pkg or srpm>... [--branch b] [--url u] [--repo r]` | Import the history of packages from their packaging repositories, or source RPMs, as the git branches `import/<pkg>/<branch>` to merge, new packages into `base/`, or `<r>/` |
| `xcraft doctor [--fix]` | Check that the host has what xcraft needs, and show the commands fixing what it misses; with `--fix`, run them, with sudo, after showing them and asking: the installs of the missing tools, with the package manager of Debian, Fedora/RHEL, SUSE, Arch, Alpine, Gentoo or Slackware and their derivatives, the subordinate ids of rootless podman, and the emulation of the other architectures |
| `xcraft import --all` | Import again the packages which have import branches of histories, not of source RPMs only |
| `xcraft --trace <command>` | Show stack traces on errors |

## How it works

**Project.** Like git, xcraft works on the project of the current directory: the first of it and its parents with an
`xcraft.toml`. If none is found, it searches the parents of the xcraft executable, so an installation in a project's
virtualenv can find that project from elsewhere. `XCRAFT_PROJECT` can point to another one, like in the tests.
XCP-ng's is xcp-ng-dist, which pins the version of xcraft it uses: run `uv sync` there, then `uv run xcraft`, or
`xcraft` in its activated `.venv`.

**Packages.** The packages are grouped by the repository they are published in: `<repo>/<pkg>/`, like `base/xen/`,
for the `repositories` of `xcraft.toml`, `["base"]` by default. Their names are unique across the repositories. Each
package directory holds a spec and its sources, except the source archives: they are listed with their SHA-256 in
`<repo>/<pkg>/sources`, and stored in `.xcraft/cache/sources/<sha256>/<filename>`. Packages come into the project
with their history, see [Import](#import). The spec of a package is `<pkg>.spec`, or its only spec; other specs are
sources of the main one.

**Metadata.** All specs are parsed by rpm in the build environment, so that macros and conditionals are evaluated as
in a build. The result is cached until a package directory or the image changes.

**Dependencies**: see [Dependencies](#dependencies).

**Build environment.** xcraft builds a container image from a base image pinned by digest, AlmaLinux 10 for XCP-ng,
with only the configured repositories, AlmaLinux 10 and its EPEL for XCP-ng, the configured RPM macros (`%dist`...)
and the packages builds expect, with those xcraft uses, in their locked versions. The image is tagged with a hash of its definition, so it is only
built again when the configuration or the locked versions of its packages change.

**Builds.** Each build runs in a fresh container: `dnf builddep`, then the dynamic `BuildRequires` if any, then
`rpmbuild`, with stdin from `/dev/null`, and without the network, like koji and mock: only the loopback interface. It runs against a local repository of what was built here, with a priority
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

**Configuration** is in `xcraft.toml`, which also marks the root of the project. Its settings are optional, but
`base_image`, `platforms` and `repos`:

```toml
base_image = "ghcr.io/almalinux/10-base:10.2@sha256:..."   # image the build environment is made from
platforms = ["linux/amd64/v2", "linux/arm64"]   # the platforms of the builds, one at a time
packages = ["rpm-build", "gcc", ...]   # installed in the build environment, with what xcraft uses
same_version = [["kernel", "kernel-headers"]]   # updated together by xcraft bump --version
[ccache_size]                          # the maximum size of the compiler cache of packages, 5G by default
kernel = "20G"
[macros]                               # RPM macros of the build environment
dist = "%{?autorev}.xcpng8.99"
[repos.baseos]                         # the only repositories of the build environment, {arch} being its RPM arch
baseurl = "https://repo.almalinux.org/almalinux/10/BaseOS/{arch}/os/"   # or file:///..., mounted in the containers
gpgkey = "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10"
includepkgs = ["foo"]                  # optional: the only packages taken from this repository
arches = ["x86_64_v2"]                 # optional: the only architectures it is for, like AlmaLinux's EPEL
[extra_upstream]                       # RPMs not available in these repositories, by URL
foo = "https://.../foo-1.0-1.noarch.rpm"
```

The settings of XCP-ng are in the `xcraft.toml` of xcp-ng-dist, and those of the tests in `tests/project/xcraft.toml`,
the project they copy.

**Architectures.** xcraft works on one platform at a time: the one of the RPM architecture given with `--arch`, like
`aarch64`, or in `$XCRAFT_ARCH`, or else the host's, `x86_64_v2` on an x86_64 host. So `xcraft build foo` builds `foo`
for one architecture only. Another architecture is built under emulation, with podman and qemu-user-static, whose
registration needs the `C` flag for `sudo` in the containers, which Fedora's has not: `xcraft doctor` checks it, and
`xcraft doctor --fix` installs the emulator and registers it in `/etc/binfmt.d/`, with systemd-binfmt, or else directly. The emulation is fine for the small packages, but their builds
keep the network: qemu-user cannot isolate them, while the large ones are for native builders, which share their builds
through the [remote storage](#remote-storage). xcraft solves the metadata of the repositories of an emulated
architecture natively, in its dnf cache: under emulation, that takes most of the time of dnf. Each architecture has its
lock, its build environment image, and its builds, in `.xcraft/cache/builds/<arch>/`. AlmaLinux rebuilds EPEL for
`x86_64_v2` only: the other architectures get Fedora's. `xcraft bump` clears the pending updates of all the
architectures.

## Build keys

Each build of a package has a **key**: a SHA-256 of everything that goes into it. xcraft never builds a package whose
build with the same key it already has, locally or in the [remote storage](#remote-storage), unless `--fetch=none`; it
builds it again as soon as one of its inputs changes. The key is the name of the build in
`.xcraft/cache/builds/<arch>/<pkg>/<key>/`, and in the storage, `builds/<arch>/<pkg>/<key>/`.

**What it covers.** The key of a package is made of:

| Part | What it is | Changed by |
|---|---|---|
| The build environment image | A hash of the base image, pinned by digest, the platform, `packages` of `xcraft.toml`, the repositories, the RPM macros (`%dist`...), and the versions of the image's packages, resolved from the lock | Any change to these settings, or an update of a package of the image |
| The platform | The one xcraft works on, see [Architectures](#configuration) | `--arch` |
| The extra upstream RPMs | The names and URLs of `extra_upstream` | `extra_upstream` |
| The recipe | The build script, `xcraft/buildenv/build.sh`, the `.repo` file of the local repository, and the scripts recording what the builds install and isolating them from the network | A change to how xcraft builds, but one declared making the same packages |
| The package | Its name, and all the files of `<repo>/<pkg>/`, but `origin`: their paths, contents, and whether they are executable. The `sources` manifest covers the archives by their SHA-256 | Any change to the spec, a patch, a source file, or an archive |
| Its build root | The upstream packages of its build root besides the image's, resolved from its `BuildRequires` and the lock (see [The lock](#the-lock)), with their versions | `xcraft lock --update` of one of them, a change of its `BuildRequires`, or what the builds found |
| Its test build suffix | `.0.ID.<n>` or `~ID.<n>`, for `--test` and `--pre` only | Each test build |
| Its dependencies | The keys of the packages of ours its build installs (see [Dependencies](#dependencies)), like `kernel-headers`, in all the build roots | Any change to one of them, recursively |

The first four are common to all the packages: changing one of them rebuilds everything. Packages depending on each
other, a cycle, share the hash of them all, from which each one gets its own key.

**What it does not cover.** The path of the checkout, the machine, the user and the time; `origin`, and the files out of
`<repo>/<pkg>/`, but those above; the SHA-256 and architecture of the upstream packages, only their versions. So the
same commit gives the same keys everywhere: a build pushed by CI, or by a colleague, is fetched by any checkout instead
of being built again. The local repository of the builds gets its own name in each checkout, `xcraft-<project id>`,
for its own metadata in the dnf cache the containers share, but the recipe the key covers has a placeholder instead.

**What a change rebuilds.**

- A spec or a patch: the package, and those built against it, directly or not (`xcraft graph --dependents <pkg>`);
- a version or a release bump: the same, as it changes the spec;
- an update of the lock: the packages with the updated package in their build root, and those built against them, or
  everything when the package is in the image;
- the configuration of the build environment, or a new version of xcraft changing the recipe: everything.

**Same builds.** A change to the recipe which makes the same packages, like a fix of how it fails, can keep the
builds: `_SAME_BUILDS` of `xcraft/cache.py` maps its hash to the one of the earliest recipe making the same packages,
which stays in the keys.
Not the packages, nor the image: what a package directory has goes in its source RPM, and what the image has can change
the builds. `test_recipe` fails on a change to the recipe in the keys, giving the new hash, to choose between both.

`xcraft build --dry-run` shows what would be built or fetched, with how many packages are cached, before building
anything. The builds only show the packages they build or fetch, with how many are cached.

**The build root is resolved.** The key is known before building, on every architecture: it covers the build root
dnf resolves from the `BuildRequires` and the lock, without building. When a build finds what changes the build roots,
like a new requirement rpm generated, the lock gets it, and the builds are cached under the keys computed with it,
which the next commands find. With `--locked`, for CI, such a build fails instead, as its key would not be the one of
the committed lock.

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
builds.

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

## Import

`xcraft import <pkg>` imports the history of a package from its packaging repository, `https://github.com/xcp-ng-rpms/<pkg>`
or `--url`, into the git repository of the project: each branch of `branches` of `[import]` in `xcraft.toml`,
`["master"]` by default, like `["master", "XS-9.0"]`, or `--branch`, becomes the branch `import/<pkg>/<branch>`. The
project merges it to bring the package, in `base/` for a new one, or another repository with `--repo`. Only git is
used.

The commits are rewritten for the project: the files of `SPECS/` and `SOURCES/` in the package directory, the archives
stored with git LFS in its `sources` manifest, by the SHA-256 of their LFS pointer, so without downloading them, and
nothing else, like the README and `.github/`. Links and submodules are refused, like a link to a file of the host. The
commits left without a change are skipped, and so are the merges of what another parent already merges. Each commit
gets the trailer `Imported-from: <owner>/<repository>@<commit>`, and keeps its author, committer, dates and message.

The project's history must start with an empty commit: the first imported commits get it as parent. The rewrite only
depends on the commits imported and on it: importing again gives the same commits, with the new ones on top, and the
branches sharing commits, like master merging XS-9.0, share them. A merge brings the package into the project, with
its history, and the next ones bring the new commits, which `xcraft import --all` imports for all the packages with
import branches of histories:

```
git merge import/xen/master import/libempserver/master
```

The package must not be in the project before its first merge: `git log` and `git blame` follow a merge only through
the parents it is the same as, which would be the project's.

`xcraft import <srpm>`, a path or a URL ending with `.src.rpm`, imports a source RPM, like a new one of XenServer, as
a new commit of the import branch of its package, `srpm_branch` of `[import]`, from the first commit of the project for
a new package. Its files are laid out the same way, its archives go into the source cache, and `removed` lists those to
remove for legal reasons, each replaced by a `<file>.deleted-by-XCP-ng.txt` saying so:

```toml
[import]
branches = ["master", "XS-9.0"]
srpm_branch = "XS-9.0"
removed = ["Citrix_Logo_Black.png", "COPYING.CitrixCommercial"]
```

A merge brings it into the project, like the history. The import of a history refuses to move an import branch which
has commits it does not import, like those of a source RPM: the packaging repository is then no longer followed.

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
  `iso/installimg/` of the project are added, like its systemd units;
- the boot files come from our packages: the kernel, Xen's debug builds, grub's image for CDs and host-installer's
  grub configurations, for UEFI, with isolinux, for BIOS, configured by the files of `iso/tree/`;
- its repository has `[iso] packages`, like `xcp-ng-deps`, and what they need, signed like the snapshot, unless
  `--netinstall`, where the installer gets the packages from the network;
- it is hybrid: it boots with BIOS and UEFI, from a CD or a USB stick.

The tools making it, like xorriso and syslinux, are installed in the containers making it, in the versions of the
lock, which records them in `iso`. The ISO is `.xcraft/iso/xcp-ng-<version>[-netinstall].iso` by default.
`xcraft publish` publishes both ISOs, with the packages and netinstall, with the snapshot: see
[Publishing](#publishing).

## Remote storage

The source archives are not in git, and builds take time: both can be shared with an S3 storage, read publicly over
http(s) and written with credentials:

```toml
[remote]
url = "https://xcraft.example/"           # public reads, like the website endpoint of a Garage bucket
s3_endpoint = "https://s3.example/"       # authenticated writes
bucket = "xcraft"
region = "garage"
build_keys = ["<full GPG fingerprint>"] # trusted public keys for verifying trees
```

The writes use the credentials of `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The storage has
`sources/<sha256>/<filename>` and `builds/<arch>/<package>/<key>/`, with a `manifest.json` of the build's files.

- When an archive of a manifest is not in the source cache, the build, or `xcraft fetch sources`, downloads it, from
  the storage, or else from the source of the spec whose URL has the name of the archive, and checks it against the
  manifest. So anyone can push sources: a bad archive cannot match the sha256 of the manifest, which is in git.
  `xcraft import` does not push: with `xcraft push sources`, the developer shares the archives with their change, and
  CI can check none is missing with `xcraft push sources --check`, which needs no credentials.
- When a package is not built locally, the build downloads its build with the same key, when the storage has it: the
  key covers everything the build depends on, so it is the same everywhere for the same commit. A build is a binary
  people install, so only CI should push builds, after `xcraft build --locked`, which fails when a build would
  change the lock, and so its key: `xcraft push builds`. Test builds are never pushed. With `--fetch=needed`, CI
  leaves in the storage the builds it has, only fetching those of the dependencies of the packages it builds.
- A build without the compiler cache of its package downloads the one of the storage first, unless `--fetch=none`: the
  one CI pushes after its builds, with `xcraft push ccache`, so that rebuilding a modified package, like the kernel
  with a patch, mostly finds its objects. The cache of a package is a tarball, in `ccache/<arch>/<package>/`, rather
  than read by ccache from the storage, as the builds run without the network. `xcraft fetch ccache` gets it again.
  `xcraft push ccache` only uploads the entries the last successful build of the package used or made, which ccache
  dates of their last use: not those of the older builds, which the local cache keeps.

Without network, or when the storage is unavailable, everything still works with what is local.

Build and compiler cache uploads, publishing and `xcraft iso` accept `--key <fingerprint>` to select a host GPG key.
Without it, they use `[sign]` when configured, or the checkout's developer key made with `xcraft key create`.
The keyring is `[sign].gnupghome`, or `GNUPGHOME` when it is omitted. Publishing uses its selected signer for signed
builds and snapshot manifests. `--unsigned` skips package signing but remote manifests still need a default signing
key; it cannot be combined with `--key` or `--dev-key`.
`remote.build_keys` only lists trusted public keys for verification; its order does not select the signing key.
The checkout also trusts its own developer key, without any configuration change.
Public keys are uploaded to `keys/<fingerprint>.asc` and checked against the configured
fingerprints. Each detached `manifest.json.asc` signs the UTF-8 tree path, a newline, and the exact manifest bytes.
Missing or invalid signatures make a remote tree unavailable. There is no unsigned-tree fallback.

To rotate CI keys, change the fingerprint passed with `--key`, add it to `remote.build_keys`, and retain the old
fingerprints while their trees are still needed.

### The signing key in CI

Builds, ccache uploads and publishing use the same private key through the runner's GPG agent. A persistent signing
runner can use an HSM or an existing host keyring, just as publishing does. The build containers receive neither the
private key nor the agent socket. Only the separate RPM-signing container gets the agent's restricted socket.

For an ephemeral runner, store the armored release private key in a protected CI secret and import it into a temporary
keyring before pushing or publishing. Keep only `remote.build_keys` in the committed configuration; CI selects the
private key with `--key`. With `[sign].gnupghome` omitted, a CI shell step can use:

```sh
export GNUPGHOME="$(mktemp -d /tmp/xcraft-gnupg.XXXXXX)"
chmod 700 "$GNUPGHOME"
trap 'gpgconf --kill gpg-agent; rm -rf "$GNUPGHOME"' EXIT
gpg --batch --import "$CI_SIGNING_KEY_FILE"
xcraft push builds --key "$SIGNING_KEY_FINGERPRINT"
xcraft push ccache --key "$SIGNING_KEY_FINGERPRINT"
xcraft publish --key "$SIGNING_KEY_FINGERPRINT"
```

`CI_SIGNING_KEY_FILE` is a CI-provided secret file; `SIGNING_KEY_FINGERPRINT` is a public CI variable. Neither requires
changing `xcraft.toml` in CI. If `[sign].gnupghome` is set, import into that
keyring instead. A passphrase-protected key needs its GPG agent unlocked before these commands, with a CI secret and
`gpg-preset-passphrase` on a runner configured with `allow-preset-passphrase`. xcraft uses batch GPG and does not prompt
for a passphrase. Give the key and S3 write credentials only to trusted jobs; pull-request builds can fetch and build
without them. Disable shell tracing while handling secrets.

The storage does not list its directories: xcraft writes the `index.html` page of each directory it changes, and of
their parents, to browse it. `xcraft push index` makes all of them, for what was not written by xcraft.

For development, `local-s3.sh` runs such a storage locally, with Garage in a container, and the `xcraft.toml` of xcp-ng-dist uses
it:

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
isos/<release>/<date>[.<n>]/<arch>/          the ISOs, with the packages and netinstall, and their signatures (.asc)
isos/<release>/latest/...                    a copy of the latest ISOs
```

`<release>` is `[publish] release` of `xcraft.toml`, like `9.0`.

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
  the metadata of their repositories.
- Each package of the snapshot must install from the packages of its architecture alone: a self-contained repository.
- The build repository has what rebuilding our packages needs, with `base`: the packages of the build environment
  image and of the build roots, as resolved, but the upstream ones ours replace, like `kernel-headers`. Each of its
  packages must install from it and `base`. `[publish] build = false` in `xcraft.toml` leaves it out, and so does
  `xcraft iso`, which does not need it.
- Our source RPMs are those of the first architecture: built from the same sources, those of the others differ by
  their build times only.
- The ISOs are made like `xcraft iso` makes them, from the snapshot, for its x86_64 architectures, when `[iso]
  installer` is configured: their boot loaders are for x86_64. They are signed like the snapshot: their repository,
  and their files, with a detached signature.
- All snapshots are kept. The storage stores identical files once: our packages are copies of the pushed builds, and
  the upstream ones and the sources of the previous snapshot, so a snapshot only costs what changed.

`xcraft publish repos` only publishes the repositories, and `xcraft publish isos` the ISOs, under a snapshot name
used by neither. `xcraft publish --local` only makes the snapshot in `.xcraft/publish/`, its ISOs in its `iso/`, of one
architecture, like the other commands,
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
  it has the key, as `RPM-GPG-KEY`. `--key <fingerprint>` overrides `[sign].key`, and `--dev-key` explicitly selects the
  developer's key even when `[sign]` exists. Without `[sign]` or `--key`, publishing uses the developer's key by
  default. Trees signed with that key are trusted by the same checkout without adding it to `remote.build_keys`.

The upstream packages keep their signatures, which are checked against the keys of their repositories.

## Dependencies

A package depends on two kinds of packages, handled differently.

**Our packages**, the ones of the project, are known to xcraft from the parsed specs. A package depends on one of ours
when:

- one of its `BuildRequires` is the name, or an explicit `Provides`, of a binary package of ours;
- or, transitively, one of those binaries requires at runtime another binary of ours, since dnf installs it too in
  the build root.

Only the runtime requirements of the binaries actually needed count: `qemu` needs `xen` for `xen-libs-devel`, even
though another binary of `xen`, `xen-dom0-tools`, requires `qemu`. `xcraft graph <pkg>` shows these dependencies.

The specs do not have what rpm generates while building: the requirements of what our packages link against, their
automatic provides (sonames, `pkgconfig()`...), and their files. The builds record them in the lock, for the packages
built against them (see [The lock](#the-lock)). A build root with an upstream package with the name of one of ours,
instead of ours, fails the build: a `BuildRequires` on the name of the package fixes it.

They decide:

- the build order: our dependencies are built first, and a failed build skips the packages needing it;
- the cache keys: the key of a package includes the keys of its dependencies, so changing a package rebuilds the
  packages built against it;
- what the builds get: the builds of our packages are in a local repository with `priority=0`, above the other
  repositories, so dnf takes our package whenever we built one, even when a repository has a newer version (like
  AlmaLinux's ocaml 5.2, while we build 4.14).

**Everything else** comes from the repositories of the build environment: for XCP-ng, AlmaLinux 10 (BaseOS, AppStream,
CRB, extras, devel) and its EPEL, which get updates, the security fixes in particular. dnf resolves them, in the
locked versions only (see below): before building, to know the build roots, and in each build, where `dnf builddep`
installs the `BuildRequires`. A build environment is therefore made of the locked packages and of ours: a package of
ours that is not built, with `--no-deps` for instance, is not available.

To see them anyway, `xcraft graph --upstream` resolves the requirements between the locked packages, in the build
environment, and shows those our builds need: the providers of our `BuildRequires`, and of the runtime requirements
of our binaries installed in the build roots, with what they require. `xcraft graph --dependents python3` thus
shows our packages built against `python3`, through the upstream packages requiring it.

## The lock

`xcraft.lock`, a TOML file next to `xcraft.toml`, is committed with the specs. It is the same for all the
architectures, and has, one line per package for readable diffs:

- `[sources]`: one version of each upstream source package installed in the build environment image, in a build root,
  with the tools making the ISO, or published, for the whole tree. On each architecture, dnf only sees the binary
  packages of that version, but the `release_suffixes` of `xcraft.toml`, like `[".alma_altarch"]` of the packages
  AlmaLinux rebuilds for x86_64_v2.
- What the builds found, which the specs do not have: `[requires]`, for each binary package of ours, the packages the
  requirements rpm generated for it resolve to, like `lzo` for `xen-dom0-libs`, which links against it; `[provides]`,
  the capabilities and files rpm generated for it that a spec of ours requires; `[buildrequires]`, for each package
  with `%generate_buildrequires`, its dynamic `BuildRequires`. An architecture which finds
  others has its own entries, which replace the shared ones, in `[requires.<arch>]` and the like.
- `[runtime]` and `[iso]`, by architecture: the upstream packages published, and those of the tools making the ISO.
- `[pending.<pkg>]`: for each of our packages, the upstream source packages of its build root updated since its
  release, with the versions it was built with.

**Build roots, resolved.** Before building, dnf resolves the build environment image, from the base image and
`packages` of `xcraft.toml`, and the build root of each package, from its `BuildRequires`, against the upstream
repositories, in the locked versions, and a repository of the metadata of our binary packages, from their specs and
what their builds found, without installing anything. It only reads metadata, so it runs natively, for all the
architectures, in the base image of the host, with the specs parsed for each architecture, like `rpmbuild --target`:
the keys of all the architectures are known on any machine, before building there. Upstream source packages needed
but not locked yet get locked, at the latest version all the architectures have, and the lock is committed with the
change of the spec; with `--locked`, for CI, it fails instead. See also [the design](docs/lock-design.md).

**Checked builds.** A build only gets the builds of its dependencies. It records what only a build can know: its
dynamic `BuildRequires`, before installing them, and what rpm generated for its binary packages, which a build fetched
from the remote storage records too. It then checks that dnf installed the build root resolved for it, with what it
recorded, and that it made the binary packages parsed from its spec for the architecture, with the provides and
requirements the spec declares. A build of a package needing one of ours it was not built after is planned again. With
`--locked`, a build recording something fails.

**Updates.** The lock only changes otherwise with `xcraft lock --update`, which takes the latest version all the
architectures have of all the locked source packages, or of some of them, given by binary or source package name, to
get updates and security fixes: when the updates of an architecture are late, the others wait for it. It looks for
them natively, on any machine. `xcraft lock --outdated` shows what it would change, without changing it, and fails
then: a scheduled CI job can run it to notify of the updates, or run the update and propose it, like the one of
xcp-ng-dist.

Since the cache key of a package covers the locked versions of its build root, an update rebuilds the packages whose
build root has an updated package, and the packages built against them.

A rebuild is not a release: rebuilt packages keep their version-release, so hosts keep the one published. For each
update, the packages with the updated package in their build root, or in the image, get pending entries, which
`xcraft impacts` shows, grouped by upstream source package, like glibc for glibc-devel and glibc-common, with whether
each package builds with the current lock; for an update of the image, which impacts all the packages, only their
count. They stay until decided: `xcraft bump`, when the update requires shipping a rebuild (static linking, OCaml
module hashes, soname changes...), or `xcraft ack`, when it does not, like most ABI compatible updates, with the reason
in the commit. Both work on packages, or on all the packages with a pending update of an upstream package, binary or
source name, with `--dep`.

**Limits.** What rpm generates for a package of ours, and its dynamic `BuildRequires`, are only known after its first
build: until then, the keys of the packages built against it miss them, and that build corrects them. A file of ours a
spec requires is only known once the package installing it is built: until then, the build roots needing it cannot be
resolved, which their builds say.

**Checking.** `xcraft repo` runs `dnf repoclosure` on the built packages, with all their requirements, generated
ones included, against the local repository and the build environment's ones: it reports what nothing provides.

## Development

```bash
make install    # the dependencies, with the dev ones, and the pre-commit hooks
make check      # ruff, ruff format, ty, typos and zizmor, like the GitHub workflows, without changing anything
make fix        # ruff's fixes, like the order of the imports, the formatting, and the typos
make test       # pytest, in parallel; the build tests need podman, and the network the first time, for the image
```

Each check also runs alone, by the id of its hook in `.pre-commit-config.yaml`, like `make ty`. The workflows also
run for the pull requests of forks. The tests run in parallel with pytest-xdist, each worker with its own Garage
server for the tests of the remote storage: `pytest -n0` runs them in one process, like to debug one with `--pdb`.
`pytest -m 'not podman'` runs the tests which need no container, in seconds: the others have the `podman` marker.
The tests have upstream packages of their own too, built once from the specs of `tests/upstream/` into a repository on
this machine, `tests/upstream/repos/`, with versions to update to, some for one architecture only.
