# Design: our own copy of the upstream packages we lock

Status: implemented with the main and dev buckets, see xcraft/upstream_copy.py; the check of the releases waits for
their bucket. The pull requests have no copy of their own, see pr-ci-design.md.

## The problem

The lock pins the upstream packages the builds and the snapshots use, but xcraft only gets them from the repositories
of `xcraft.toml`, in their locked versions. Those repositories do not keep every version:

| Repositories | Versions kept | Upstream packages xcp-ng-dist uses on x86_64_v2 |
|---|---|---|
| AlmaLinux: `baseos`, `appstream`, `crb`, `devel` | all of the current minor release, like 10.2; at the next one, about every six months, `almalinux/10/` switches to it, and the previous one moves to `vault.almalinux.org`, complete | 821 |
| EPEL: Fedora's, and AlmaLinux's rebuild for x86_64_v2 | the latest only: 1 package out of more than 26,000 has two versions | 87, like `ccache`, `fakeroot`, `busybox`, `hiredis`, `jemalloc`, `genisoimage`, `epel-rpm-macros` |

When a locked version leaves its repository, which EPEL does at each update, and AlmaLinux at each minor release:

- the build environment image cannot be built: its packages are resolved in their locked versions;
- the build roots needing it cannot be resolved, so the packages built against it cannot be built, not even again;
- `--locked` fails, in CI too, on commits which have nothing to do with it, until the lock is updated to the new
  version.

So an update of EPEL forces an update of the lock, and a version we would rather keep, or go back to, cannot be used
anymore: the lock does not decide the versions alone.

## What we have already

It happened on 2026-11-01: EPEL replaced `xxhash` 0.8.4-1.el10_3 by 0.8.4-2.el10_3, and the checks of xcp-ng-dist's
master, and its aarch64 builds, failed on `xxhash-libs` and `xxhash-devel`, "pinned, but in no repository anymore",
on a commit which only changed build IDs.

## What we have already

- Each published snapshot has, in `base/<arch>/` and `build/<arch>/`, every upstream package the runtime and the builds
  need, in the locked versions. But only the latest one is used, by xcraft publish for its downloads, a version locked
  between two publishes is in none, and only x86_64_v2 is published yet.
- The lock has the SHA-256 of each pinned binary package, from the metadata of its repository: a copy can be checked
  against it, and found by it.
- Each machine keeps the pinned RPMs it downloaded, by SHA-256, in the store of xcraft/pinned.py, the `xcraft-pinned`
  podman volume: the build environment and the builds install from a repository made there, of the pins only. A
  developer's machine has many versions EPEL dropped since, like both `xxhash` RPMs of aarch64 above.
- xcraft.repodata makes the metadata of a repository from the metadata entries of its packages, taken from other
  repositories, without the packages: createrepo_c writes the entries of a package, in primary, filelists and other,
  keyed by its SHA-256, the same in any repository but for the location. xcraft publish makes the snapshots so: the
  entries of the packages of the latest snapshot are read from its metadata, `createrepo_c` only runs on the new RPMs,
  and xcraft.repodata writes the whole metadata.
- The storage stores identical files once, and copies files without transferring them.

## Goals

- Every version of an upstream package the lock ever had stays available to xcraft, from our storage, for the builds,
  the image, the snapshots and the ISOs, whatever the upstream repositories do.
- The lock alone decides the versions: updating it is a choice, like going back to an older version we copied.
- Nothing to do by hand: CI copies what the lock uses.

Not a goal: a mirror of the upstream repositories, only of what we lock.

## Proposal

### Layout

Under `.xcraft/`, with the rest of xcraft's own data, as the copy is for xcraft, not for the users of the published
repositories: a flat repository by architecture, and one of the source packages, append-only.

```
.xcraft/upstream/<arch>/<file>.rpm      every upstream package pinned on <arch> and copied, never removed
.xcraft/upstream/<arch>/repodata/       the metadata of all of them
.xcraft/upstream/Source/<file>.src.rpm  their source packages
.xcraft/upstream/Source/repodata/
```

The lock is the reference: it lists the versions, with the SHA-256 of each binary package, and xcraft checks each RPM
against it. The copy is only a pool to take them from, like the upstream repositories: it has all the versions ever
copied, and xcraft only sees the locked ones, see resolve.py. So it needs no version of its own, nor a view by lock.

A file name is a NEVRA: a package of the same name with other contents, like a rebuild of the same release, is not
copied but reported. AlmaLinux's EPEL for x86_64_v2 rebuilds Fedora's with `.alma_altarch` releases, so they do not
collide, and the architectures have their own directories anyway. The noarch packages are in each of them, stored once
by the storage.

### What goes in

For each architecture, the upstream binary packages the lock pins there: those of the build environment image, of the
build roots of our packages, of `runtime` and of `iso`: about 900 on x86_64_v2. Not all the binary packages of the
locked source packages: their unused subpackages, like debug ones, would multiply the size for no use.

The source packages of those the snapshots publish go in too: of `runtime`, and, with `[publish] build`, of the image
and of the build roots. AlmaLinux's vault keeps all their versions, but EPEL's sources follow its binaries, only the
latest.

### Where each package comes from

`xcraft push upstream` copies, for the architecture, the pinned packages the copy misses, checked against the SHA-256
of the lock, from, in order:

1. the store of the pinned RPMs of the machine, xcraft/pinned.py: the RPMs the builds installed, on CI or on a
   developer's machine, even long gone upstream;
2. a published snapshot: its manifest has the SHA-256 of its files, so the copy is server-side, without transfer;
3. the upstream repositories, while they still have it: downloaded like xcraft/buildenv/pin.py does, into the store.

A package found nowhere is reported: the lock must be updated, as today, but it only happens to packages never copied
nor installed on the machine.

`xcraft push`, without a command, does it too, after the builds: the packages are then in the store of the machine
already, as the builds installed them, so the copy downloads nothing, and the developers do not need to think about
it.

### The buckets

The copy of the ci bucket, `.xcraft/upstream/` there, is the reference, written by CI on the main branch only, with the
CI key: see ci-secret-management-design.md for the buckets. Until they are set up, the ci bucket is the main one.

- **dev:** a developer pushes the packages of their lock the ci copy misses to the copy of the dev bucket.
- **pull requests:** no copy: they push nothing but their stage, in the dev bucket, see pr-ci-design.md, which has
  the upstream packages it publishes.
- **release:** no copy. A release publishes a snapshot of the ci bucket, whose packages are in the ci copy already.

When a pull request is merged, CI on the main branch builds the packages it changed again, so it installs the pins of
the new lock: the pin download, xcraft/buildenv/pin.py, takes them from the ci copy, or else from the upstream
repositories. `xcraft push` then copies them to the ci copy, from the store of the runner.

A version is then lost only if upstream drops it before the main branch's CI downloads it, the risk of the lock
update itself.

### Publishing

xcraft publish copies the packages of the snapshot server-side, without uploading them: ours from the storage, as
today, the upstream ones the latest snapshot has from it, and the other upstream ones from the copy of the storage it
publishes to. It does not fill the copy: on the main branch, `xcraft push` runs before it, in the same job, and fails
it when a pinned package cannot be copied. Those the copy misses anyway, like in the dev bucket after a lock update on
a developer's machine, are downloaded and uploaded with the snapshot, as before the copy.

Publishing to the release bucket checks the ci copy instead, reading it only, with the public reads of the ci bucket:
a release whose lock pins a package the ci copy misses is refused, so a released commit can always be built again,
even if the release bucket only has what it publishes.

### Metadata

Updated like the snapshots' are made, with xcraft.repodata: no RPM of the copy is downloaded to add others, and no
`mergerepo_c`.

1. Download the metadata of `.xcraft/upstream/<arch>/`, a few MiB, and read its entries, by SHA-256.
2. The pinned packages missing in it are those to copy. For those copied server-side from a snapshot, the entries are
   in the snapshot's metadata, which xcraft publish reads already; for those from the store of the machine or the
   upstream repositories, `createrepo_c` runs on these RPMs only, local then.
3. Write the metadata of the old and new entries with xcraft.repodata, the location of each package being its file
   name.
4. Upload the new metadata files, named after their checksums, so new paths, then `repomd.xml` last. The metadata files
   it replaces are deleted by the next update, not at once: a dnf which read the old `repomd.xml` just before still
   finds the files it names.

Nothing to copy, nothing to write: the usual push only downloads the metadata.

### Concurrency

Garage has no conditional writes: two updates of the metadata of an architecture at once, like the pushes of two
branches, can lose the packages of the first. That is harmless: they are in the storage already, and the next update
adds them again, as it compares the pins of the lock to the metadata, not to the files. The risk left is upstream
dropping such a version before the next update, the time of a push: a concurrency group by architecture for the CI
jobs pushing would remove it, cheaply, but is not required.

The jobs of different architectures write different metadata, but for `Source/`, which each one adds the source
packages of its pins to: an update lost there is made again by the next push of its architecture, likewise. The copies
of the pull requests are written by their own jobs only.

### Using it

xcraft adds the copies of the main and dev buckets to the repositories it resolves and downloads the pins from, itself:
no change to `xcraft.toml`, nor to the build environment image, which installs from the repository of the pins only, so
the storage is not an input of the build keys. They are skipped when they cannot be reached, like offline, and are not
checked for signatures by dnf: the pins are checked against their SHA-256, and their RPMs against the keys of the
repositories, see below, and the resolution installs nothing. A version locked and not copied yet, like after a lock
update on a developer's machine, is in the upstream repositories still.

dnf finds a pinned package in the copy when its repository no longer has it, and in both otherwise: the same NEVRA with
the same checksum, so either is fine. Nothing else changes:

- the build keys: they have the versions, not where they come from;
- `xcraft lock --update`: it takes the latest version all the architectures have, which is upstream;
- going back to an older version copied before: edit it in `xcraft.lock`, and it resolves from the copy, as
  `xcraft lock` does not change the versions it has.

The copy only needs the public reads of the storage, so the builds of forks use it too.

### Bootstrap

The first `xcraft push upstream` copies what the lock pins today, from the store of the machine, the latest snapshot,
or the repositories. Run on a developer's machine which built aarch64 before 2026-11-01, it copies the `xxhash` RPMs
EPEL dropped, which makes the lock of xcp-ng-dist usable again without updating it.

## Sizes, on xcp-ng-dist

Measured on the latest snapshot of x86_64_v2:

| | Files | Size |
|---|---|---|
| binary packages, runtime | 530 | 324 MiB |
| binary packages, builds | 377 | 215 MiB |
| source packages | 672 | 4.9 GiB, ours included |

So about 540 MiB by architecture for the binaries, and the sources, shared, up to 4.9 GiB. Each lock update then only
adds the packages it changes. The storage stores identical files once: the packages already
in the snapshots cost nothing, and the noarch ones are stored once for all the architectures.

The sources could be limited to EPEL's, which are the only ones no one else keeps, but the vault is not ours either:
copying them all costs a few GiB once.

## Alternatives considered

- **The metadata merged with `mergerepo_c`.** It has to be checked to keep the locations, and it needs the metadata of
  the new packages from `createrepo_c` anyway: xcraft.repodata does both, already used by xcraft publish.
- **Packages by SHA-256, and metadata by lock.** Nothing is overwritten, so no lost update, but the metadata of a lock
  needs the entries of all its packages: kept by package, or read from the metadata of another lock, which misses the
  packages of the locks of other branches. The lock already says which packages to use, so the copy does not need to.
- **No metadata: fetch the pinned RPMs from the copy by SHA-256 in xcraft/buildenv/pin.py.** Enough for the builds,
  which install from the repository of the pins, but not for the resolution of the image, the build roots and what is
  published, which the checks of `--locked` run on metadata only.
- **The latest snapshot as a repository of the builds.** It has the packages, with metadata, but only the latest
  snapshot, of a publish: a version locked and dropped between two publishes is in none, and the builds would depend on
  publishing.
- **AlmaLinux's vault, by minor release, in `xcraft.toml`.** It keeps AlmaLinux's versions across minor releases, but
  does nothing for EPEL, which is the urgent part.
- **Fedora's build system, Koji, which keeps EPEL's builds for some time.** There is no repository of them, only a URL
  by build, and they are removed eventually.
- **A mirror of the upstream repositories.** Hundreds of GiB, for the few hundred packages we lock.

## Decisions

- The packages of the copy are kept, without expiry: an old package must build again with the upstream packages its
  lock pins, which the repositories may not have anymore. The storage grows with each lock update: a few hundred MiB
  a month, by the rate of the lock updates of xcp-ng-dist.
- The metadata of the copy is not signed: the lock is what is trusted, by the SHA-256 of each package, which xcraft
  checks before using it, and the image and the build roots are checked against the lock once installed. Forged
  metadata could make dnf choose other packages, and a lock update pin them, a new dependency or a new version: the
  pinned RPMs are checked against the keys of the repositories of `xcraft.toml` before the builds install them, and
  before they are copied, so a package no upstream key signs is rejected, unless a repository of the architecture has
  no key. The source archives are not signed for the same reason. The packages keep their upstream signatures.
- The originals are kept, with their upstream signatures: the copies signed again by our keys, for incoming and the
  releases, are kept next to them, see [the layout of the published
  repositories](publishing-layout-design.md#the-upstream-originals-and-their-copies-signed-once-by-key).

## Risks

- The metadata of the copy is read by Python: xcraft.repodata cannot read zstd before Python 3.14. The metadata it
  writes is gzip, so this only matters if it changes.
- The store of a developer's machine is trusted for nothing: its RPMs are copied only when their SHA-256 is the lock's.
