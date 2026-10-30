# xcraft demo

A tour of the main features, with packages fast to build.

## Before the demo

CI builds everything on each push of the main branch, on a native runner of each architecture, and shares it through
the test storage, https://91.98.220.14, with a self-signed certificate. A checkout fetches it all in a few minutes
rather than building it, ~1h30. The steps run in xcp-ng-dist, next to xcraft, with the xcraft it pins, and rebuild only
small packages, after cleaning them.

```
cd ../xcp-ng-dist
uv sync && source .venv/bin/activate.fish     # xcraft, in the version xcp-ng-dist pins; in bash: .venv/bin/activate
set -x AWS_ACCESS_KEY_ID …; set -x AWS_SECRET_ACCESS_KEY …  # the key xcraft-dev of the storage: the dev bucket
xcraft doctor                    # what the host needs: --fix installs and configures it
xcraft fetch                     # the builds of CI, and the sources
xcraft key show                  # or xcraft key create: the developer's key, for step 12
```

## 1. What it is

One tool, xcraft, and one repository, xcp-ng-dist, to build the whole XCP-ng distribution:

- **The packages are in the repository**: each `base/<pkg>/` has the spec, its patches and small sources, imported
  from the xcp-ng-rpms repositories, most with their history. The source archives are not in git: they are listed with
  their sha256, and shared through a remote storage.
- **A reproducible build environment**: a podman container made only of AlmaLinux 10 and EPEL packages, from a base
  image pinned by digest, with the RPM macros of XCP-ng. Each build runs in a fresh one.
- **A committed lock**: the exact version of each upstream source package, in the image and in the build root of each
  of our packages, one for all the architectures. The build roots are resolved from the specs and the lock, for every
  architecture, on any machine. An AlmaLinux update changes nothing until the lock is updated, on purpose.
- **Our packages build against each other**: a local repository of our builds wins over upstream, so a change in a
  package is used by those built against it.
- **Builds are cached by content**: the key covers the package, the keys of the packages it needs, the locked upstream
  packages and the build environment. Nothing is rebuilt for nothing, and a build of CI is reused as is everywhere.
- **Build numbers are committed**: each package has a build ID in `.buildid/<pkg>`, a number and the key it was given
  to, in its release as `+b<n>`. A new key gets a new number, so that no number is given to two different builds.

```
xcraft --help                    # the commands, by what they are about
xcraft doctor                    # what xcraft needs on the host, and how to get what it misses
ls -a                            # xcraft.toml, next to the lock and .buildid/
ls base/ && ls base/xen/         # the packages, by the repository they are published in
cat base/xen/sources             # archives listed by sha256, not stored in git
cat xcraft.toml                  # the settings: the build environment, the remote storage...
grep '^"pciutils" = ' xcraft.lock  # an upstream source package, in one version for the whole tree
grep -m1 -A2 '^"biosdevname" = ' xcraft.lock  # what rpm generated for it: it links against pciutils-libs
cat .buildid/xen                 # its number, 1, and its key
xcraft build -n --arch aarch64   # the keys of aarch64, resolved here, without emulation: most built by CI
```

## 2. The history of the packages

Each package comes with the history of its packaging repository, rewritten for the project, the same way each time:
`xcraft import` writes it in the git branches `import/<pkg>/master` and `import/<pkg>/XS-9.0`, and a merge brings it.
Only git is used.

```
git log --first-parent --oneline            # the distribution; jj log -r 'first_ancestors(@)'
git log --oneline base/libempserver         # its history, from xcp-ng-rpms, with the source RPMs of XenServer
git log -1 import/libempserver/XS-9.0       # the trailer Imported-from: where each commit comes from
git branch --list 'import/*'                # master and XS-9.0, of the packages with a history
xcraft import xsconsole                     # one without its history yet: import/xsconsole/master, to merge
git log --oneline import/xsconsole/master | head
git branch -D import/xsconsole/master       # not for now
```

A new source RPM of XenServer becomes a new commit of `import/<pkg>/XS-9.0`, with `xcraft import <srpm>`, a path or
a URL, without the files to remove for legal reasons, and a merge brings it too.

## 3. Status at a glance

```
xcraft status
xcraft status xen qemu                      # with their build numbers, +b1
```

## 4. Dependencies come from the specs

```
xcraft graph qemu                # every package with C code needs kernel-headers
xcraft graph branding-xcp-ng --dependents
xcraft graph xen --upstream --flat
```

## 5. Builds and their cache

```
xcraft clean branding-xcp-ng xcp-ng-release --builds
xcraft build xcp-ng-release --dry-run       # fetched from the storage, rather than built
xcraft build xcp-ng-release --fetch=none    # built: branding-xcp-ng (~3s), then xcp-ng-release (~5s)
xcraft build xcp-ng-release                 # again: everything cached
```

## 6. A change rebuilds only what it impacts

```
xcraft bump branding-xcp-ng -m "just a demo"
xcraft build --dry-run                      # branding-xcp-ng and the packages built against it
xcraft build xcp-ng-release                 # development builds: +b2~1.dev
git diff .buildid                           # their new build IDs: number 2, for their new keys
xcraft status branding-xcp-ng xcp-ng-release xapi       # xapi not built yet
git restore base/branding-xcp-ng .buildid xcraft.lock   # back to the builds of CI
```

A change of a branch gets development builds, `+b<n>~<counter>.dev`: they upgrade the previous ones, and the final
build of the same number, made by CI once merged, upgrades them. Only CI makes final builds, on the main branch.
Another branch giving the same number to another build conflicts on merge, in the file of the package.

## 7. Shared builds through the remote storage

```
xcraft clean python-bitarray --builds       # like a fresh checkout
xcraft build python-bitarray --dry-run      # fetch python-bitarray: built by CI
xcraft build python-bitarray                # fetched, not built
xcraft clean python-bitarray --builds
xcraft --debug fetch builds python-bitarray     # what xcraft runs, downloads and checks
```

`xcraft fetch` gets what the caches miss: the current builds, and the source archives, from the storage, or else
from the URLs of the sources of the specs. The compiler caches are shared too: CI pushes them with `xcraft push
ccache`, and a build without the one of its package gets it first, for a rebuild of a modified package, like the
kernel with a patch, to find most of its objects.

The storage has two buckets. CI writes the main one, with its key; developers write the dev one, with theirs: the
source archives of their changes, `xcraft push sources`, and test snapshots. Each tree is signed by its manifest,
`manifest.asc`, and xcraft checks it against the keys of `xcraft.toml` before downloading anything else. It is read
publicly over https, with a page for each directory:

- <https://91.98.220.14/>: `repos/` and `isos/`, the snapshots CI publishes, and `.xcraft/`, what xcraft shares
- <https://91.98.220.14/.xcraft/builds/x86_64_v2/python-bitarray/>: a directory per build key and number; each has
  `RPMS/`, `SRPMS/`, `build.log`, and `manifest.asc`, the checksums of its files, signed by CI
- <https://91.98.220.14/.xcraft/ccache/x86_64_v2/>: the compiler caches
- <https://91.98.220.14/.xcraft/sources/>: a directory per archive, by its sha256
- <https://91.98.220.14:8443/>: the dev bucket

## 8. Inspecting and debugging a build

```
xcraft clean xcp-python-libs --builds
xcraft build xcp-python-libs --fetch=none   # ~4s: its errors and warnings shown, under the package
xcraft log xcp-python-libs --grep 'error:|warning:'     # the same, in its whole log
xcraft clean libempserver --builds
xcraft build libempserver --fetch=none -v   # the whole log, as it goes
xcraft log python-bitarray --step build --grep test
xcraft shell libempserver        # in its build tree, which is .xcraft/build/libempserver/ on the host
rpmbuild -ba SPECS/libempserver.spec    # in the shell, as it shows: built like by xcraft, without the network
```

The builds run without the network, like koji and mock: only the loopback interface, for the test suites. A build only
uses what it is given.

## 9. Development builds for test hosts

```
xcraft bump python-bitarray -m "just a demo"
xcraft build python-bitarray --dev demo     # ~8s: 2.8.5-13+b2~1.demo, above the CI's builds of 2.8.5-12
xcraft status python-bitarray --dev demo
xcraft repo python-bitarray --dev demo      # repo-demo/, with a .repo file for the test hosts
git restore base/python-bitarray .buildid xcraft.lock && rm -r repo-demo
```

With a test host: `--host root@testhost --upgrade`.

## 10. Upstream updates under control

```
xcraft lock                                 # the lock, for all the architectures
xcraft lock --outdated                      # the updates, of all the architectures, looked for natively
xcraft lock --update sg3_utils              # an AlmaLinux update, of a package only xapi builds with
xcraft build --dry-run                      # xapi and xcp-featured: their build IDs changed
xcraft reuse --dep sg3_utils                # an ABI compatible fix: xapi reuses its build, accepting the change
xcraft build --dry-run                      # nothing to build
git diff .buildid/xapi                      # accepts <key>: say why in the commit
git restore .buildid xcraft.lock            # back to the builds of CI
```

Every day, CI updates the lock, and proposes it in a pull request: see `.github/workflows/lock-update.yml`.

A new upstream version, with the archive downloaded from the `Source0` of the spec:

```
xcraft bump xcp-ng-xapi-plugins --version 1.17.0       # changelog entry: Update to 1.17.0
git diff base/xcp-ng-xapi-plugins           # Version, Release, changelog, and the new archive in sources
xcraft build xcp-ng-xapi-plugins            # ~5s, with its patches on the new version
xcraft push sources xcp-ng-xapi-plugins     # the archive, which a developer shares with their change
git restore base/xcp-ng-xapi-plugins .buildid xcraft.lock
```

`kernel` and `kernel-headers` are of the same archive: `same_version` in `xcraft.toml` updates them together.

## 11. Other architectures

xcraft works on one architecture at a time: the host's by default, another one with `--arch` or `XCRAFT_ARCH`. All of
them share the lock and the build keys, known on any machine, without emulation. CI builds them on native runners, and
shares them through the storage; another architecture builds here under emulation, slowly.

```
xcraft build python-bitarray --arch aarch64     # fetched, with kernel-headers: built by CI on an arm runner
rpm -qp --qf '%{name} %{arch}\n' .xcraft/cache/builds/aarch64/python-bitarray/*/RPMS/aarch64/*.rpm
xcraft clean python-bitarray --builds --arch aarch64
xcraft build python-bitarray --arch aarch64 --fetch=none    # ~7min, emulated, with the network: qemu-user cannot
                                                            # isolate it
```

## 12. Publishing a signed snapshot

A snapshot is a self-contained repository of all our packages and the upstream ones they need, as locked, for all the
architectures, laid out like the repositories of XCP-ng 8.3, with the sources next to the binaries, and a build
repository with what rebuilding them needs, with the installation ISOs. Ours are signed with the key of CI, possibly
in an HSM through gpg, or the developer's key for a test.

```
xcraft key show                             # the developer's key
xcraft publish --local                      # in .xcraft/publish/, of this architecture, with the developer's key
ls .xcraft/publish/ .xcraft/publish/*/      # base/, build/, with <arch>/ and Source/, iso/, and RPM-GPG-KEY
rpm -qpi .xcraft/publish/base/x86_64_v2/xen-hypervisor-4*.rpm | grep Signature  # ours: the developer's key
rpm -qpi .xcraft/publish/base/x86_64_v2/glibc-2*.rpm | grep Signature           # upstream: AlmaLinux's, as is
xcraft publish --stage demo --arch x86_64_v2   # ~4min, to the dev bucket, as this stage's latest snapshot: all
                                               # architectures by default, but aarch64 does not build entirely yet
xcraft publish --stage demo --arch x86_64_v2   # again: ~15s, the same snapshot, copied in the storage
```

- <https://91.98.220.14:8443/repos/9.0/demo/>: a directory per snapshot, by date, and `latest/`
- <https://91.98.220.14:8443/repos/9.0/demo/latest/base/>: `x86_64_v2/` and `Source/`, like in XCP-ng 8.3
- <https://91.98.220.14:8443/repos/9.0/demo/latest/base/x86_64_v2/repodata/>: `repomd.xml`, and its signature
  `repomd.xml.asc`
- <https://91.98.220.14:8443/isos/9.0/demo/latest/>: the ISOs, with `SHA256SUMS` and its signature
- <https://91.98.220.14/repos/9.0/incoming/latest/>: the snapshot of CI, signed by its key, of each push of the main
  branch, and <https://91.98.220.14/isos/9.0/incoming/latest/>, its ISOs

`xcraft boot` boots an ISO in QEMU, and `xcraft vm` controls the virtual machine, to test an installation.
