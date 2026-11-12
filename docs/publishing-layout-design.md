# Design: the layout of the published repositories, and their signatures

Status: the first three steps of the [order](#order) implemented: the builds signed at build and stored by signer,
the upstream packages signed again in final mode, and the publications of dev mode and of the pull requests; the
expiry of the snapshots of incoming and the release bucket to come. It gathers the questions of the publication of
the pull requests, of the mirrors, of the release keys and of the signatures of our packages and of the upstream
ones. It builds on [the buckets of the CI secrets
design](ci-secret-management-design.md#4-a-bucket-for-the-releases) and [the CI of pull requests](pr-ci-design.md).

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
`isos/<release>/<stage>/<date>[.<n>]/`, and copies it as `latest/`, which the hosts and the mirrors use.

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
   see the CI secrets design. The buckets are named after their writers, not after the keys.

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
releases from their bucket, those of the pull requests partly from the ci bucket, see below. The same layout for all
of them: mirrored as they are, and never broken by what happens to the builds or the copies.

Garage v2.1.0 copies by reference, measured with 264 MiB of objects, uploaded whole and by parts: the copies, within a
bucket or to another one, add nothing to the disk, and stay whole once the originals are deleted. A copy to another
bucket needs the read of the source and the write of the destination, and is refused without the read. The version
of the server is to check, for the same behavior.

### Signatures

Every snapshot is signed: its metadata by the key of the stage, its packages as said below. Publishing has no unsigned
mode. The upstream packages are checked first, as now: their SHA-256 against the lock, their signatures against the
keys of their repositories.

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
are used once. Master's builds and the upstream originals are copied server-side from the ci bucket, with its read
given to the dev bucket's key of the pull requests: nothing is uploaded for them, and the stage has all its files. A
final package the ci bucket does not have, master not having built it, is built by the run, signed by the PR key, and
a file of the stage, as the development builds. An upstream package the ci copy does not have, that the pull request
adds or updates, is uploaded as it is from the store of the runner, as for every dev publication, see
[signatures](#signatures). The ISOs, made from the files, download the copied packages.

### Removing what nothing refers to

- **The stages of the pull requests** are removed when they close, with `xcraft unpublish`, which leaves nothing
  behind: their files are their own, copies or development builds. They are the frequent removals.
- **The dated snapshots of incoming,** one by push of master, expire after a retention, like 90 days, but the latest
  and those of the releases, which are published from them. Their files are copies of the builds and of the signed
  copies: their blocks are shared, so they cost their metadata only, and free nothing else.
- **The signed copies** are kept, like the originals; those of the release keys while the releases are.
- **The remote builds** are not pruned: master's builds that no lock has anymore, and those the developers push to
  the dev bucket, as now, see the open questions.

### The keys and the buckets

| Bucket | Writers | Keys | Signed by them | Reads |
|---|---|---|---|---|
| ci | the CI of master | the CI key | the builds, the copies of the upstream packages | |
| release | the release manager, or the signing host | a release key by major version | the builds, signed again, and the copies of the upstream packages | ci, for the releases |
| dev | the developers, the CI of the pull requests | their keys | the builds | ci, for master's packages |

The trust of xcraft follows: the build keys of a bucket are those of its writers. A release key signs only the trees
of its major version, `repos/<release>/` and its copies: to bind, by `[publish] release`.

## Open questions

- The retention of the dated snapshots of incoming, and whether the other stages of the ci bucket have one.
- The binding of the release keys to their major versions in `xcraft.toml`, and whether the CI key is by major version
  too.
- Whether a repository of all the versions, a flat repository, is published for the hosts, to install an older
  version; the builds and the signed copies make it cheap, as metadata only.
- Whether `xcraft iso`, `xcraft boot` and `xcraft push isos` keep `--unsigned`, now that dev mode generates a key.
- The pruning of the remote builds, master's that no lock of a kept branch has, and the developers' in the dev
  bucket.
- The incoming stage of aarch64, which is not published yet: the pull requests need it, and the releases.

## Order

1. Our packages signed at build, in both modes, with the public key kept with each build, replacing `.xcraft/signed/`,
   and the local snapshots and ISOs of dev mode trusting the signers of their packages. The storage and the caches
   are wiped, as at a full rebuild: no migration of the unsigned builds.
2. The copies of the upstream packages signed by the CI key, with the keys of incoming's hosts and ISOs reduced to
   ours, and the end of the unsigned publications.
3. `xcraft publish --dev`, with the copies from the ci bucket, `xcraft unpublish`, then the CI of the pull requests.
4. The expiry of the dated snapshots of incoming.
5. The release bucket, the release keys by major version, and their signed copies.
