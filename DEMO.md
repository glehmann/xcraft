# xcraft demo

A tour of the main features, with packages fast to build.

## Before the demo

Everything is built first, ~1h30, `kernel` alone ~35 minutes, and shared like CI does. The steps rebuild only small
packages, after cleaning them.

```
./local-s3.sh up                 # the local remote storage
./local-s3.sh env | source       # its credentials, to push; in bash: eval "$(./local-s3.sh env)"
xcraft build
xcraft push builds
xcraft key show                  # or xcraft key create: the developer's key, for step 10
```

## 1. What it is

One tool, and one repository, to build the whole XCP-ng distribution:

- **The packages are in the repository**: each `specs/<pkg>/` has the spec, its patches and small sources, imported
  from the xcp-ng-rpms repositories. The source archives are not in git: they are listed with their sha256, and
  shared through a remote storage.
- **A reproducible build environment**: a podman container made only of AlmaLinux 10 and EPEL packages, from a base
  image pinned by digest, with the RPM macros of XCP-ng. Each build runs in a fresh one.
- **A committed lock**: the exact version of each upstream package, in the image and in the build root of each of our
  packages. An AlmaLinux update changes nothing until the lock is updated, on purpose.
- **Our packages build against each other**: a local repository of our builds wins over upstream, so a change in a
  package is used by those built against it.
- **Builds are cached by content**: the key covers the package, the keys of the packages it needs, the locked upstream
  packages and the build environment. Nothing is rebuilt for nothing, and a build of CI is reused as is everywhere.

```
xcraft --help
ls specs/ && ls specs/xen/
cat specs/xen/sources            # archives listed by sha256, not stored in git
cat xcraft.toml                  # the settings, defaults in xcraft/project.py
grep -A8 '^"biosdevname" = \[' locks/x86_64_v2.toml    # the upstream packages its build installs
grep '^"pciutils-devel" = ' locks/x86_64_v2.toml        # their version, the same for the whole tree
```

## 2. Status at a glance

```
xcraft status
xcraft status xen qemu
```

## 3. Dependencies come from the specs

```
xcraft graph qemu                # every package depends on kernel, because of kernel-headers
xcraft graph branding-xcp-ng --dependents
xcraft graph xen --upstream --flat
```

## 4. Builds and their cache

```
xcraft clean branding-xcp-ng xcp-ng-release --builds
xcraft build xcp-ng-release --dry-run       # what would be built, and how long it took last time
xcraft build xcp-ng-release --no-fetch      # branding-xcp-ng (~2s), then xcp-ng-release (~3s)
xcraft build xcp-ng-release                 # again: everything cached
```

## 5. A change rebuilds only what it impacts

```
xcraft bump branding-xcp-ng -m "just a demo"
xcraft build --dry-run                      # branding-xcp-ng and the packages built against it
xcraft build xcp-ng-release                 # only what xcp-ng-release needs
jj restore specs/branding-xcp-ng            # back to the cached builds
```

## 6. Shared builds through remote storage

```
xcraft clean python-bitarray --builds
xcraft build python-bitarray --no-fetch     # built, ~6s
xcraft push builds python-bitarray          # shared, like CI does
xcraft push sources python-bitarray         # the archives, which a developer shares with their change
xcraft clean python-bitarray --builds       # like a fresh checkout
xcraft build python-bitarray --dry-run      # fetch python-bitarray
xcraft build python-bitarray                # fetched, not built
```

The storage, read publicly over http, with a page for each directory:

- <http://xcraft.web.garage.localhost:3902/>: `builds/`, `sources/`, and `repos/` once published
- <http://xcraft.web.garage.localhost:3902/builds/x86_64_v2/python-bitarray/>: a directory per build key; each has
  `RPMS/`, `SRPMS/`, `build.log`, and `manifest.json`, the checksums of its files
- <http://xcraft.web.garage.localhost:3902/builds/x86_64_v2/kernel/>: pushed before the demo
- <http://xcraft.web.garage.localhost:3902/sources/b7564fd218cc4479f7f0106d341e096f78907b47865aeeff702c807df1927c01/>:
  the archive of `specs/python-bitarray/sources`, by its sha256

## 7. Inspecting and debugging a build

```
xcraft clean xcp-python-libs --builds
xcraft build xcp-python-libs --no-fetch     # ~4s: its errors and warnings shown, under the package
xcraft log xcp-python-libs --grep 'error:|warning:'     # the same, in its whole log
xcraft clean libempserver --builds
xcraft build libempserver --no-fetch -v     # the whole log, as it goes
xcraft log python-bitarray --step build --grep test
xcraft shell libempserver        # shell in the build environment, sources prepared
```

## 8. Test builds

```
xcraft build libempserver --test demo       # 0.demo.1: above the release, below the next one
xcraft build libempserver --test demo       # 0.demo.2
xcraft status libempserver                  # with its test builds
xcraft repo libempserver --test demo        # repo-demo/, with a .repo file for the test hosts
```

With a test host: `--host root@testhost --upgrade`.

## 9. Upstream updates under control

```
xcraft lock
xcraft impacts
xcraft bump python-bitarray -m "Rebuild"    # or: xcraft ack python-bitarray
jj restore specs/python-bitarray            # not to rebuild sm and xapi for step 10
```

A new upstream version, with the archive downloaded from the `Source0` of the spec:

```
xcraft bump xcp-ng-xapi-plugins --version 1.16.0       # changelog entry: Update to 1.16.0
jj diff --git specs/xcp-ng-xapi-plugins     # Version, Release, changelog, and the new archive in sources
xcraft build xcp-ng-xapi-plugins            # ~3s, with its patches on the new version
jj restore specs/xcp-ng-xapi-plugins
```

`kernel` and `kernel-headers` are of the same archive: `same_version` in `xcraft.toml` updates them together.

## 10. Publishing a signed snapshot

A snapshot is a self-contained repository of all our packages and the upstream ones they need, as locked. Ours are
signed with the release key of `xcraft.toml`, possibly in an HSM through gpg, or the developer's key for a test.

```
xcraft key show                             # the developer's key
xcraft publish --local                      # in .xcraft/publish/x86_64_v2/, signed with the developer's key
ls .xcraft/publish/x86_64_v2/               # os/, source/, and RPM-GPG-KEY, the public key
rpm -qpi .xcraft/publish/x86_64_v2/os/xen-4*.rpm | grep Signature     # ours: the developer's key
rpm -qpi .xcraft/publish/x86_64_v2/os/glibc-2*.rpm | grep Signature   # upstream: AlmaLinux's, as is
xcraft publish --dev-key                    # to the storage, as the latest snapshot
```

- <http://xcraft.web.garage.localhost:3902/repos/9.0/>: a directory per snapshot, by date, and `latest/`
- <http://xcraft.web.garage.localhost:3902/repos/9.0/latest/x86_64_v2/os/repodata/>: `repomd.xml`, and its signature
  `repomd.xml.asc`
- <http://xcraft.web.garage.localhost:3902/signed/>: our builds signed by each key, once, shared by the snapshots
