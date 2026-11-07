# Design: the layout of the published repositories, and their signatures

Status: proposal, gathering the questions of the publication of the pull requests, of the mirrors, of the release
keys and of the signature of the upstream packages. It builds on [the buckets of the CI secrets
design](ci-secret-management-design.md#4-a-bucket-for-the-releases) and [the CI of pull requests](pr-ci-design.md).

## Snapshots and stages

A **snapshot** is the distribution as one commit makes it, for all its architectures: our builds of that commit and the
upstream packages its lock pins, published together, at once, and never changed after. It has:

- `base/<arch>/`, a repository by architecture, self-contained: our packages, and the upstream ones they need at
  runtime, the extra ones and the installer's;
- `build/<arch>/`, the upstream packages the builds of ours need, which `base` does not have, when published;
- `base/Source/` and `build/Source/`, the source packages of both;
- the public keys of its signatures, and its ISOs, by architecture, published next to it, under `isos/`;
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
5. **The writers of the buckets.** The releases must be out of reach of the CI's credentials: a bucket of their own,
   see the CI secrets design. The buckets are named after their writers, not after the keys.

## Proposal

### A pool of signed packages, by key and content

Each bucket has a pool of the packages its writers signed, public, kept:

```text
pool/<fingerprint>/<arch>/<sha256 of the package as built or downloaded>/<file>
```

- **By key:** the packages of the CI key, of each release key, of a developer, side by side in one bucket, without a
  bucket by key.
- **By content:** the SHA-256 of the package before it is signed, the one of its build tree for ours, the one of its
  pin in the lock for the upstream ones. A path is computed from the builds and the lock alone, without reading the
  storage, and other contents never take the path of a package, whatever their names: a reset of the build IDs or an
  upstream package published again cannot overwrite one.
- **Signed once:** the signatures have a date, so a package is signed once by key, then copied: the rule of the signed
  builds today, in `.xcraft/signed/`, which the pool replaces, as a public tree, extended to the upstream packages.

### Server-side copies

A stage has all its files, copied server-side from a pool: the stages of incoming and of the releases from the pool of
their bucket, those of the pull requests partly from the pool of the ci bucket, see below. The same layout for all of
them: mirrored as they are, and never broken by what happens to a pool.

Garage v2.1.0 copies by reference, measured with 264 MiB of objects, uploaded whole and by parts: the copies, within a
bucket or to another one, add nothing to the disk, and stay whole once the originals are deleted. A copy to another
bucket needs the read of the source and the write of the destination, and is refused without the read. The version
of the server is to check, for the same behavior.

### Signatures

Every snapshot is signed: its metadata, and each of its packages, ours and the upstream ones, signed once each into
the pool by key. Publishing has no unsigned mode. The upstream packages are checked first, as now: their SHA-256
against the lock, their signatures against the keys of their repositories.

- **Incoming and the releases:** every package is signed by the key of the stage, the CI key or the release key of
  the major version. The hosts, the installer and the ISOs trust that key alone: the keys of the upstream
  repositories are not needed on the hosts anymore.
- **The pull requests, and the developers' stages:** their development builds are signed by their key, the PR key or
  the developer's, and master's packages keep the CI key's signature, copied as they are. The metadata is signed by
  their key, and the stage publishes both public keys, in two files, older versions of rpm importing only the first
  key of a file: the testers' `.repo` gives both, `gpgkey=<stage>/RPM-GPG-KEY <stage>/RPM-GPG-KEY-ci`.

The first publication with a key signs all the upstream packages, 619 in the base repository of x86_64_v2 today,
more with the build repository: around ten minutes by architecture with a hardware key, at a signature a second, then
only those of the lock updates.

### The releases

A release is the publication of an incoming snapshot, tested, in the release bucket, with the release key of its
major version: the same packages, the same inputs, signed again into the release pool, and the same layout as
incoming.

### The stages of the pull requests

`xcraft publish --dev`, see [the CI of pull requests](pr-ci-design.md), signs the development builds by the PR key as
files of the stage, not in a pool: their counter is the run number, so they are signed and used once. The final
packages, master's builds and the upstream ones, are copied server-side from the pool of the ci bucket, with the read
of the ci bucket given to the dev bucket's key of the pull requests: nothing is signed nor uploaded for them, and the
stage has all its files. A final package the ci pool does not have, master not having published it, is signed by the
PR key into the stage, as the development builds. The ISOs, made from the files, download the copied packages.

### Removing what nothing refers to

A pool entry also in a stage costs nothing, its blocks being shared: what costs is what only a pool has. So:

- **The stages of the pull requests** are removed when they close, with `xcraft unpublish`, which leaves nothing
  behind: their files are their own, copies or development builds. They are the frequent removals, and change no
  pool.
- **The dated snapshots of incoming** are what makes a pool grow: one by push of master, each with the builds of its
  changes. Kept forever, they would keep every pool entry. They expire instead, after a retention, like 90 days,
  but the latest and those of the releases, which are published from them.
- **The pools** are then pruned by their bucket's writer, by a command run with the publications, or on a schedule:
  it marks the entries the stages of its bucket have, by the SHA-256 of their manifests, and removes the others older
  than a day, not to disturb a publication having just signed them. The stages of the other buckets have their own
  copies: they are not read. The release pool is never pruned while the releases are kept.

### The keys and the buckets

| Bucket | Writers | Keys, in their pools | Reads |
|---|---|---|---|
| ci | the CI of master | the CI key | |
| release | the release manager, or the signing host | a release key by major version | ci, for the releases |
| dev | the developers, the CI of the pull requests | their keys | ci, for master's packages |

The trust of xcraft follows: the build keys of a bucket are those of its pools. A release key signs only the trees
of its major version, `repos/<release>/` and its pool: to bind, by `[publish] release`.

## Open questions

- The retention of the dated snapshots of incoming, and whether the other stages of the ci bucket have one.
- The binding of the release keys to their major versions in `xcraft.toml`, and whether the CI key is by major version
  too.
- Whether a repository of all the versions of the pool, a flat repository, is published for the hosts, to install an
  older version; the pool makes it cheap, as metadata only.
- Whether `xcraft iso`, `xcraft boot` and `xcraft push isos` keep `--unsigned`, for the ISOs of a developer without a
  key, once publishing has none.
- The incoming stage of aarch64, which is not published yet: the pull requests need it, and the releases.

## Order

1. The pool, replacing `.xcraft/signed/`, with the stages copied from it: no change for the hosts.
2. The signature of the upstream packages, with the keys of the ISOs and of the hosts reduced to ours, and the end of
   the unsigned publications.
3. `xcraft publish --dev`, with the copies from the ci bucket and the two keys, `xcraft unpublish`, then the CI of
   the pull requests.
4. The expiry of the dated snapshots of incoming, and the pruning of the pools.
5. The release bucket and the release keys by major version.
