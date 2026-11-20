# Design: the layout of the published repositories, and their signatures

Status: the steps of the [order](#order) implemented, but the fifth one, the release bucket, and `$snapshot`, in the
repository files of the hosts, which the package of xcp-ng-dist installs: the builds signed at build and stored by
signer, the upstream packages signed again in final mode, the publications of dev mode and of the pull requests, the
expiry of the dated snapshots, by `snapshot_days`, the cumulative stages, `xcraft prune`, the metalinks, `xcraft
mirror`, and the end of `--unsigned`. `xcraft prune` keeps the source archives for now. It gathers the questions of
the publication of the pull requests, of the mirrors, of the rollbacks of the hosts, of the release keys and of the
signatures of our packages and of the upstream ones. It builds on [the
buckets of the CI secrets design](ci-secret-management-design.md#4-a-bucket-for-the-releases) and [the CI of pull
requests](pr-ci-design.md).

## Snapshots and stages

A **snapshot** is the distribution as one commit makes it, for all its architectures: our builds of that commit and the
upstream packages its lock pins, published together, at once, and never changed after. It has:

- `base/<arch>/`, a repository by architecture, self-contained: our packages, and the upstream ones they need at
  runtime, the extra ones and the installer's;
- `build/<arch>/`, the upstream packages the builds of ours need, which `base` does not have, when published;
- `base/Source/` and `build/Source/`, the source packages of both;
- the public keys of the signers of its packages, a file by key;
- its ISOs, by architecture, published next to it, under `isos/`;
- the metadata of each repository, signed, and a signed manifest of all its files with their SHA-256.

A **stage** is a channel of snapshots, like `incoming`, a release's `stable`, or a pull request's `pr123`: each
publication adds a dated snapshot, `repos/<release>/<stage>/<date>[.<n>]/`, with its ISOs in
`isos/<release>/<stage>/<date>[.<n>]/`, and copies it as `latest/`, which the hosts and the mirrors use; the `latest/`
of a cumulative stage keeps older versions too, see [the cumulative stages](#the-cumulative-stages).

## The problems

1. **Publishing a pull request copies master's packages.** Its stage, in the dev bucket, needs master's final builds
   and the upstream packages next to its development builds: a gigabyte by architecture, signed and uploaded again at
   each push, as xcraft copies files server-side within a bucket only.
2. **The release stages must be mirrored.** A mirror copies a stage alone: it must have all its files, not refer to
   other paths. The incoming stage is not mirrored, but it must be published like the releases are, to exercise what
   the releases use.
3. **One package name, several contents.** The same package signed by the CI key, a release key or the PR key is
   three files of one name. There is a release key by major version, for XCP-ng 9, 10, and so on: our packages have
   the major version in their `%{dist}`, but an upstream package, of AlmaLinux 10 for both XCP-ng 9 and 10, is then
   two files of one name. Upstream may also publish other contents under the same version, which
   `xcraft lock --update` pins again, and a reset of the build IDs, like the restart at 1, gives a number again to
   other contents.
4. **The upstream packages keep their own signatures.** The hosts must trust the keys of AlmaLinux and EPEL too, and
   anything signed by them is installable from the hosts' other repositories. XCP-ng 8 signs again all the packages it
   publishes, the upstream ones included: the hosts trust its key only.
5. **Each of our packages is stored twice.** The builds are unsigned, and publishing signs a copy of them, kept in
   `.xcraft/signed/` not to sign them again: the same packages, twice the space.
6. **The originals of the upstream packages are kept.** With their upstream signatures, in the upstream copy, see
   [its design](upstream-mirror-design.md), or in a mirror: their signed copies cannot replace them.
7. **The writers of the buckets.** The releases must be out of reach of the CI's credentials: a bucket of their own,
   see the CI secrets design. The main bucket is the one of CI, and of its key.

## Proposal

### Our packages signed at build

Each build signs its packages at its end, before it is stored in the cache: its files are those the stages copy, and
`.xcraft/signed/` is gone. The key depends on the mode of the run:

- **Dev mode,** with `--dev`: the key configured, or else one xcraft generates, without passphrase, with
  `xcraft key create`, in `.xcraft/key/`, showing its fingerprint once, and how to replace it. It is trusted by
  nobody: it only tells which host made a build, as the dev bucket has no `build_keys`. The final builds a dev run
  makes, those the remote storage misses, are signed by it too: a dev run never fails for want of a key.
- **Final mode,** with `--final`: the key configured, the CI key on master, or the run fails before building anything.

rpmbuild has no access to gpg: the container of the build gets neither the agent's socket, nor a key, nor a
`GNUPGHOME`, as its specs, scriptlets and build tools could otherwise sign anything with the key. The packages are
signed once it is gone, in a new container, the one of xcraft/signing.py publishing uses now: the image of the host
with rpmsign, the RPMs of the build and the agent's socket only, running no code of the packages.

The PR key is the key of the dev runs of the pull requests. The key is checked to be in its keyring before the
builds, not to fail at the end of a long one. The developer's key is an Ed25519 one, made at once: the tests make one
by project.

Each build keeps the public key of its signer, `RPM-GPG-KEY` in its directory, pushed and fetched with it: a
repository can publish the keys of its packages without a keyring, after the developer changed keys or machines, and
for the builds fetched from CI. The remote builds stay signed by their manifests too, as now, by the key of their
path, whose SHA-256 of the files bind the packages to it; the packages a snapshot signs are checked against its key.

### The builds by signer

The signer is not an input of the build: the same build signed by two keys is two files of one name. So the builds,
in the cache and in the storage, are by signer, whether signed at build or signed again later:

```text
builds/<fingerprint>/<arch>/<pkg>/<build key>/
```

An entry has the packages signed by its key, its manifest signed by the same key, and its public key. An entry signed
again from another has the same main headers, only the signatures differ. A command of key F builds against the
builds of any key it trusts, the signatures not mattering to the build roots: those of the cache, and those of the
storage under F, or a remote's `build_keys`, in that order, known without listing the storage; else it builds, and
signs by F. A build is signed again into `builds/F/`, from the one of another key, only when F pushes or publishes it
in final mode: the manifest and the packages of a tree always have the same signer, and a remote tree is trusted only
when signed by the key of its path. A change of key, of the CI key or a developer's, a
final build of a dev run reused by a final run, or a previous build reused by [an identical
rebuild](identical-rebuilds-design.md) so make new entries, and change none: nothing of the cache is rewritten, and
`xcraft clean` removes the entries of the former keys. The release builds are `builds/<release key>/` of the release
bucket, signed again from the CI's, and the builds of a retired key are all under one prefix.

Signing again replaces the previous signature, with `rpmsign --resign`: the rpm of EL10 has a single signature
anyway, but rpm 6 can hold several, and a copy must not keep the signature of its original.

### The upstream originals, and their copies signed once by key

The upstream copy, `.xcraft/upstream/<arch>/`, is unchanged: the originals, with their upstream signatures, found by
their SHA-256 in the lock. Next to it, the copies signed again, once by key, kept to be copied without signing them
again:

```text
.xcraft/upstream-signed/<fingerprint>/<arch>/<sha256 of the original>/<file>
```

The source packages are not signed again: an SRPM is not installed, and they are most of the space, up to 4.9 GiB.

The same rule as the builds, a tree by signer, for the copies only: the originals stay as they are, in one tree, as
they are inputs, what the lock pins and the builds install, through one repository, `.xcraft/upstream/<arch>/`. By
their upstream key, they would not be found from the lock, which does not have it, nor the configuration, a
repository giving several keys, like EPEL across a rotation, or none, its packages being unsigned; the builds would
need a repository by key; and they are kept forever, so a pruning by key gains nothing. The copies are outputs, for
the publication of incoming and of the releases only, by our keys.

- **The path is computed from the lock and the key alone,** by the SHA-256 the lock pins, not the signed file's:
  publishing knows where to look without reading anything, and other contents never take the path of a package,
  whatever their names.
- **Metadata by key and architecture,** updated with xcraft.repodata like the upstream copy's: a snapshot takes the
  entries of the signed files from it, without downloading them, and it lists which pins are signed already. Its
  `repomd.xml` is signed by the key, `repomd.xml.asc`, and the metadata files checked against it: what a snapshot
  signs comes from the key, not from whoever can write to the bucket. Metadata without a signature, made before, is
  not read: the pins are signed again.
- **Auditable:** signing rewrites the signature header only, so a signed copy has the main header of its original,
  byte for byte, which anyone can check, from the header ranges of both metadata, a few KiB by package.
- **Kept without expiry,** like the originals: a few hundred MiB by architecture and key, only those a lock update
  brings once the first publication with a key signed them all.

### Server-side copies

A stage has all its files, copied server-side from the builds and the signed copies: those of incoming and of the
releases from their bucket, those of the pull requests partly from the main bucket, see below. The same layout for all
of them: mirrored as they are, and never broken by what happens to the builds or the copies.

Garage v2.1.0 copies by reference, measured with 264 MiB of objects, uploaded whole and by parts: the copies, within a
bucket or to another one, add nothing to the disk, and stay whole once the originals are deleted. A copy to another
bucket needs the read of the source and the write of the destination, and is refused without the read. The version
of the server is to check, for the same behavior.

### Signatures

Every snapshot is signed: its metadata by the key of the stage, its packages as said below. Publishing has no unsigned
mode, and neither have `xcraft iso`, `xcraft boot` and `xcraft push isos`: their `--unsigned` goes, dev mode
generating a key. The upstream packages are checked first, as now: their SHA-256 against the lock, their signatures
against the keys of their repositories.

- **Incoming and the releases:** every package is signed by the key of the stage, the CI key or the release key of
  the major version. Incoming copies the builds of CI as they are, signed by the CI key, and the upstream packages
  from their copies signed by the CI key; a release, both from its builds and copies signed by its key. The hosts,
  the installer and the ISOs trust that key alone: the keys of the upstream repositories are not needed on the hosts
  anymore.
- **The pull requests, the developers' stages, and the local snapshots and ISOs of dev mode:** the packages as they
  are, with no signature at publication: the development builds and the final builds made by the run, signed by its
  key, the PR key or the developer's; master's builds, signed by the CI key; the upstream originals, signed by
  AlmaLinux, EPEL, and so on, those the upstream copy does not have yet included, like a dependency the changes add or
  update: checked against the lock and their upstream signatures, never signed again, the key of their repository,
  from the configuration being published, joining the others. The stage publishes the public key of each signer, in a
  file by key, older versions of rpm importing only the first key of a file, and gives them all: in its `.repo`,
  `gpgkey=` lists them, and its ISOs' installer imports them all, as it imports the key of the snapshot and those of
  the repositories of `xcraft.toml` now. A pull request adding an upstream repository gives its key too, so the
  check of their upstream signatures proves nothing for its packages: the PR key could sign anything for the stage
  anyway, which only the testers trust.

The first publication with a key signs all the upstream packages, 619 in the base repository of x86_64_v2 today,
more with the build repository: around ten minutes by architecture with a hardware key, at a signature a second, then
only those of the lock updates.

### The releases

A release is the publication of an incoming snapshot, tested, in the release bucket, with the release key of its
major version: the same packages, the same inputs, our builds signed again into `builds/<release key>/` and the
upstream packages into the copies of the release key, and the same layout as incoming. The release key signs what
the CI key signed: a compromised CI key gets its packages into a release, as before this design, see [the CI secrets
design](ci-secret-management-design.md).

### The stages of the pull requests

`xcraft publish --dev`, see [the CI of pull requests](pr-ci-design.md), signs nothing: the development builds, signed
at build by the PR key, are files of the stage, not pushed as builds, as their counter is the run number, and they
are used once. Master's builds and the upstream originals are copied server-side from the main bucket, with its read
given to the dev bucket's key of the pull requests: nothing is uploaded for them, and the stage has all its files. A
final package the main bucket does not have, master not having built it, is built by the run, signed by the PR key, and
a file of the stage, as the development builds. An upstream package the main copy does not have, that the pull request
adds or updates, is uploaded as it is from the store of the runner, as for every dev publication, see
[signatures](#signatures). The ISOs, made from the files, download the copied packages.

### The cumulative stages

`dnf history rollback`, `undo` and `downgrade` need the older versions in an enabled repository: dnf does not take
them from its cache, but with `keepcache=1` set before the update. A snapshot has the current versions only, so a host
cannot go back. A stage can so be **cumulative**: its `latest/` has the packages of its latest snapshot, and the older
versions the stage published, kept by a retention, with metadata over all of them. dnf updates to the newest version
of each package, which are those of the latest snapshot: an updated host is the same either way, the older versions
are only there to go back. It is how the repositories of RHEL and AlmaLinux are, cumulative within a minor release.

A stage is cumulative when `xcraft.toml` gives it a retention, exact otherwise, like the stages of the pull requests
and of the developers, whose hosts do not follow them over time:

```toml
[publish.stages.stable]
retention = { days = 90, keep = 2 }

[publish.stages.incoming]
retention = { days = 30, keep = 1 }
snapshot_days = 90
```

- **`days`:** how long a version stays once out of the latest snapshot; without it, for the life of the release.
- **`snapshot_days`,** apart from the retention, of any stage, cumulative or not: how long its dated snapshots are
  kept, but the latest one; without it, for the life of the release, as those of the releases, which hosts can pin.
  It is also how long their builds are kept, see [removing what nothing refers to](#removing-what-nothing-refers-to):
  a commit of master is a starting point without rebuilding for 90 days, which costs the metadata of the snapshots
  only, where the rollbacks from incoming's `latest/` need 30 days.
- **`keep`:** the versions always kept, the current one included, for the packages of the latest snapshot, by name
  and architecture: a host updating once a quarter jumps over several versions, and the time alone would remove the
  one it comes from. A package out of the latest snapshot, removed, renamed or no longer built for an architecture,
  has the time rule only, and its versions go after `days`.
- **By stage and release:** the history of a stage is what it published, its previous `latest/`, from its signed
  inputs, and the new snapshot. The first publication of a stage, like the first `stable` of a release, has its
  snapshot only: the versions of incoming that no host of the users installed never reach it. A new release,
  `repos/<release>/`, starts empty the same way; a host updated by dnf goes back with the repositories of the previous
  release, `--releasever`, while it is published.
- **The source packages** follow their binaries: one goes with the last binary built from it, kept as long as one is.
- **One name, one content:** a cumulative `latest/` cannot have two files of one name. Our build IDs make our
  packages unique; an upstream package pinned again with other contents under the same NEVRA, see the problems,
  replaces the previous one, which xcraft reports.
- **Changing the configuration** applies at the next publication of the stage: from exact to cumulative, its history
  starts there; from cumulative to exact, `latest/` loses its history; a shorter retention removes the versions past
  it at once. `[publish] stage` stays the default stage, the tables only describe them.

The dated snapshots stay exact: what one commit made, which its `contents.json`, its ISOs and `xcraft contents
--diff-stage` describe, and not what a retention happened to keep that day. `latest/` is then not a copy of the latest
snapshot anymore, but has metadata of its own, made again when its history changes, even when the retention only
removes versions. Publishing it writes, in order:

1. the new packages, new names nothing refers to yet;
2. the metadata files, named after their checksums by createrepo_c, the metalinks, whose alternates have the previous
   `repomd.xml`, then `repomd.xml` and its signature: a reader gets the previous repository or the new one, both
   complete, but for the request between the two files;
3. the manifest, last, with all the files;
4. the deletions, of the versions past the retention only, and of the metadata files of the previous `repomd.xml`
   at the next publication: no host needs a file the moment it is removed.

So the pool of [the CI secrets design](ci-secret-management-design.md#mirrors-and-atomic-publication) is not
needed: the mirrors copy `latest/` alone, see below, and it only grows, but by the retention.

### The dated snapshots for the hosts

The repository files of the hosts select the snapshot by a variable of dnf, `$snapshot`, in a file of
`/etc/dnf/vars/`, which the package of the repositories installs, `%config(noreplace)`, with `latest`:

```ini
[xcp-ng-base]
metalink=https://updates.xcp-ng.org/repos/9.0/stable/$snapshot/base/x86_64_v2/repodata/metalink.xml
baseurl=https://updates.xcp-ng.org/repos/9.0/stable/$snapshot/base/x86_64_v2/
```

As dnf 4.20 of AlmaLinux 10.2 does it, tested with a metalink and local servers:

- **Pinned:** `echo 2026-11-20 > /etc/dnf/vars/snapshot` keeps a host on a snapshot, until it is `latest` again. Once,
  to test or reproduce a state, the URL is given, `--setopt=xcp-ng-base.metalink=<URL of the snapshot>`:
  `DNF_VAR_snapshot` is ignored when the file exists, which it always does.
- **Going back:** a host pinned to A then moved to B goes back from A, which has all its previous versions, with A in
  the file for the time of `dnf history rollback <n>`. The hosts following `latest/` go back with its history.
- **A cache by URL:** dnf keeps the metadata of each value of `$snapshot` in its own directory, `<repo>-<hash>`, and
  takes those of the current value, with `-C` too.
- **Without the file,** dnf keeps `$snapshot` as it is in the URL, and the repository fails, like a mistyped date.
- **The metalink first, then `baseurl`:** with both, dnf uses the metalink, and `baseurl` only when it cannot
  download it: with the metalink alone, our server down fails the repository. `baseurl` on our server too is so a
  fallback for the metalink only, not for our server; on a mirror, the hosts would depend on it.
- **A stale mirror is skipped:** dnf takes its `repomd.xml`, and its `primary`, then the next mirror, whose
  `repomd.xml` matches the metalink: what a stale mirror costs is its metadata, downloaded for nothing.
- **x86_64_v2** is written in the path, `$basearch` being x86_64 there: a repository file by architecture, or a
  variable of ours in `/etc/dnf/vars/` too.
- **The dated snapshots of a release are a public interface** once hosts can pin them: they are kept for the life of
  the release, and the mirrors do not have them.

### The mirrors

The mirrors copy `latest/` of the stages of the releases, and of the ISOs, by rsync: the dated snapshots stay in our
storage only, like vault.almalinux.org or Fedora's archives, and a mirror has one copy of the repository, which rsync
updates by its new packages only. Garage serves S3 and HTTP only, and mirrorbits needs a file system too, so a
**primary mirror** host has the copy the mirrors sync from:

- **`xcraft mirror <stage> <dir>`** makes it from the public HTTP endpoint, without S3 credentials: it checks the
  signed manifest of `latest/`, hard links the files whose SHA-256 it has already, downloads the others and checks
  them against the manifest, in a new directory, then switches the symlink an rsync module serves, with `ln -sfn` and
  `mv -T`. An rsync session sees one complete tree, checked before any mirror gets it, which the bucket cannot give,
  as S3 writes no two objects at once. rclone hard links nothing, and compares by size and date, or by the MD5 of the
  ETags, which multipart uploads do not have: a package signed again under the same name and size would be missed.
- **Its files have the dates of the storage:** each one the `Last-Modified` of its object, which the HTTP endpoint
  gives, and fixed modes, 0644 and 0755. The copy only depends on the bucket: made again from scratch, on a new host
  or after a loss, it is the same for rsync, whose quick check compares sizes and dates, and the mirrors transfer
  nothing again. A hard link has one date: a file is linked only to one of the same SHA-256 and the same date, and
  downloaded otherwise.
- **The copy is checked against the manifest,** which it has, as the mirrors do: `xcraft mirror --check <dir>`
  checks its signature with the release keys only, then the SHA-256 of each file, reports the files the manifest does
  not have, and downloads again the damaged ones, as a periodic timer of the host. Without xcraft, a mirror checks its
  copy with our public key: `gpgv --keyring <keys> manifest.asc && gpg --decrypt manifest.asc | tail -n +2 |
  sha256sum -c --quiet`, `gpg --decrypt` printing the text whatever the key, so after `gpgv` only. Publishing writes
  the manifest of `latest/` last, after its metadata and its metalinks, so that it has all its files.
- **It runs after each publication of a mirrored stage,** started by the CI, and periodically too, as a fallback.
- **The mirrors sync with `rsync -a --delay-updates --delete-after`:** the changed files are put in place together at
  the end, and what `latest/` deletes is past the retention, which no host needs anymore.

The hosts find the mirrors with a **metalink**, which dnf supports as Fedora, EPEL and AlmaLinux use it: a file by
repository, written by `xcraft publish` next to `repomd.xml`, `repodata/metalink.xml`, with the URL of `repomd.xml` on
our server and on each mirror, its size and SHA-256, and those of the few previous ones, its alternates, for the
mirrors a sync behind. dnf takes the repository from the first mirror whose `repomd.xml` matches, by `preference`, or
by latency with `fastestmirror=True`, which the package of the repositories sets, and skips the stale or broken ones:
a mirror can be late or slow, never wrong, the packages being checked by the metadata, and the metadata by their
signature. The metalinks of the dated snapshots list our server alone. A static metalink sends every host the same
list, without the routing by country of MirrorManager, enough for a few mirrors; the mirrors are a list by stage
of `xcraft.toml`, `[publish.stages.<stage>] mirrors`, and changing it writes the metalinks again, without changing
the repositories.

Compared to mirrorbits, which redirects each file of XCP-ng 8:

- our server serves a small file by refresh of the metadata, rather than a redirection by file;
- a host gets all its repository from one mirror, checked against the hash of `repomd.xml`, where mirrorbits redirects
  a file by what its last scan of the mirrors found, by size and date, a `repomd.xml` changed with the same size
  included;
- the routing by country, the health checks of the mirrors and the statistics are lost: a check of the `repomd.xml`
  of each mirror against ours, and a `lastupdate` file by stage, tell their delay;
- dnf only reads metalinks: mirrorbits stays for the ISOs and the network installs of the installer, which use plain
  URLs, and whose files never change in place, which its scans then follow without risk.

### Removing what nothing refers to

What is published is the reference: the builds of everything a kept publication has are kept, and the rest is
removed. Keeping them costs their manifests only, Garage sharing the blocks of the copies, and any published state is
a starting point for a fix, `xcraft maint start --at <commit>` finding all its builds, so that a security fix
rebuilds only the patched package.

- **The stages of the pull requests** are removed when they close, with `xcraft unpublish`, which leaves nothing
  behind: their files are their own, copies or development builds. They are the frequent removals.
- **The dated snapshots of incoming,** one by push of master, expire after its `snapshot_days`, 90, but the latest
  and those of the releases, which are published from them. Their files are copies of the builds and of the signed
  copies: their blocks are shared, so they cost their metadata only, and free nothing else. Their retention is so the
  one of the builds of master: an older commit is a starting point after rebuilding its packages.
- **The dated snapshots of the releases** are kept for the life of the release, see the hosts above.
- **The signed copies** are kept, like the originals; those of the release keys while the releases are.
- **The remote builds** are pruned by `xcraft prune --to <remote> [--keep-days N] [-n]`, a mark and sweep run by a
  weekly job of CI, with the key of the bucket, as `xcraft clean` does for the local cache:
  - **marked:** the builds of the inputs of each kept publication, its dated snapshots and `latest/`, and those whose
    manifest was uploaded in the last N days, 15 by default, in every bucket: a grace period for a publication
    running meanwhile, or failing, its builds waiting for the next one, over a weekend or holidays. What must last
    longer is published, and kept by the retention of its stage, the embargoed builds included, in their private
    bucket;
  - **removed:** the other build trees, their manifest, their generations and their public key, and the generations
    of the kept trees that their last manifest does not have, which are garbage already;
  - **the other inputs of a starting point** follow the same rule: the source archives of `sources/` the manifests of
    the marked builds list. The upstream originals and their signed copies are kept anyway; the images and the
    resolutions are made again from the lock;
  - **the branches whose builds matter publish:** master to incoming, at each push; a maintenance branch to the
    incoming of its release, `[publish] release` telling them apart, so that the main bucket has no other stage; an
    embargo branch to a private stage of `remote.private`. Their builds are otherwise
    kept for the grace period only;
  - **the dev bucket:** the open pull requests keep their builds by their stages, and the developers' pushes go after
    the grace period;
  - **the release bucket** is not pruned automatically: everything a supported release published is kept, and a
    release is pruned by hand at its end of life.

  `-n` shows what would go, and the space it would free: what no kept publication has, the builds never published, or
  whose publications expired.

### The keys and the buckets

| Bucket | Writers | Keys | Signed by them | Reads |
|---|---|---|---|---|
| main | the CI of master | the CI key | the builds, the copies of the upstream packages | |
| release | the release manager, or the signing host | a release key by major version | the builds, signed again, and the copies of the upstream packages | main, for the releases |
| dev | the developers, the CI of the pull requests | their keys | the builds | main, for master's packages |

The trust of xcraft follows: the build keys of a bucket are those of its writers. A release key signs only the trees
of its major version, `repos/<release>/` and its copies: to bind, by `[publish] release`.

## Open questions

- The binding of the release keys to their major versions in `xcraft.toml`, and whether the CI key is by major version
  too.
- The incoming stage of aarch64, which is not published yet: the pull requests need it, and the releases.
- Whether `baseurl=` of the hosts is our server, a fallback for the metalink alone, or a mirror, the hosts then
  depending on it when our server is down.
- Whether a recent mirrorbits compares hashes, or excludes paths, for the files changed in place.

## Order

1. Our packages signed at build, in both modes, with the public key kept with each build, replacing `.xcraft/signed/`,
   and the local snapshots and ISOs of dev mode trusting the signers of their packages. The storage and the caches
   are wiped, as at a full rebuild: no migration of the unsigned builds.
2. The copies of the upstream packages signed by the CI key, with the keys of incoming's hosts and ISOs reduced to
   ours, and the end of the unsigned publications.
3. `xcraft publish --dev`, with the copies from the main bucket, `xcraft unpublish`, then the CI of the pull requests.
4. The expiry of the dated snapshots of incoming.
5. The release bucket, the release keys by major version, and their signed copies.
6. The cumulative stages, and `xcraft prune`.
7. The metalinks, `$snapshot` in the repository files, and `xcraft mirror` with the primary mirror host.
8. The end of `--unsigned` for `xcraft iso`, `xcraft boot` and `xcraft push isos`.
