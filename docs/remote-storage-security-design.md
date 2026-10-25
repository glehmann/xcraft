# Design: securing the remote storage

Status: proposal.

## The problem

The remote storage is one Garage bucket, `xcraft`, behind Caddy: public reads over HTTPS, and S3 writes with a key
which can write all of it. The developers push sources with it, and CI pushes everything:

| Prefix | Pushed by | Checked on download against |
|---|---|---|
| `sources/<sha256>/<file>` | developers, CI | the `sources` manifest of the package, committed and reviewed |
| `builds/<arch>/<pkg>/<key>/` | CI | the `manifest.json` of the tree, from the same storage |
| `ccache/<arch>/<pkg>/` | CI | the `manifest.json` of the tree, from the same storage |
| `signed/<key>/<arch>/<pkg>/<key>/` | xcraft publish | the `manifest.json` of the tree, from the same storage |
| `repos/<release>/…`, `isos/<release>/…` | xcraft publish | nothing: the snapshots are for the hosts |
| `index.html`, in each directory | each push | nothing: they are for the browsers |

Garage gives the rights of a key by bucket, not by prefix. So whoever can push sources can also:

- **replace a build**, with its manifest: the build keys are hashes of the inputs of the builds, not of what they
  made, so nothing tells the build is not the one CI made, and everyone fetching it, CI and the developers, uses it, and
  xcraft publish signs it with the release key;
- **replace a compiler cache**: its objects go as they are into the next build of the package, which CI then trusts
  and pushes;
- **replace the files of a snapshot**: xcraft publish copies those of the latest snapshot into the next one, by the
  SHA-256 of its manifest, which the same storage gives;
- **delete anything**: the builds can be made again, but a source archive whose URL is gone only exists there.

The sources are the only part checked against what the repository has: a replaced archive fails the builds, but cannot
be built.

## Goals

- Only CI can change what the builds, the compiler caches and the snapshots get from the storage, and xcraft can tell
  when something else did.
- The developers can still share the source archives of their changes, without any right on the rest.
- The trust comes from the repository, reviewed, and CI's secrets: not from the storage.

Not a goal: a CI which is itself compromised, which can sign what it makes. Its secrets are what this design protects
the storage with.

## Proposal

### Two buckets, two kinds of keys

| Bucket | Has | Written by | Read by |
|---|---|---|---|
| `xcraft` | everything of today: sources, builds, compiler caches, signed builds, snapshots, ISOs, pages | CI only, with `xcraft-ci` | everyone, publicly |
| `xcraft-incoming` | the source archives the developers push, not checked yet: `sources/<sha256>/<file>` | the developers, with a key each, and CI | CI, and everyone, publicly |

A key by developer, rather than one shared: one can be removed alone, and Garage tells which key wrote what.

Caddy serves the public reads of `xcraft-incoming` under `/incoming/`, like those of `xcraft` under `/`, and passes the
signed S3 requests to Garage, which checks the rights.

### The source archives

1. A developer changing an archive, like with xcraft bump --version, runs `xcraft push sources`, which uploads it to
   `xcraft-incoming`: their key can write nothing else.
2. CI, on the pull request and on the push, runs `xcraft push sources`, which takes each archive the committed
   `sources` manifests list, and the main bucket misses, from, in order:
   - `xcraft-incoming`, checked against the SHA-256 of the manifest, then copied server-side into `xcraft`;
   - the URL of the spec, like xcraft fetch does, checked, then uploaded.

   An archive found nowhere, or not matching, fails the job: the pull request cannot be merged before its archives are
   promoted.
3. The builds of a pull request, before the promotion, get the archives from `xcraft-incoming` too: xcraft looks for
   an archive in the cache, `xcraft`, `xcraft-incoming`, then the URL of the spec, checked each time.

A developer can only replace or delete archives of `xcraft-incoming`, which nothing uses unchecked: at worst, a pull
request waits for its archives to be pushed again. The archives promoted can be deleted from `xcraft-incoming` by CI,
or by an expiration of the bucket.

### Signed trees

CI signs the manifest of each tree it pushes: the builds, the compiler caches, the signed builds and the snapshots.
xcraft checks the signature before using a tree, and the files against the manifest, as it already does.

- **The signature** is a detached gpg signature, `manifest.json.asc`, next to `manifest.json`, of the path of the tree
  followed by the manifest: a signed manifest moved under another path, like the key of another build, does not
  verify.
- **The verification keys** are trusted public keys in `xcraft.toml`, by fingerprint:

  ```toml
  [remote]
  build_keys = ["<fingerprint>"]   # trusted public keys, all verify
  ```

  The public keys are in the storage, `keys/<fingerprint>.asc`, which is safe: xcraft checks their fingerprints.
  Several keys allow rotation: retain the old fingerprints until their trees are not used anymore. Order is irrelevant.
  A checkout also trusts its own developer key, without adding it to this list.
- **The private key** is a secret of the repository's CI, apart from the S3 credentials: the storage alone is not enough
  to forge a tree. CI selects the same key for uploads and publishing with `--key <fingerprint>`, through the host GPG
  agent. `[sign]` remains an optional default; without either, a developer uses the checkout's developer key.
- **Where xcraft verifies:** xcraft fetch and xcraft build, for the builds and the compiler caches, and xcraft publish,
  for the builds it signs with the release key and the snapshots it copies files from. A tree without a valid
  signature is as if the storage did not have it: it is built again, or fails to publish.

### The private key in CI

CI uses the same key for uploads and publishing through its host GPG agent, either in an HSM or imported from a
protected CI secret into a temporary keyring. Pass its fingerprint with `--key` to `push`, `push builds`, `push ccache`
and `publish`; the workflow does not modify `xcraft.toml`. Set `GNUPGHOME` when `[sign].gnupghome` is omitted,
unlock the agent before signing if the key has
a passphrase, and kill the agent and remove the temporary keyring at job exit. Only trusted jobs receive the private
key and S3 write credentials. See the README's CI signing instructions. Snapshot manifests use the publishing signer,
or the default signing key when package signing is disabled with `--unsigned`.

### What stays as it is

- The sources in `xcraft`: content-addressed, and checked against the committed manifests. CI pushes them only, now.
- The snapshots and the ISOs for the hosts: their packages and metadata are signed with the release key, which the
  hosts check.
- The developer's key remains local to a checkout. It can sign trees in a test storage; other checkouts do not trust
  those trees unless its public fingerprint is explicitly configured.

## What it does not cover

- **A compromised CI**: it has access to the release key and the S3 credentials of `xcraft`. Who can change the workflows can
  get them: the protection of the branches and the review of the workflows matter as much.
- **Deletions by CI's key**: Garage seems to have neither versioning nor objects which cannot be deleted (see below). The
  builds can be made again, and the sources fetched again from their URLs or the developers; a backup of the storage
  would cover the rest, like the archives whose URL is gone.
- **The public reads**: anyone can read the storage, as today.

## Migration

1. Add CI's signing fingerprint to `remote.build_keys`, provision its private key, and pass `--key` in the workflows.
2. Sign the trees already in the storage, once, from CI: their files are checked against their manifests first.
3. Create `xcraft-incoming`, the keys of the developers, and the route of Caddy; remove the rights of the shared key on
   `xcraft`, and make it CI's only.
4. Release xcraft with the verification, and the push of the sources to `xcraft-incoming`.

Until the step 2, xcraft would accept unsigned trees with a warning, and refuse them after: a setting, or a version of
xcraft, makes the switch.

## Open questions

- Expiration in Garage: its lifecycle support, for emptying `xcraft-incoming` without CI, to check.
- Versioning and object locks in Garage, which would make deletions recoverable: to confirm they are missing.
