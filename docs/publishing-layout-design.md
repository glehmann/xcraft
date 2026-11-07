# Design: the layout of the published repositories, and their signatures

Status: proposal, gathering the questions of the publication of the pull requests, of the mirrors, of the release
keys and of the signature of the upstream packages. It builds on [the buckets of the CI secrets
design](ci-secret-management-design.md#4-a-bucket-for-the-releases) and [the CI of pull requests](pr-ci-design.md).

## The problems

1. **Publishing a pull request copies master's packages.** Its stage, in the dev bucket, needs master's final builds
   and the upstream packages next to its development builds: a gigabyte by architecture, signed and uploaded again at
   each push, as xcraft copies files server-side within a bucket only.
2. **The release stages must be mirrored.** A mirror copies a stage alone: it must have all its files, not refer to
   other paths. The incoming stage is not mirrored, but it must be published like the releases are, to exercise what
   the releases use.
3. **One package name, several contents.** The same package signed by the CI key, a release key or the PR key is
   three files of one name; there is a key by major release, for XCP-ng 9, 10, and so on; upstream may publish other
   contents under the same version, which `xcraft lock --update` pins again; and a reset of the build IDs, like the
   restart at 1, gives a number again to other contents.
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

- **By key:** the packages of the CI key, of each release key, of the PR key, of a developer, side by side in one
  bucket, without a bucket by key.
- **By content:** the SHA-256 of the package before it is signed, the one of its build tree for ours, the one of its
  pin in the lock for the upstream ones. A path is computed from the builds and the lock alone, without reading the
  storage, and other contents never take the path of a package, whatever their names: a reset of the build IDs or an
  upstream package published again cannot overwrite one.
- **Signed once:** the signatures have a date, so a package is signed once by key, then copied: the rule of the signed
  builds today, in `.xcraft/signed/`, which the pool replaces, as a public tree, extended to the upstream packages.

### Every package signed by the key of the stage

Publishing signs every package of the snapshot with its key, ours and the upstream ones, once each, into the pool. The
upstream packages are checked first, as now: their SHA-256 against the lock, their signatures against the keys of
their repositories. The hosts, the installer and the ISOs then trust one key, the stage's: the keys of the
repositories are not needed on the hosts anymore.

The first publication with a key signs all the upstream packages, 619 in the base repository of x86_64_v2 today,
more with the build repository: around ten minutes by architecture with a hardware key, at a signature a second, then
only those of the lock updates.

### The stages: plain files, copied from the pool

A stage, `repos/<release>/<stage>/<date>/` and `latest/`, has all its files, copied server-side from the pool of its
bucket, as the snapshots copy the builds now: the same for incoming and for the releases, mirrored as they are, and
stored once by Garage, its blocks being shared.

A release is the publication of an incoming snapshot, tested, in the release bucket, with the release key of its
major version: the same packages, the same inputs, signed again into the release pool, and the same layout as
incoming.

### The stages of the pull requests: references to master's pool

`xcraft publish --dev`, see [the CI of pull requests](pr-ci-design.md), publishes the development builds only, signed
by the PR key, as files of the stage, not in a pool: their counter is the run number, so they are signed and used
once, and removing the stage removes all a pull request wrote. The final packages, master's builds and the upstream
ones, are referred to in the metadata of the stage at their path in the pool of the ci bucket, with the `xml:base` of
their location, signed by the CI key: nothing is copied nor uploaded for them. dnf checks them against the SHA-256 of
the metadata, signed by the PR key. The testers trust the CI key and the PR key. A final package the ci pool does not
have, master not having published it, is signed by the PR key into the stage, as the development builds.

Such a stage cannot be mirrored, and breaks once the ci pool loses the packages it refers to: the pull requests are
temporary, and so are their stages. The ISOs, made from the files, download the referred packages.

### Removing what nothing refers to

What a stage has is copied from its pool, and Garage stores the blocks of a copy once: a pool entry also in a stage
costs nothing, and what costs is what only a pool has. A pool entry is kept while a stage refers to it, by a copy of
its file or by a reference:

- **The stages of the pull requests** are removed when they close, with `xcraft unpublish`, and leave nothing
  behind, their development builds being theirs only. They are the frequent removals, and remove nothing from a
  pool.
- **The dated snapshots of incoming** are what makes a pool grow: one by push of master, each with the builds of its
  changes. Kept forever, they would keep every pool entry. They expire instead, after a retention, like 90 days,
  but the latest and those of the releases, which are published from them.
- **The pools** are then pruned by their bucket's writer, by a command run with the publications, or on a schedule:
  it marks the entries the stages of its bucket have, by the SHA-256 of their manifests, and those the stages of the
  pull requests refer to, in the dev bucket, by the locations of their metadata, readable by anyone, and removes the
  others older than a day: a publication or a pull request referring to an entry it has just signed is not
  disturbed. The release pool is never pruned while the releases are kept.

A pull request referring to an entry pruned before its stage was published, from a fork point older than the
retention, publishes again with that package signed into its stage, as one the ci pool does not have.

### The keys and the buckets

| Bucket | Writers | Keys, in their pools |
|---|---|---|
| ci | the CI of master | the CI key |
| release | the release manager, or the signing host | a release key by major version |
| dev | the developers, the CI of the pull requests | their keys, the PR key |

The trust of xcraft follows: the build keys of a bucket are those of its pools. A release key signs only the trees
of its major version, `repos/<release>/` and its pool: to bind, by `[publish] release`.

## Open questions

- The pool of the ci bucket after a full rebuild: emptied with the bucket today, which breaks the stages of the open
  pull requests, as they are temporary; or kept, by content.
- The retention of the dated snapshots of incoming, and whether the other stages of the ci bucket have one.
- The binding of the release keys to their major versions in `xcraft.toml`, and whether the CI key is by major version
  too.
- Whether a repository of all the versions of the pool, a flat repository, is published for the hosts, to install an
  older version; the pool makes it cheap, as metadata only.
- The public URL of the ci bucket, for the references of the pull requests: in `[remote.main]`.
- The incoming stage of aarch64, which is not published yet: the pull requests need it, and the releases.

## Order

1. The pool, replacing `.xcraft/signed/`, with the stages copied from it: no change for the hosts.
2. The signature of the upstream packages, with the keys of the ISOs and of the hosts reduced to ours.
3. `xcraft publish --dev`, referring to the pool of the ci bucket, `xcraft unpublish`, then the CI of the pull
   requests.
4. The expiry of the dated snapshots of incoming, and the pruning of the pools.
5. The release bucket and the release keys by major version.
