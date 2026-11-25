# Design: a minimal build root

Status: proposed. mock and the package sets below were tried by hand on 2026-11-25; nothing is implemented in xcraft.

## Context and goals

The builds run in the build environment image: AlmaLinux's base image, the `packages` of `xcraft.toml`, mock's
build root list, and what xcraft itself uses in the containers, then the `BuildRequires` that `dnf builddep` installs.
So a build root also has dnf, Python, sudo, createrepo_c and what they pull: 192 packages before the
`BuildRequires`, where mock's has 145.

That has two costs:

- **Hidden build dependencies.** A spec using a command of the image without its `BuildRequires`, like `python3`, or
  a macro of `systemd-rpm-macros`, builds with xcraft, but not elsewhere, in mock or koji, and not with xcraft once
  the image loses it.
- **Needless rebuilds.** The keys of all the builds cover all the packages of the image, see build-keys.md: an update
  of dnf, Python, curl or sudo, which make nothing of the builds, builds everything again.

Goals:

- Build in a root of the packages that rpmbuild needs and the `BuildRequires` only, like mock's, or smaller.
- Keep the tools of xcraft, dnf and Python in particular, out of that root, and out of the keys.
- Keep what xcraft's builds have: the pinned packages only, no network, no privilege gained by the build, the
  compiler caches, `xcraft shell`, the development builds from trees, the emulated architectures, and their speed.
- Run in the rootless containers of today, with no capability added.

Non-goals:

- Builds identical to Fedora's or koji's: the same root makes the same specs build, which is enough.
- Removing from the keys the root packages that cannot change a build, like `ca-certificates`: a judgment on each
  package, for little gain once the tools are out.

## Today

`xcraft/buildenv/Containerfile` installs the `packages` of `xcraft.toml` and `container.PACKAGES`
(`dnf-plugins-core`, `createrepo_c`, `sudo`) on the base image; `build.sh` runs `dnf builddep`, the loop of the
dynamic `BuildRequires`, `record.py`, and `rpmbuild` without gaining privileges, in a container without the network
but its loopback interface.

The image of XCP-ng against the root of mock's `almalinux-10-x86_64_v2` configuration, both from the same pinned
repository, so with the same versions:

| | Packages |
| --- | --- |
| xcraft's image | 192 |
| mock's root | 145 |
| in both | 135 |

Only in xcraft's image, 57:

- dnf and its Python, which xcraft needs: `dnf`, `yum`, `libdnf`, `librepo`, `libsolv`, `libcomps`, `libmodulemd`,
  `libyaml`, `python3`, `python3-libs`, the `python3-*` bindings of dnf, rpm, dbus and systemd, `mpdecimal`,
  `dnf-plugins-core`, `createrepo_c`, `sudo`;
- what they pull: `systemd`, `systemd-pam`, `dbus*`, `glib2`, `gnutls`, `libcurl-minimal`, `tpm2-tss`,
  `ima-evm-utils`, `rpm-sign-libs`, `libseccomp`, `libusb1`, `expat`, `usermode`, `libuser`;
- from the base image: `vim-minimal`, `tzdata`, `rootfiles`, `virt-what`, `dmidecode`, `attr`, `keyutils`,
  `gdb-gdbserver`, `rpm-plugin-syslog`;
- `systemd-rpm-macros`, the one that matters to the specs: `%{_unitdir}` or `%systemd_post` without a
  `BuildRequires` on it build with xcraft only.

Only in mock's root, 10: the full `coreutils` rather than `coreutils-single`, and the libraries of the full `curl`.

xcraft also runs Python in the builds themselves: `macros.archives` runs `/usr/bin/python3 /xcraft/archives.py` at
the end of every `%install`, and the `%prep` of a development build from a tree runs `python3 /xcraft/develop.py`.

## How small the root can be

`rpm-build` and `redhat-rpm-config` already need most of mock's list: the root is mostly their dependencies. `rpm`
requires `curl`, whose full libcurl pulls `openldap`, `cyrus-sasl`, `libssh` and `libfido2`; `redhat-rpm-config`
requires a dozen `*-srpm-macros`; mock's own list adds `util-linux`, which pulls `pam`, `authselect` and
`libpwquality`, and `which` and `info`. Resolved against the pinned repository:

| Root | Packages | Installed |
| --- | --- | --- |
| mock's list | 145 | 189 MB |
| mock's list, with `coreutils-single`, `util-linux-core` and `libcurl-minimal` | 123 | 152 MB |
| `rpm-build`, `redhat-rpm-config` and `glibc-minimal-langpack` only | 128 | 169 MB |
| the same, with `coreutils-single` and `libcurl-minimal` | 112 | 143 MB |

The second row is the proposed root, without `shadow-utils`, which only mock needs, to add its build user. The 22
packages it drops are mostly libraries without their headers, which no build can use; they are also those with
frequent security updates, like `pam`, `openldap` and `libssh`, so the rebuilds they would cause. `util-linux-core`
lacks some commands of `util-linux`, which their specs would then require. `which` and `info` stay: specs written for
Fedora and EL assume them, for two small packages.

## Considered: mock in the container

mock makes such a root for each build, and was tried in the container, from the pinned repository.

**Speed.** Its root comes from a cache it unpacks in about 1 s, then a dnf update of 0.6 to 0.8 s, and a clean of 0.3
to 1.4 s after the build: 2.5 to 3.5 s more by build. It also makes a source package first, 3.8 s for the kernel. The
builds took about as long as `xcraft reproduce`, which builds without compiler caches too:

| Package | xcraft | mock |
| --- | --- | --- |
| dmv-utils | 3 s | 3.2 s |
| xcp-ng-config | 3 s | 2.9 s |
| biosdevname | 14 s | 12.2 s |
| sm, with `%check` | 36 s | 34.9 s |
| xen | 3 min 42 s | 3 min 18 s |

xcraft's times include its own steps, and xen's difference is noise.

**Privileges.** With the default capabilities of podman, mock fails: it unshares a mount namespace. It works with
`--cap-add SYS_ADMIN`, or with no capability added in a nested user namespace mapping the ids of the container,
but not through `/usr/bin/mock`, a setuid wrapper, and only with `SUDO_UID` set, so that it has a
user to build as. With `isolation=simple`, mock leaves the network to the builds: only its systemd-nspawn isolation
removes it, so it relies on `--network none`. Run as root without `chrootuid`, it builds as root, with all the
capabilities.

**Fit.** It has its own configuration to generate from `xcraft.toml`, its own logs, its own handling of the dynamic
`BuildRequires`, of the compiler caches and of a shell, and a version to lock. What it brings, the root and its
isolation, comes from techniques xcraft can use directly: see below.

So xcraft does not use mock, not even to find the specs missing `BuildRequires` beforehand: building all the
packages in xcraft's own root finds them, in the root they will build in, rather than in mock's, which differs.

## Design: the root as a chroot of the image

### The image

The image keeps the base image and the tools of xcraft, and gets the build root in `/buildroot`, as one more layer:

- `dnf --installroot=/buildroot --setopt=install_weak_deps=False install` of the `packages` of `xcraft.toml`, from
  the pinned repository only, given on the command line rather than read from the root;
- in the root, what xcraft puts in the image today: `macros.xcraft`, `undefined.list` and the `rpmbuild` wrapper;
- the files a root needs: the `builder` user in `/etc/passwd` and `/etc/group`, `/etc/hosts` with `localhost`, which
  test suites need, and the name of the container, written by the helper, and the directories mounted in it:
  `/build`, `/ccache`, but not `/pinned`, as dnf runs outside.

Each container then starts from a fresh copy of the root, by the copy-on-write of the image: nothing to unpack nor
clean, unlike mock.

The `packages` of xcp-ng-dist become those of the root: mock's list, with `coreutils-single`, `util-linux-core` and
`libcurl-minimal` rather than `coreutils`, `util-linux` and `curl`'s libcurl.

### A build

`build.sh` keeps its steps, run from the tools on the root:

1. The static `BuildRequires`: `rpmbuild -bs`, in the root, writes the source package, without running `%prep`, and
   `dnf --installroot=/buildroot builddep` installs what it requires, from the tools. The spec is so read with the
   macros of the root, where `dnf builddep` of the spec, as today, would read it with those of the tools.
2. The dynamic ones, of the specs with `%generate_buildrequires` only, as today: `rpmbuild -br` in the root, then
   `builddep` of the source package it wrote, until it succeeds. `rpmbuild -br` runs `%prep`: a loop of it for all
   the specs would run the `%prep` of all of them twice, 30 s more for the kernel.
3. Our packages replacing upstream ones of the root, and ccache with the compilers, are installed in the root
   the same way.
4. `record.py` records the packages of the root: its dnf API opened on `/buildroot`.
5. `rpmbuild` runs in the root, see below.
6. `made.py` reads the packages in `/build/RPMS`, from the tools, as today.

dnf, Python and the scripts of xcraft never enter the root.

### Isolation

The container has no network but its loopback interface, up, as today: the root needs nothing more for that. To
mount in the root what it needs, a helper makes a user namespace mapping the ids of the container, as root in it,
and a mount namespace, mounts in the root `/proc`, `/sys`, `/dev`, `/dev/pts`, a `tmpfs` on `/dev/shm`,
and `/build`, `/ccache` and the dune cache at the same paths, then `chroot`s, drops to the user and to no new
privileges, and runs the command. These are the mounts mock made in the same namespace, with no capability added to
the container; a mount of a new `/proc` in a pid namespace is refused there, as podman masks some of its files, so
`/proc` is bound from the container's.

### Python in the builds

- `archives.py` becomes a Lua macro in `macros.archives`: rpm's Lua has `io` and `posix`, enough to rewrite the
  member headers of the archives, as `archives.py` does without `ar` or `objcopy`, which fail on the archives of
  objects that are not ELF.
- `develop.py` runs from the tools before `rpmbuild`, on the directory of the sources kept from the previous build,
  in `/build`; the `%prep` of the development tree no longer runs it.

### Spec parsing

`parse.py` parses the specs with the rpm Python API, so in the tools, with their macros: those of
`systemd-rpm-macros` then expand there, but not in the builds. It must use the macro files of the root instead: an
rpmrc whose `macrofiles` are those of `/buildroot`, which rpm's API loads with `rpm.reloadConfig()`. The `%(...)`
and Lua of the macros run in the tools then, with the same rpm and coreutils as the root, both pinned.

### Lock and keys

- `[image]` of `xcraft.lock` splits: the packages of the root, which the resolution starts each build root from, and
  those of the tools, which it does not see.
- The keys cover the packages of the root and of the build roots only, and no longer the base image, its digest, or
  `container.PACKAGES`, see `recipe()` and `lock_inputs()` in `buildkey.py`: an update of dnf, Python or the base
  image builds nothing again.
- `xcraft graph --upstream` shows the root rather than the image.

### Compiler caches and development builds

They mount in the root what they mount in the container today.

### The shell

A developer updating a package to a new version, which needs other packages, installs them in the shell, a step at a
time, and builds, to check the new dependencies as they go. The root has neither dnf nor sudo, so the shell stays in
the container, on the side of the tools, as the builder, with sudo, in the build tree, with the network. Commands of
its `PATH`, scripts rather than aliases, run the usual ones on the root:

- `dnf` runs `sudo dnf --installroot=/buildroot`, with the pinned repository, ours, and the upstream repositories of
  the project, disabled: `dnf install foo` installs a pinned package, or ours, in the root the build uses, and
  `dnf --enablerepo='*' install foo` one not pinned yet, as the shell already does without the root, see
  `xcraft shell`; `dnf builddep SPECS/<spec>` reads the spec with the macros of the root, like the first step of a
  build.
- `rpmbuild` runs the one of the root, through the helper, with the network of the container.
- `root` opens a shell in the root, to run a command as the build sees it.

The developer then edits the version and the `BuildRequires` of the spec in the build tree, runs `dnf builddep` or
`dnf install`, then `rpmbuild -ba`, and again; each install adds to the root of the shell. A package not pinned yet
gets pinned by a `BuildRequires` on it and the next `xcraft shell`, or `xcraft build`.

Against the shell of today:

- better: a build succeeding in the shell succeeds in the builds, where today the `rpmbuild` of the shell sees the
  whole image, so a forgotten `BuildRequires`, like `python3`, fails later, or never;
- the same: what is installed by hand stays in the root of the shell only, and a build starts from a clean root;
- different: the tools of debugging, like `strace` or `gdb`, are installed in the root too, as the build runs there.

### Emulated architectures

Under emulation, the helper cannot make a user namespace, as qemu-user cannot, multithreaded, and a chroot needs its
mounts, so its namespaces. The tools then run natively, like `container.run_natively()` does for the resolution, and
the root only is of the emulated architecture: `dnf --installroot --forcearch`, then `chroot` to its binaries, run by
qemu through binfmt_misc with the `F` flag, as `mock --forcearch` does.

Tried on 2026-11-25, on an x86_64 host whose qemu-aarch64 has the `OCF` flags, in a container of the host's platform
with the options of xcraft's, `--userns=keep-id`, `--network none` and no new privileges, and no capability added:

- `dnf --installroot=/buildroot --forcearch=aarch64` installed the root, 121 packages, from the pinned repository of
  aarch64, in 224 s, their scriptlets run by qemu: once, for the layer of the image;
- `rpmbuild -bs` in the root, then `dnf --installroot builddep` from the tools, installed `gcc`, 13 packages, in
  4.4 s, where dnf under emulation, in a container of aarch64, as the builds install their dependencies today, took
  37.6 s for it;
- the helper, a user namespace mapping the ids of the container and a mount namespace, binding `/proc`, `/sys`, `/dev`
  and `/build` in the root, ran `rpmbuild -bb` there: gcc compiled an aarch64 program, which ran, as uid 1000, without
  capabilities, without new privileges, with the loopback interface only, and neither `python3` nor `dnf` in the root.

The helper must import what it needs before `chroot`: Python found no more of its modules after it. rpm warned that it
could not canonicalize the host name: `/etc/hosts` of the root needs the name of the container too.

### Tests

- `should_fail_the_build_of_a_spec_missing_a_build_requirement`: a toy package of the test project running
  `python3` without requiring it.
- `should_not_build_again_when_only_the_tools_change`: a change of `container.PACKAGES`, or of the base image, keeps
  the keys.
- The tests of the shell, of the development builds and of an emulated architecture, extended to check that the
  build runs in the root.

## Costs, against today

- More code of xcraft: podman makes the whole environment of the builds today. The helper makes the namespaces and the
  mounts of the root, xcraft writes its `/etc/hosts`, user and locale, and the builds will find the cases mock met
  over the years: a test suite needing a terminal, a host name, room in `/dev/shm`.
- The shell gets its commands of the root, see [The shell](#the-shell): the developer works on two systems, the
  container and the root, rather than one.
- The emulated architectures need native tools on a root of the emulated architecture, and qemu registered with the
  `F` flag, which `xcraft doctor` checks: the largest part of the change, see
  [Emulated architectures](#emulated-architectures).
- Two configurations of rpm: the tools parse the specs with the macro files of the root, where one rpm does both
  today. Both are pinned at the same versions, but the macros running commands or Lua run them in the tools.
- A boundary to keep: the tools are out of the keys only while nothing of theirs changes what a build makes. It holds,
  `record.py` records, `parse.py` makes the graph of the dependencies, and `develop.py` runs for the development builds
  only, but a later step on what the builds make, run from the tools, would need the recipe again.
- The specs lose what they used without requiring it: `tzdata`, among the packages leaving, for a test formatting
  dates in a time zone. That is the goal, but work on the specs inherited from upstream.
- A larger image: the tools and the root, two sets of system packages, about 150 MB more.

Not worse: the speed, the root being a layer of the image, cached, and `dnf builddep` the same; the isolation, no
network, a builder without capabilities, without new privileges, and the root read only to it; the compiler caches and
the development builds.

## Migration

The keys of all the packages change: a key-changing change of xcraft, rebuilt as such. xcraft with the root builds
all the packages of xcp-ng-dist, with their tests, before that change is pushed: the builds failing for a missing
`BuildRequires` find the specs to fix, in their own revisions, pushed with it.

## Open questions

- `util-linux-core` and `coreutils-single`: which commands of `util-linux` the specs use, to keep the full one if
  many do.
- Whether `parse.py` with the root's macro files expands the specs as the root's rpm does: its `%{_rpmconfigdir}`
  and the scripts it calls are the tools', of the same pinned versions.
- The binfmt_misc registration of qemu with `F`, which `xcraft doctor` checks only for `C` today: Fedora's has it,
  the others may not.

## Plan

1. The root in the image and the chroot of the helper, for the native architecture, with `archives.py` in Lua and
   `develop.py` outside the root.
2. The parsing of the specs with the root's macros.
3. The split of the lock and of the keys.
4. The emulated architectures, from native tools.
5. All the packages built with the root, the specs missing `BuildRequires` fixed, then the full rebuild.
