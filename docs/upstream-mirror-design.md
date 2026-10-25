# Design: our own copy of the upstream packages we lock

Status: proposal.

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

- Each published snapshot has, in `base/<arch>/` and `build/<arch>/`, every upstream package the runtime and the builds
  need, in the locked versions, and snapshots are never deleted. But only the latest one is used, by xcraft publish
  for its downloads, and a version locked between two publishes is in none.
- The lock has the SHA-256 of each locked binary package, from the metadata of its repository: a copy can be checked
  against it, and found by it.
- The storage stores identical files once, and copies files without transferring them.

## Goals

- Every version of an upstream package the lock ever had stays available to xcraft, from our storage, for the builds,
  the image, the snapshots and the ISOs, whatever the upstream repositories do.
- The lock alone decides the versions: updating it is a choice, like going back to an older version we copied.
- Nothing to do by hand: CI copies what the lock uses.

Not a goal: a mirror of the upstream repositories, only of what we lock.

## Proposal

### Layout

In the storage, a repository by architecture, append-only:

```
upstream/<arch>/Packages/<file>.rpm    the upstream binary packages ever locked and used on <arch>
upstream/<arch>/repodata/              their metadata, all versions
upstream/Source/Packages/<file>.rpm    their source packages, for the snapshots
upstream/Source/repodata/
```

The packages are never removed: the repository has all the versions, and xcraft only sees the locked ones, as with the
upstream repositories (see resolve.py).

### What goes in

For each architecture, the upstream binary packages the lock uses there: those of the build environment image, of the
build roots of our packages, of `runtime` and of `iso`, as resolved: about 900 on x86_64_v2. Not all the binary packages
of the locked source packages: their unused subpackages, like debug ones, would multiply the size for no use.

Their source packages go in `upstream/Source/`. AlmaLinux's vault keeps all their versions, but EPEL's sources follow
its binaries: only the latest.

### Where each package comes from

`xcraft push upstream` copies, for the architecture, the packages the storage misses, checked against the SHA-256 of
the lock, from, in order:

1. a published snapshot: the manifests of the snapshots have the SHA-256 of their files, so the copy is server-side,
   without transfer;
2. the upstream repositories, while they still have it: downloaded in the build environment, like xcraft publish does,
   checked, uploaded.

A package found nowhere is reported: the lock must be updated, as today, but it only happens to packages never copied,
like on the first run, or when CI did not run between a lock update and the next upstream one.

`xcraft push`, without a command, does it too, after the builds: CI keeps the copy complete, and the developers do not
need to think about it.

### Metadata

The metadata of a repository lists all its packages, but its RPMs are not where it is made: it is updated by merging,
without downloading the packages already there.

1. Download the current metadata of `upstream/<arch>/`: a few MiB.
2. Make the metadata of the packages copied now, with `createrepo_c` on them: they were downloaded or are in the cache
   for the copy, but for those copied server-side from a snapshot, which have to be downloaded for this, like
   xcraft publish does.
3. Merge both with `mergerepo_c`, which only reads metadata: the packages keep their `location` in `Packages/`.
4. Upload the new metadata files, named after their checksums, then `repomd.xml` last: a reader sees the old metadata
   or the new one, complete. The old metadata files are deleted later, by the next update, not to break a dnf which
   read the old `repomd.xml` just before.

### Concurrency

Garage has no conditional writes, so two updates of the same metadata at once would lose the packages of one of them:

- each architecture has its repository, updated by its own CI job: they never write the same metadata, but for
  `upstream/Source/`, which only the job of the first architecture of `xcraft.toml` updates;
- the jobs of an architecture take a concurrency group of their own, for all the branches, not to run at once;
- an update lost anyway, like by a developer's push at the same time, is made again by the next one: it compares the
  packages the lock uses to the metadata, not to the files.

### Using it

xcp-ng-dist adds the repository to `xcraft.toml`, with the keys of the upstream repositories, as the packages keep
their upstream signatures:

```toml
[repos.upstream]                       # our copy of the upstream packages ever locked: see xcraft push upstream
baseurl = "https://<storage>/upstream/{arch}/"
gpgkey = "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10 https://.../RPM-GPG-KEY-EPEL-10"
source_baseurl = "https://<storage>/upstream/Source/"
```

dnf finds a locked package there when its repository no longer has it, and in both otherwise: the same NEVRA with the
same checksum, so either is fine. Nothing else changes:

- the build keys: they have the versions, not where they come from;
- `xcraft lock --update`: it takes the latest version all the architectures have, which is upstream;
- going back to an older version copied before: edit it in `xcraft.lock`, and it resolves from the copy, as
  `xcraft lock` does not change the versions it has.

The copy only needs the public reads of the storage, so the builds of forks use it too.

### Bootstrap

The first `xcraft push upstream` copies what the lock uses today, from the latest snapshot or the repositories. The
versions already gone from both, if any, are reported: updating them in the lock, as today, makes them available again,
and copied from then on.

## Sizes, on xcp-ng-dist

Measured on the latest snapshot of x86_64_v2:

| | Files | Size |
|---|---|---|
| binary packages, runtime | 530 | 324 MiB |
| binary packages, builds | 377 | 215 MiB |
| source packages | 672 | 4.9 GiB, ours included |

So about 540 MiB by architecture for the binaries, and the sources, shared, up to 4.9 GiB. Each lock update then only
adds the packages it changes. The storage stores identical files once: the packages already in the snapshots cost
nothing, and neither do the noarch ones of the other architectures.

The sources could be limited to EPEL's, which are the only ones no one else keeps, but the vault is not ours either:
copying them all costs a few GiB once.

## Alternatives considered

- **The latest snapshot as a repository of the builds.** It has the packages, with metadata, but only the latest
  snapshot, of a publish: a version locked and dropped between two publishes is in none, and the builds would depend on
  publishing.
- **AlmaLinux's vault, by minor release, in `xcraft.toml`.** It keeps AlmaLinux's versions across minor releases, but
  does nothing for EPEL, which is the urgent part.
- **Fedora's build system, Koji, which keeps EPEL's builds for some time.** There is no repository of them, only a URL
  by build, and they are removed eventually.
- **A mirror of the upstream repositories.** Hundreds of GiB, for the few hundred packages we lock.

## Risks and open questions

- `mergerepo_c` keeping the `location` of the packages unchanged, and merging `filelists` and `other` too: to check with
  a prototype, before relying on it. Otherwise, the metadata of the packages copied before could be kept as is, and only
  `primary` merged by xcraft, with `createrepo_c`'s Python bindings.
- The copy is unsigned as a repository: its packages keep their upstream signatures, which dnf checks, but its metadata
  could be signed too, with the release key, like the snapshots'.
- The storage grows with each lock update, without removal: a few hundred MiB a month, by the rate of the lock updates
  of xcp-ng-dist. A later cleanup could remove the versions no tag nor branch locks anymore.
- AlmaLinux's EPEL for x86_64_v2 rebuilds Fedora's, with `.alma_altarch` releases: they are different packages, by
  architecture, which the layout by architecture already separates.
