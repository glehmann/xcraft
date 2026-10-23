# xcraft demo

A tour of the main features, with packages fast to build.

## Before the demo

Everything is built first, ~1h30, `kernel` alone ~15 minutes, and shared like CI does. The steps rebuild only small
packages, after cleaning them. They run in xcp-ng-dist, next to xcraft, with the xcraft it pins.

```
cd ../xcp-ng-dist
uv sync && source .venv/bin/activate.fish     # xcraft, in the version xcp-ng-dist pins; in bash: .venv/bin/activate
../xcraft/local-s3.sh up         # the local remote storage
../xcraft/local-s3.sh env | source  # its credentials, to push; in bash: eval "$(../xcraft/local-s3.sh env)"
xcraft doctor                    # what the host needs: --fix for the emulation of aarch64
xcraft build
xcraft push                      # the builds and the sources, like CI
xcraft key show                  # or xcraft key create: the developer's key, for step 12
xcraft build libempserver --arch aarch64     # for step 11: the aarch64 image and kernel-headers, under emulation
```

## 1. What it is

One tool, xcraft, and one repository, xcp-ng-dist, to build the whole XCP-ng distribution:

- **The packages are in the repository**: each `base/<pkg>/` has the spec, its patches and small sources, imported
  from the xcp-ng-rpms repositories, most with their history. The source archives are not in git: they are listed with
  their sha256, and shared through a remote storage.
- **A reproducible build environment**: a podman container made only of AlmaLinux 10 and EPEL packages, from a base
  image pinned by digest, with the RPM macros of XCP-ng. Each build runs in a fresh one.
- **A committed lock**: the exact version of each upstream package, in the image and in the build root of each of our
  packages. An AlmaLinux update changes nothing until the lock is updated, on purpose.
- **Our packages build against each other**: a local repository of our builds wins over upstream, so a change in a
  package is used by those built against it.
- **Builds are cached by content**: the key covers the package, the keys of the packages it needs, the locked upstream
  packages and the build environment. Nothing is rebuilt for nothing, and a build of CI is reused as is everywhere. A
  change to how xcraft builds which makes the same packages, like a fix of how it fails, can keep the builds.

```
xcraft --help                    # the commands, by what they are about
xcraft doctor                    # what xcraft needs on the host, and how to get what it misses
ls base/ && ls base/xen/         # the packages, by the repository they are published in
cat base/xen/sources             # archives listed by sha256, not stored in git
cat xcraft.toml                  # the settings: the build environment, the remote storage...
grep -A8 '^"biosdevname" = \[' xcraft.x86_64_v2.lock  # the upstream packages its build installs
grep '^"pciutils-devel" = ' xcraft.x86_64_v2.lock      # their version, the same for the whole tree
```

## 2. The history of the packages

Each package comes with the history of its packaging repository, rewritten for the project, the same way each time:
`xcraft import` writes it in the git branches `import/<pkg>/master` and `import/<pkg>/XS-9.0`, and a merge brings it.
Only git is used.

```
git log --first-parent --oneline            # the distribution: a merge for each package; jj log -r 'first_ancestors(@)'
git log --oneline base/libempserver         # its history, from xcp-ng-rpms, with the source RPMs of XenServer
git log -1 import/libempserver/XS-9.0       # the trailer Imported-from: where each commit comes from
git branch --list 'import/*'                # master and XS-9.0, of each package
xcraft import libempserver                  # again: the same commits, and the new ones on top, to merge
```

A new source RPM of XenServer becomes a new commit of `import/<pkg>/XS-9.0`, with `xcraft import <srpm>`, a path or
a URL, without the files to remove for legal reasons, and a merge brings it too.

## 3. Status at a glance

```
xcraft status
xcraft status xen qemu
```

## 4. Dependencies come from the specs

```
xcraft graph qemu                # every package depends on kernel, because of kernel-headers
xcraft graph branding-xcp-ng --dependents
xcraft graph xen --upstream --flat
```

## 5. Builds and their cache

```
xcraft clean branding-xcp-ng xcp-ng-release --builds
xcraft build xcp-ng-release --dry-run       # what would be built, and how long it took last time
xcraft build xcp-ng-release --no-fetch      # branding-xcp-ng (~2s), then xcp-ng-release (~3s)
xcraft build xcp-ng-release                 # again: everything cached
```

## 6. A change rebuilds only what it impacts

```
xcraft bump branding-xcp-ng -m "just a demo"
xcraft build --dry-run                      # branding-xcp-ng and the packages built against it
xcraft build xcp-ng-release                 # only what xcp-ng-release needs
git restore base/branding-xcp-ng            # back to the cached builds
```

## 7. Shared builds through remote storage

```
xcraft clean python-bitarray --builds
xcraft build python-bitarray --no-fetch     # built, ~6s
xcraft push builds python-bitarray          # shared, like CI does
xcraft push sources python-bitarray         # the archives, which a developer shares with their change
xcraft clean python-bitarray --builds       # like a fresh checkout
xcraft build python-bitarray --dry-run      # fetch python-bitarray
xcraft build python-bitarray                # fetched, not built
xcraft clean python-bitarray --builds
xcraft --debug fetch builds python-bitarray     # what xcraft runs, downloads and uploads
```

`xcraft fetch` gets what the caches miss: the current builds, and the source archives, from the storage, or else
from the `Source0` of the specs. The compiler caches are shared too: CI pushes them with `xcraft push ccache`, and a
build without the one of its package gets it first, for a rebuild of a modified package, like the kernel with a patch,
to find most of its objects.

The storage, read publicly over http, with a page for each directory:

- <http://xcraft.web.garage.localhost:3902/>: `builds/`, `sources/`, and `repos/` once published
- <http://xcraft.web.garage.localhost:3902/builds/x86_64_v2/python-bitarray/>: a directory per build key; each has
  `RPMS/`, `SRPMS/`, `build.log`, and `manifest.json`, the checksums of its files
- <http://xcraft.web.garage.localhost:3902/builds/x86_64_v2/kernel/>: pushed before the demo
- <http://xcraft.web.garage.localhost:3902/sources/>: a directory per archive, by the sha256 of its `sources` manifest

## 8. Inspecting and debugging a build

```
xcraft clean xcp-python-libs --builds
xcraft build xcp-python-libs --no-fetch     # ~4s: its errors and warnings shown, under the package
xcraft log xcp-python-libs --grep 'error:|warning:'     # the same, in its whole log
xcraft clean libempserver --builds
xcraft build libempserver --no-fetch -v     # the whole log, as it goes
xcraft log python-bitarray --step build --grep test
xcraft shell libempserver        # in its build tree, which is .xcraft/build/libempserver/ on the host
rpmbuild -ba SPECS/libempserver.spec    # in the shell, as it shows: built like by xcraft, without the network
```

The builds run without the network, like koji and mock: only the loopback interface, for the test suites. A build only
uses what it is given.

## 9. Test builds

```
xcraft build libempserver --test demo       # 0.demo.1: above the release, below the next one
xcraft build libempserver --test demo       # 0.demo.2
xcraft status libempserver                  # with its test builds
xcraft repo libempserver --test demo        # repo-demo/, with a .repo file for the test hosts
```

With a test host: `--host root@testhost --upgrade`.

## 10. Upstream updates under control

```
xcraft lock
xcraft impacts
xcraft bump python-bitarray -m "Rebuild"    # or: xcraft ack python-bitarray
git restore base/python-bitarray            # not to rebuild sm and xapi for step 12
```

A new upstream version, with the archive downloaded from the `Source0` of the spec:

```
xcraft bump xcp-ng-xapi-plugins --version 1.16.0       # changelog entry: Update to 1.16.0
git diff base/xcp-ng-xapi-plugins           # Version, Release, changelog, and the new archive in sources
xcraft build xcp-ng-xapi-plugins            # ~3s, with its patches on the new version
git restore base/xcp-ng-xapi-plugins
```

`kernel` and `kernel-headers` are of the same archive: `same_version` in `xcraft.toml` updates them together.

## 11. Other architectures

xcraft works on one architecture at a time: the host's by default, another one with `--arch` or `XCRAFT_ARCH`.
Another one builds under emulation, fine for the small packages: the large ones are built by native builders, which
share them through the storage. Before the demo, build libempserver once for aarch64: it needs kernel-headers, ~15min
under emulation.

```
xcraft status --arch aarch64
xcraft clean libempserver --builds --arch aarch64
xcraft build libempserver --arch aarch64 --no-fetch     # ~2min, emulated, with the network: qemu-user cannot isolate it
rpm -qp --qf '%{name} %{arch}\n' .xcraft/cache/builds/aarch64/libempserver/*/RPMS/aarch64/*.rpm
```

## 12. Publishing a signed snapshot

A snapshot is a self-contained repository of all our packages and the upstream ones they need, as locked, for all the
architectures, laid out like the repositories of XCP-ng 8.3, with the sources next to the binaries, and a build
repository with what rebuilding them needs. Ours are signed with the release key of `xcraft.toml`, possibly in an HSM
through gpg, or the developer's key for a test.

```
xcraft key show                             # the developer's key
xcraft publish --local                      # in .xcraft/publish/, of this architecture, with the developer's key
ls .xcraft/publish/ .xcraft/publish/*/      # base/ and build/, with <arch>/ and Source/, and RPM-GPG-KEY
rpm -qpi .xcraft/publish/base/x86_64_v2/xen-4*.rpm | grep Signature     # ours: the developer's key
rpm -qpi .xcraft/publish/base/x86_64_v2/glibc-2*.rpm | grep Signature   # upstream: AlmaLinux's, as is
xcraft publish --dev-key --arch x86_64_v2   # to the storage, as the latest snapshot: all the architectures by
                                            # default, but aarch64 has no native builder yet
```

- <http://xcraft.web.garage.localhost:3902/repos/9.0/>: a directory per snapshot, by date, and `latest/`
- <http://xcraft.web.garage.localhost:3902/repos/9.0/latest/base/>: `x86_64_v2/` and `Source/`, like in XCP-ng 8.3
- <http://xcraft.web.garage.localhost:3902/repos/9.0/latest/base/x86_64_v2/repodata/>: `repomd.xml`, and its
  signature `repomd.xml.asc`
- <http://xcraft.web.garage.localhost:3902/signed/>: our builds signed by each key, once, shared by the snapshots
