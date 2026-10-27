# Design: securing the remote storage

Status: signed trees, independent main and developer buckets, `--to main|dev` and publication stages are implemented.
The current stage layout is described below; flat public RPM-repository URLs remain a separate design question.

The bucket and publication decisions were recovered from the OpenCode session "Implement remote storage security
design", `ses_e8af5dc5affeI2ZtHxpwxsKCO4`. Later discussion adds explicit source fetching and a shared signing key
selected by `--key`.

## The problem

The original remote storage used one Garage bucket, `xcraft`: public reads, and S3 writes with a key which could
write all of it. The developers pushed sources with it, and CI pushed everything:

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
- The developers can upload custom repositories of test packages for others to install on test hosts.
- The trust comes from the repository, reviewed, and CI's secrets: not from the storage.
- Each bucket has its own configured public URL, independent of the other bucket's endpoint.

Not a goal: a CI which is itself compromised, which can sign what it makes. Its secrets are what this design protects
the storage with.

## Proposal

### Two buckets, two kinds of keys

| Bucket | Has | Written by | Read by |
|---|---|---|---|
| `xcraft` | verified sources, builds, compiler caches, signed builds, release snapshots, ISOs, pages | CI only, with `xcraft-ci` | everyone, publicly |
| `xcraft-dev` | source archives from developers, custom test repositories, their public signing keys and pages | developers, with a key each | everyone, publicly, including CI |

A key by developer, rather than one shared: one can be removed alone, and Garage tells which key wrote what.
Developer keys have read/write rights only on `xcraft-dev`. CI has read/write rights on `xcraft`. It downloads sources
from the public URL of `xcraft-dev` and does not need S3 permissions on the developer bucket.

The buckets have independent public endpoints, for example Garage's website endpoints on separate hostnames.
Their S3 API endpoint can be the same Garage server. Garage checks S3 credentials and bucket permissions directly.
No additional service or shared URL path is required.

### Configuration

```toml
[remote.main]
url = "https://xcraft.storage.example/"
s3_endpoint = "https://s3.storage.example/"
bucket = "xcraft"
region = "garage"
build_keys = ["<CI signing fingerprint>"] # trusted public keys for verifying trees

[remote.dev]
url = "https://xcraft-dev.storage.example/"
s3_endpoint = "https://s3.storage.example/"
bucket = "xcraft-dev"
region = "garage"
```

Both public URLs are explicit. The developer bucket is optional for projects which only consume CI's storage;
without it, source downloads use the main bucket and spec URLs, and developer uploads fail with a configuration
error. Neither bucket's URL or name is derived from the other.

Each command uses the S3 credentials of its caller, from the environment. The two buckets can use independent S3
services. `build_keys` belongs only to the trusted remote: a developer upload does not require CI's signing key.

### Upload destination

`push` and `publish`, including their subcommands, accept `--to main|dev`, defaulting to `dev`:

```sh
xcraft push sources                       # developer sources
xcraft push builds                        # developer-signed builds in the developer bucket
xcraft publish                            # developer-signed snapshot in the developer bucket
xcraft fetch sources                      # explicit download and verification
xcraft push --to main --key <fingerprint>  # CI sources and builds
xcraft push ccache --to main --key <fingerprint>
xcraft publish --to main --key <fingerprint>
```

The destination never depends on detecting CI or probing credential permissions. Destination selection does not
change download preference: reusable builds and compiler caches try main before dev. The configured allowed signing
keys and the checkout's own developer key are accepted in either bucket. Missing trees, unavailable buckets, invalid
signatures and corrupted payloads allow trying the other bucket. Sources check both buckets against committed hashes.

### The source archives

1. A developer changing an archive, like with xcraft bump --version, runs `xcraft push sources`, which uploads it to
   `xcraft-dev`, under `sources/<sha256>/<file>`: their key cannot write the main bucket.
2. CI runs `xcraft fetch sources` to download the archives listed in the committed `sources` manifests and check their
   SHA-256. In trusted jobs, `xcraft push --to main` uploads the checked local archives missing from the main bucket, then the
   builds. Push never fetches sources; it fails if an archive to upload is missing from the local cache.
3. All source downloads, including pull-request builds, look in the cache, `xcraft`, `xcraft-dev`, then the URL of the
   spec. Each archive is checked against the committed SHA-256 before use. An archive found nowhere, or not matching,
   fails the fetch or build.

A developer can replace or delete archives of `xcraft-dev`, which nothing uses unchecked: at worst, a pull request
waits for its archives to be pushed again. Uploaded archives remain available from `xcraft`, independently of what
happens to the developer copy. Optional expiration must target `sources/` only, not the whole developer bucket.

### Custom test repositories

Developer publishing uses `xcraft publish --to dev`, which is also the default. The snapshot follows the same
assembly and upload process as official publishing, with the developer's signing key for packages, metadata and
tree manifests, unless `--key` selects another. Its private key stays local. The public key accompanies the snapshot.
Local test repositories made by `xcraft repo` remain available for test builds.

The developer and repository names organize uploads and avoid accidental name collisions. They do not isolate
developers: Garage grants rights by bucket, so any developer key can replace or delete any file in `xcraft-dev`.
Test hosts must explicitly install the intended test repository and trust its developer key. The public key being
next to a repository is not itself proof of who published it.

The developer bucket can also hold reusable builds and compiler caches. Their bucket grants no trust: xcraft verifies
their manifest against the configured signing fingerprints or the checkout's own developer key before downloading
payloads. A custom repository does not automatically become a trusted build or a release repository.

### Publication stages

The current interface distinguishes storage from publication stage:

- `--to main|dev` selects a bucket and defaults to `dev`.
- `--stage <name>` selects the publication subdirectory for repositories and ISOs. It defaults to the OS user name,
  so developers do not overwrite each other's `latest` snapshots unless they explicitly select the same stage.
- The master workflow publishes the `incoming` stage to main for x86 only while arm64 builds are broken.
- Developer stages allow custom names, such as `kernel-fix`. A stage is organizational, not an access-control boundary.

The latest repository and ISO URLs are:

```text
/repos/9.0/incoming/latest/base/<arch>/
/repos/9.0/incoming/latest/base/Source/
/isos/9.0/incoming/latest/<arch>/

# Independent developer bucket
/repos/9.0/<username>/latest/base/<arch>/
/isos/9.0/<username>/latest/<arch>/
```

Each stage also retains dated snapshots under the same parent, with an independent suffix sequence. Signatures bind
manifests to the complete path, including stage. The earlier proposed flat XCP-ng repository layout and `--repo`
option are superseded here by the requested stage layout; flattening public URLs is not part of this implementation.

### Signed trees

CI signs the manifest of each tree it pushes: the builds, the compiler caches, the signed builds and the snapshots.
xcraft checks the signature before using a tree, and the files against the manifest, as it already does.
It downloads only the manifest, signature and candidate public keys before verifying them. The manifest includes the
tree path in its signed payload, and the public keys must match locally configured fingerprints. An unknown signer
or invalid signature must not trigger any build-file or compiler-cache payload downloads.

- **The signature** is a detached gpg signature, `manifest.json.asc`, next to `manifest.json`, of the path of the tree
  in UTF-8, a newline, then the exact manifest bytes: a signed manifest moved under another path, like the key of
  another build, does not verify.
- **The verification keys** are trusted public keys in `xcraft.toml`, by fingerprint:

  ```toml
  [remote.main]
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
protected CI secret into a temporary keyring. Pass `--to main` and its fingerprint with `--key` to `push`, `push builds`,
`push ccache` and `publish`; the workflow does not modify `xcraft.toml`. Set `GNUPGHOME` when `[sign].gnupghome` is omitted,
unlock the agent before signing if the key has
a passphrase, and kill the agent and remove the temporary keyring at job exit. Only trusted jobs receive the private
key and S3 write credentials. See the README's CI signing instructions. Snapshot manifests use the publishing signer,
or the default signing key when package signing is disabled with `--unsigned`.

### What stays as it is

- The sources in `xcraft`: content-addressed, and checked against the committed manifests. CI pushes them only, now.
- The snapshots and the ISOs for the hosts: their packages and metadata are signed with the release key, which the
  hosts check.
- The developer's private key, local to a checkout, for test builds. Only its public key accompanies uploaded test
  repositories. It can also sign trees in a test storage; other checkouts do not trust those trees unless its public
  fingerprint is explicitly configured.

## What it does not cover

- **A compromised CI**: it has access to the release key and the S3 credentials of `xcraft`. Who can change the workflows can
  get them: the protection of the branches and the review of the workflows matter as much.
- **Deletions by CI's key**: Garage seems to have neither versioning nor objects which cannot be deleted (see below). The
  builds can be made again, and the sources fetched again from their URLs or the developers; a backup of the storage
  would cover the rest, like the archives whose URL is gone.
- **The public reads**: anyone can read the storage, as today.

## Open questions

- Whether a future flat public RPM-repository layout should coexist with the stage snapshot URLs.
- Whether testing and base receive whole tested snapshots or independently selected packages.
- Expiration in Garage: its lifecycle support for source archives, independently of test repositories, to check.
- Versioning and object locks in Garage, which would make deletions recoverable: to confirm they are missing.
