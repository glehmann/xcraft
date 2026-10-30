# Design: syncing the compiler caches entry by entry

Status: proposed, not implemented.

## The problem

Each package has its own ccache directory, which its builds fill. CI shares it through the remote storage, and a build
without a local compiler cache fetches it first, for a rebuild of a modified package, like the kernel with a patch, to
find most of its objects: see `xcraft/ccache.py`.

It is shared whole, as one archive, `.xcraft/ccache/<arch>/<pkg>/<pkg>.ccache`, a tarball in a signed tree:

- `xcraft push ccache` takes the entries the last successful build of the package used or made, those ccache dates
  after its start, hard-links them into a staging directory, compresses them again with zstd at level 19, in a ccache
  container, packs them, and uploads the archive, replacing the one of the storage;
- `xcraft fetch ccache`, or a build without a local cache, downloads the archive, checks it against the signed
  manifest of its tree, and replaces the local cache with it.

So each push of the kernel's cache compresses again and uploads all of it, even after a patch changing a few objects.
Its cache has 19,872 entries, of ~106 kB on average locally: ~2.1 GB, ~10,000 results and ~10,000 ccache manifests.
The time is in the compression at level 19 of all the entries, and in the upload of the whole archive.

## Goals

- Push only what the storage does not have, and compress again only that.
- Fetch only what the local cache does not have.
- Keep the trust of the signed trees: nothing the storage serves is used before its signature is checked against the
  keys of `xcraft.toml`, and what a storage writer without the key of CI changes can only cost cache misses.
- Keep the fetches safe while CI pushes, and the local builds safe while xcraft fetches.
- No new tool on the runners and the developers' machines.

## Proposal

### One object by entry

The entries are objects of their own, in the tree of the cache, with its signed manifest:

```text
.xcraft/ccache/<arch>/<pkg>/manifest.asc             signed by CI: a <sha256>  <entry> line by entry
.xcraft/ccache/<arch>/<pkg>/<d>/<dd>/<hash>R         a result: what the compilation made
.xcraft/ccache/<arch>/<pkg>/<d>/<dd>/<hash>M         a ccache manifest: the results of a source, by included files
```

The paths are those of the ccache directory, but its configuration, its temporary files, its statistics, and when the
last build started: `_LOCAL` and the `stats` files, as now.

rsync would need ssh or an rsync daemon on the server, where CI only has S3 credentials. rclone speaks S3, but it is
one more tool to install, and it does not know the signed manifests, which xcraft would write and check around it. The
S3 client of xcraft, boto3, does what is needed, with the concurrent transfers it already has.

### What is new

ccache names a result by the hash of its inputs: a result of a name is always valid for it, whatever its bytes,
which another compression level or ccache version changes. A ccache manifest, though, changes in place: ccache adds to
it the results of the new combinations of included files it sees.

So an entry is uploaded when:

- **a result**: its name is not in the manifest of the storage;
- **a ccache manifest**: its name is not in the manifest of the storage, or its local bytes have another SHA-256.

CI starts from the fetched cache, which has the bytes of the storage: the ccache manifests the build did not change
keep the SHA-256 of the manifest of the storage, and are not uploaded. A developer's push of a fetched cache uploads
nothing either.

A result the storage has is never uploaded again, even when the local one has other bytes: the one of the storage is
kept, with its SHA-256, in the new manifest.

### Push

1. Read the manifest of the cache in the storage, checking its signature against the trusted keys. Without one, like
   for the first push, or untrusted, everything is new.
2. Split the entries the last build used or made, those ccache dates after its start, in those the storage has, and
   the new ones, as above.
3. Hard-link only the new ones into the staging directory, and compress them again with `ccache --recompress 19`
   there: ccache only compresses what it finds there, and replaces the files, so the local cache keeps its own.
4. Upload them, concurrently, with `remote.transfer()`.
5. Write the new signed manifest: the entries the storage has, with their SHA-256 there, and the new ones, with
   their SHA-256 once compressed again.
6. Delete the entries of the storage the new manifest does not have: those the last build did not use.

The manifest is written after the entries it lists, and the entries are deleted after the manifest which does not
list them anymore, like for the trees of the builds, so that a manifest of the storage always lists entries the
storage has, but those deleted under a reader, below.

### Fetch

1. Read the manifest of the cache in the storage, checking its signature, before anything else: an unsigned or
   forged manifest makes no entry downloaded.
2. Download, concurrently, the entries it lists that the local cache does not have with the same SHA-256, each in a
   temporary file next to its place, checked against the manifest, then renamed into the cache: ccache writes its
   files that way too, so a build running meanwhile sees either nothing or the whole entry.
3. An entry missing, or with another SHA-256, is skipped: a cache miss, not an error.

The fetch adds to the local cache, rather than replacing it: the local entries the storage does not have are kept,
and ccache's size limit removes the oldest. A fetch interrupted leaves a valid cache, which the next one completes.

### A fetch while CI pushes

The reader works from the manifest it checked, a consistent set of entries, while the writer:

- **uploads new entries before its new manifest**: the reader's manifest does not list them;
- **uploads again a ccache manifest changed**: the reader gets the old bytes, or the new ones, which do not match its
  manifest and are skipped;
- **deletes the entries its new manifest does not list**: the reader gets a 404 for those, skipped.

So a push during a fetch can only make it miss entries. Two pushes of the same cache do not happen at once: only the
main branch pushes the caches, and its build workflow runs one at a time by architecture.

### Concurrency

`remote.transfer()` and `download_tree()` already run 16 requests at once, `REQUESTS`, with one S3 client and a pool
of 64 HTTPS connections kept open. For ~20,000 objects of ~100 kB, the time of each request is mostly its round trip:
at 20 to 50 ms, 16 at once make ~300 to 800 objects by second, ~30 s to 1 min for a whole kernel cache, once. A larger
pool for the caches only, like 64 requests, is an option if Garage keeps up.

### The pages

The directory pages stop at the tree of each cache: they list its manifest and its 16 directories, not their ~20,000
entries.

### What changes

- `xcraft/ccache.py`: `push()` and `fetch()` as above; the archive and `archive_name()` go.
- `xcraft/remote.py`: a download of the entries of a tree missing from a local directory, adding to it, and the
  pages stopping at the caches.
- The README and the docstring of `xcraft/ccache.py`, for the new layout.
- The trees of the caches with an archive are not read anymore: the first push of each cache uploads it all, once.

## Before implementing

Time a push of the kernel's cache in CI, from a fetched cache after a rebuild, split in the compression and the
upload, to know what this saves; and, once implemented, the objects by second of a first fetch, to choose the size of
the pool.

## Open questions

- How large is a typical push after a patch of the kernel: how many results and ccache manifests change?
- Does Garage keep up with 64 requests at once for small objects, and does it need more than its defaults?
- Should a developer's fetch skip the ccache manifests it has locally with other bytes, rather than replacing them,
  as they may list results of its own builds?
