# Remote storage

The source archives are not in git, and builds take time: both can be shared with an S3 storage, read publicly over
http(s) and written with credentials:

```toml
[remote.main]
url = "https://xcraft.example/"           # public reads, like the website endpoint of a Garage bucket
s3_endpoint = "https://s3.example/"       # authenticated writes
bucket = "xcraft"
region = "garage"
build_keys = ["<full GPG fingerprint>"] # trusted public keys for verifying trees

[remote.dev]
url = "https://xcraft-dev.example/"
s3_endpoint = "https://s3.example/"
bucket = "xcraft-dev"
region = "garage"
```

The writes use the credentials of `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The top of the storage only has
the published repositories, in `repos/`, the published ISOs, in `isos/`, and what xcraft keeps for itself, in
`.xcraft/`, like the local state of a checkout: `.xcraft/sources/<sha256>/<filename>`,
`.xcraft/builds/<arch>/<package>/<key>+b<n>/`, with a `manifest.asc` of the build's files, and the signed builds,
the compiler caches, the resolutions, the public keys and the ISOs of the checkouts, in `.xcraft/signed/`,
`.xcraft/ccache/`, `.xcraft/resolved/`, `.xcraft/keys/` and `.xcraft/isos/`.

- When an archive of a manifest is not in the source cache, the build, or `xcraft fetch sources`, downloads it, from
  the storage, or else from the source of the spec whose URL has the name of the archive, and checks it against the
  manifest. So anyone can push sources: a bad archive cannot match the sha256 of the manifest, which is in git.
  `xcraft import` does not push: with `xcraft push sources`, the developer shares the archives with their change, and
  CI runs `xcraft fetch sources` to fetch and verify sources, then `xcraft push --to main` to upload the cached files to the main
  bucket. Push never fetches missing sources: it fails if an archive to upload is absent from the local cache.
  `xcraft push sources --to main --check` checks the main bucket and needs no credentials.
- When a package is not built locally, the build downloads its build with the same key, when the storage has it: the
  key covers everything the build depends on, so it is the same everywhere for the same commit. A build is a binary
  people install, so only CI should push builds, after `xcraft build --locked`, which fails when a build would
  change the lock, and so its key: `xcraft push builds`. Development builds are never pushed, nor fetched: `push
  builds` and `fetch builds` skip the packages with ones, unless `--final`. With `--fetch=needed`, CI
  leaves in the storage the builds it has, only fetching those of the dependencies of the packages it builds.
- A build without the compiler cache of its package downloads the one of the storage first, unless `--fetch=none`: the
  one CI pushes after its builds, with `xcraft push ccache`, so that rebuilding a modified package, like the kernel
  with a patch, mostly finds its objects. The cache of a package is a signed tree, `.xcraft/ccache/<arch>/<package>/`,
  with an object by entry, rather than read by ccache from the storage, as the builds run without the network. `xcraft
  fetch ccache` adds to the local cache the entries it does not have. `xcraft push ccache` only keeps the entries the
  last successful build of the package used or made, which ccache dates of their last use: not those of the older
  builds, which the local cache keeps. The local cache is compressed again first, in place, from zstd's fast level 1,
  ccache's, to level 9, `ccache --recompress`: for the kernel, 15% less to transfer, in 20s on 8 CPUs, once, as it
  stays so. Then only the entries the storage does not have are uploaded, 64 at once. See
  [the design](ccache-sync-design.md).
- The builds running dune, like those of xapi, have the cache of dune of their package, `.xcraft/dune-cache/<package>/`,
  on this machine only: rebuilding the package after a change, or with another release, takes again what its rules
  made before. For xapi, it halves its builds, from 2m40s to 1m24s on 8 CPUs, with 2.7 GB of cache. All the rules are
  cached, those of the project too, as dune's default, `enabled-except-user-rules`, caches none of xapi's: one missing
  a dependency may get the output of a previous build, which `xcraft clean --dune-cache` forgets. It is not in the
  recipe of the builds, which it does not change, nor in the remote storage: the builds of CI never get it.
- A resolution not in the local cache is downloaded from the storage, when it has it, signed by a trusted key, rather
  than resolved: the image and the build roots of each architecture, by the hash of what they are resolved from, the
  same everywhere, which CI pushes with `xcraft push`. So a new checkout of what CI built resolves nothing, without
  the metadata of the upstream repositories. A storage failing is shown once, and the command resolves instead.

Without network, or when the storage is unavailable, everything still works with what is local.

Developers have individual S3 keys with rights only on `xcraft-dev`. CI has a separate key with read/write rights on
the main bucket. Both buckets allow public reads and have independent configured URLs. Source downloads check the
local cache, main bucket, developer bucket, then the spec URL against the committed manifest. Either bucket can be
omitted. Uploads to an unconfigured bucket fail with a configuration error.

`xcraft push` and `xcraft publish` accept `--to main|dev`, defaulting to `dev`. This applies to their subcommands too.
CI uses `--to main`; developer commands use their own bucket. Builds and compiler caches are fetched from main first,
then dev if main is absent, unavailable or invalid. Both locations use the same configured allowed signing keys and
the checkout's own developer key. Publishing to dev uses the developer's signing key unless `--key` selects another.

Build and compiler cache uploads, publishing and `xcraft iso` accept `--key <fingerprint>` to select a host GPG key.
Without it, they use `[sign]` when configured, or the checkout's developer key made with `xcraft key create`.
The keyring is `[sign].gnupghome`, or `GNUPGHOME` when it is omitted. Publishing uses its selected signer for signed
builds and snapshot manifests. `--unsigned` skips package signing but remote manifests still need a default signing
key; it cannot be combined with `--key` or `--dev-key`.
`remote.main.build_keys` only lists trusted public keys for verification; its order does not select the signing key.
The checkout also trusts its own developer key, without any configuration change.
Public keys are uploaded to `.xcraft/keys/<fingerprint>.asc` and checked against the configured
fingerprints. Each `manifest.asc` is signed in clear: its text is the path of the tree's files, then a line by file
of the tree, sorted, as `sha256sum` writes them, so that `gpg --decrypt manifest.asc | tail -n +2 | sha256sum -c`
checks a downloaded tree.
The trees made once, like the builds, the signed builds, the resolutions and the ISOs, have their files in a
directory of their own, their generation, `<tree>/<sha256>/`, named by the SHA-256 of the tree path and its file
lines: machines uploading the same tree with other files, like two builds of the same key, never write over each
other's files, and the tree is as its last manifest says, for the readers of either manifest. A tree whose upload is
interrupted before its manifest is as it was. The snapshots and the compiler caches, updated in place, and the trees
of older versions of xcraft, have their files in the tree itself.
So the uploads of the builds, the signed builds, the resolutions and the ISOs can run on many machines at once, while
the publishes of a release and stage must not: they copy into its `latest` snapshot, and its dated ones, in place,
which CI must serialize, one publish of a stage at a time. The compiler caches of a package keep the entries of its
last push, whichever ran last, each checked against its manifest when fetched.
Only the text gpg verified is read.
Missing or invalid signatures make a remote tree unavailable. There is no unsigned-tree fallback.
Only the manifest and public keys are downloaded before verifying the signer and signature. Build
files and compiler-cache tarballs are downloaded only after verification, then checked against the signed digests.

To rotate CI keys, change the fingerprint passed with `--key`, add it to `remote.main.build_keys`, and retain the old
fingerprints while their trees are still needed.
`xcraft push --to main` uploads cached sources and builds to main storage, so it requires CI's credentials and a signing key.

## The signing key in CI

Builds, ccache uploads and publishing use the same private key through the runner's GPG agent. A persistent signing
runner can use an HSM or an existing host keyring, just as publishing does. The build containers receive neither the
private key nor the agent socket. Only the separate RPM-signing container gets the agent's restricted socket.

For an ephemeral runner, store the armored release private key in a protected CI secret and import it into a temporary
keyring before pushing or publishing. Keep only `remote.main.build_keys` in the committed configuration; CI selects the
private key with `--key`. With `[sign].gnupghome` omitted, a CI shell step can use:

```sh
export GNUPGHOME="$(mktemp -d /tmp/xcraft-gnupg.XXXXXX)"
chmod 700 "$GNUPGHOME"
trap 'gpgconf --kill gpg-agent; rm -rf "$GNUPGHOME"' EXIT
gpg --batch --import "$CI_SIGNING_KEY_FILE"
xcraft fetch sources
xcraft push --to main --key "$SIGNING_KEY_FINGERPRINT"
xcraft push ccache --to main --key "$SIGNING_KEY_FINGERPRINT"
xcraft publish --to main --key "$SIGNING_KEY_FINGERPRINT"
```

`CI_SIGNING_KEY_FILE` is a CI-provided secret file; `SIGNING_KEY_FINGERPRINT` is a public CI variable. Neither requires
changing `xcraft.toml` in CI. If `[sign].gnupghome` is set, import into that
keyring instead. A passphrase-protected key needs its GPG agent unlocked before these commands, with a CI secret and
`gpg-preset-passphrase` on a runner configured with `allow-preset-passphrase`. xcraft uses batch GPG and does not prompt
for a passphrase. Give the key and S3 write credentials only to trusted jobs; pull-request builds can fetch and build
without them. Disable shell tracing while handling secrets.

The storage does not list its directories: xcraft writes the `index.html` page of each directory it changes, and of
their parents, to browse it. `xcraft push index` makes all of them, for what was not written by xcraft. A project shows
its logo at their top, as its `logo` in `xcraft.toml`, when it has one: a link, not a copy in the storage.

For development, `local-s3.sh` runs such a storage locally, with Garage in a container, and the `xcraft.toml` of xcp-ng-dist uses
it:

```
./local-s3.sh up                  # set it up, or start it again, and show the credentials to export
eval "$(./local-s3.sh env)"       # export them, to push
./local-s3.sh status              # whether it runs, and what it stores
./local-s3.sh down                # destroy it, with its data
```

It keeps its data in a podman volume, and its configuration in a podman secret, until `down`. Its public reads use
the name `xcraft.web.garage.localhost`, which resolves to the local host on most systems.
Set `[remote.dev].bucket` to `xcraft-dev` and its `url` to `http://xcraft-dev.web.garage.localhost:3902` for this server.
Use `eval "$(./local-s3.sh env ci)"` for the CI credentials; the default credentials can write the developer bucket only.
