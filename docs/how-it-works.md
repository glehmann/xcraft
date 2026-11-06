# How it works

**Project.** Like git, xcraft works on the project of the current directory: the first of it and its parents with an
`xcraft.toml`. If none is found, it searches the parents of the xcraft executable, so an installation in a project's
virtualenv can find that project from elsewhere. `XCRAFT_PROJECT` can point to another one, like in the tests.
XCP-ng's is xcp-ng-dist, which pins the version of xcraft it uses: run `uv sync` there, then `uv run xcraft`, or
`xcraft` in its activated `.venv`.

**Packages.** The packages are grouped by the repository they are published in: `<repo>/<pkg>/`, like `base/xen/`,
for the `repositories` of `xcraft.toml`, `["base"]` by default. Their names are unique across the repositories. Each
package directory holds a spec and its sources, except the source archives: they are listed with their SHA-256 in
`<repo>/<pkg>/sources`, and stored in `.xcraft/cache/sources/<sha256>/<filename>`. Packages come into the project
with their history, see [Import](import.md). The spec of a package is `<pkg>.spec`, or its only spec; other specs are
sources of the main one.

**Metadata.** All specs are parsed by rpm in the build environment, so that macros and conditionals are evaluated as
in a build. The result is cached until a package directory or the image changes.

**Dependencies**: see [Dependencies](dependencies.md).

**Build environment.** xcraft builds a container image from a base image pinned by digest, AlmaLinux 10 for XCP-ng,
with only the configured repositories, AlmaLinux 10 and its EPEL for XCP-ng, the configured RPM macros (`%dist`...)
and the packages builds expect, with those xcraft uses, in their locked versions. The image is tagged with a hash of its definition, so it is only
built again when the configuration or the locked versions of its packages change.

**Builds.** Each build runs in a fresh container: `dnf builddep`, then the dynamic `BuildRequires` if any, then
`rpmbuild`, with stdin from `/dev/null`, and without the network, like koji and mock: only the loopback interface. It runs against a local repository of what was built here, with a priority
over the other repositories, so a package built locally always wins. A build result is cached in
`.xcraft/cache/builds/<arch>/<pkg>/<key>`, under its [build key](builds.md#build-keys). A build whose log gets larger than 256 MiB,
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
packages = ["rpm-build", "redhat-rpm-config", ...]   # in the image, with what xcraft uses: what specs may assume
same_version = [["kernel", "kernel-headers"]]   # updated together by xcraft bump --version
main_branch = "master"                 # the branch the build IDs are numbered against, master by default
[ccache_size]                          # the maximum size of the compiler cache of packages, 5G by default
kernel = "20G"
[macros]                               # RPM macros of the build environment
dist = "%{?autorev}.xcpng8.99"
[repos.baseos]                         # the only repositories of the build environment, {arch} being its RPM arch
baseurl = "https://repo.almalinux.org/almalinux/10/BaseOS/{arch}/os/"   # or file:///..., mounted in the containers
gpgkey = "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10"
includepkgs = ["foo"]                  # optional: the only packages taken from this repository
arches = ["x86_64_v2"]                 # optional: the only architectures it is for, like AlmaLinux's EPEL
[extra_upstream]                       # RPMs not available in these repositories, by URL, of their SHA-256
foo = { url = "https://.../foo-1.0-1.noarch.rpm", sha256 = "..." }
```

The settings of XCP-ng are in the `xcraft.toml` of xcp-ng-dist, and those of the tests in `tests/project/xcraft.toml`,
the project they copy.

**Architectures.** xcraft works on one platform at a time: the one of the RPM architecture given with `--arch`, like
`aarch64`, or in `$XCRAFT_ARCH`, or else the host's, `x86_64_v2` on an x86_64 host. So `xcraft build foo` builds `foo`
for one architecture only. Another architecture is built under emulation, with podman and qemu-user-static, whose
registration needs the `C` flag for `sudo` in the containers, which Fedora's has not: `xcraft doctor` checks it, and
`xcraft doctor --fix` installs the emulator and registers it in `/etc/binfmt.d/`, with systemd-binfmt, or else directly. The emulation is fine for the small packages, but their builds
keep the network: qemu-user cannot isolate them, while the large ones are for native builders, which share their builds
through the [remote storage](remote-storage.md). xcraft solves the metadata of the repositories of an emulated
architecture natively, in its dnf cache: under emulation, that takes most of the time of dnf. Each architecture has its
lock, its build environment image, and its builds, in `.xcraft/cache/builds/<arch>/`. AlmaLinux rebuilds EPEL for
`x86_64_v2` only: the other architectures get Fedora's.
