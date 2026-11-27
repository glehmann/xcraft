# Publishing and signing

## Publishing

`xcraft publish` makes a snapshot of the distribution, for all the architectures: all our packages, and the upstream
ones they need at runtime, in the remote storage, laid out like the repositories of XCP-ng 8.3:

```
repos/<release>/<stage>/<date>[.<n>]/base/<arch>/   our RPMs and upstream packages, with repodata
repos/<release>/<stage>/<date>[.<n>]/base/Source/   the source RPMs of all architectures
repos/<release>/<stage>/<date>[.<n>]/build/<arch>/  upstream build dependencies absent from base/<arch>/
repos/<release>/<stage>/<date>[.<n>]/build/Source/  their source RPMs
repos/<release>/<stage>/latest/...                 a copy of this stage's latest snapshot
isos/<release>/<stage>/<date>[.<n>]/                the packages and netinstall ISOs of all the architectures,
                                                    with signatures (.asc), and their SHA256SUMS, signed too
isos/<release>/<stage>/latest/...                  a copy of this stage's latest ISOs
```

`<release>` is `[publish] release` of `xcraft.toml`, like `9.0`.
`--stage` selects the stage subdirectory for both repositories and ISOs. It defaults to the current OS user name,
keeping developer publications separate, and can also be set as `[publish] stage`. Each stage has its own dated
snapshots and `latest`; publishing one stage does not replace another. For example:

```sh
xcraft publish --to main --stage incoming --arch x86_64_v2 --key <fingerprint>
xcraft publish --stage kernel-fix  # developer bucket, custom stage
```

A stage keeps its dated snapshots for the life of the release, unless `[publish.stages.<stage>] snapshot_days` gives
how long: each publication to it then removes those of its repositories and ISOs published more than that many days
ago, but the latest one of each, by the dates of their names.

```toml
[publish.stages.incoming]
snapshot_days = 90
```

A stage with a `retention` is cumulative: its `latest` has the packages of its latest snapshot, and the older
versions it published, with metadata over all of them, for `dnf history rollback`, `undo` and `downgrade` on the
hosts; dnf updates to the newest ones, those of the latest snapshot. Its dated snapshots stay exact.

```toml
[publish.stages.stable]
retention = { days = 90, keep = 2 }
```

- `days`: how long a version stays once out of the latest snapshot, for the life of the release without it.
- `keep`: the versions always kept, the current one included, of the packages of the latest snapshot, however old:
  a host updating rarely can always go back one version. A package the latest snapshot does not have anymore has the
  time rule only.
- A source package stays with the last binary built from it. A package of the snapshot replaces an older one of the
  same name with other contents, which publishing reports.
- When each older version left the latest snapshot is in what the latest snapshot was made from, in `.xcraft/`:
  the retention applies at each publication, the latest snapshot reused too. A publication interrupted, which left
  `latest` without its manifest, loses none of them, and one which cannot read the metadata of an older version
  fails, rather than dropping it. A repository whose packages did not change keeps its metadata.
- The metadata files of the previous `latest` are deleted at the next publication, not at once: a host which has read
  the previous `repomd.xml` still finds them.

Each repository has a metalink, `repodata/metalink.xml`, for the `metalink=` of the hosts' repository files: the URL
of its `repomd.xml` on each server, with its size and SHA-256, and for `latest`, those of its last three previous
ones, for the mirrors a few syncs behind. dnf takes the repository from the first server whose `repomd.xml` matches,
skipping the stale or broken ones. The metalinks of `latest` list the mirrors of the stage first, then our server;
those of the dated snapshots, which the mirrors do not have, our server only. A mirror has the paths of the storage
below its URL:

```toml
[publish.stages.stable]
mirrors = ["https://mirror.example.org/xcp-ng/"]
```

The mirrors sync by rsync from a copy of the latest snapshots of the stage, its repositories and ISOs, which
`xcraft mirror DIR --stage s` makes on the primary mirror host, after each publication and periodically, an rsync
module serving `DIR`: Garage has no rsync. It reads the public endpoint of the storage, without S3 credentials, checks
the signed manifest of each latest snapshot, then makes a new version of the copy, switched at once by a symlink, at
the paths of the storage: an rsync session sees one complete tree. A latest snapshot published again during the copy,
its manifest dated otherwise after it than before, fails it, without switching: the next run makes it again. A file
has the date of the storage, its Last-Modified, and the modes 0644 and 0755, so a copy made again from scratch, on a
new host, is the same for rsync, and the mirrors transfer nothing again; a file of the previous version, the same, is
hard linked rather than downloaded. `--check` checks the copy against its manifests, downloads the damaged files
again, and reports those the manifests do not have; without xcraft, a mirror checks its own copy with the public keys
of the storage:

```sh
gpgv --keyring <keys> manifest.asc && gpg --decrypt manifest.asc | tail -n +2 | sha256sum -c --quiet
```

The mirrors sync with `rsync -a --delay-updates --delete-after`: the files changed are put in place together, at the
end.

- Our packages are built or fetched, but only fetched for the architectures the host emulates: those are built by
  native builders, which push them. The publish fails when the remote storage misses one of their builds. With
  `--fetch-only`, all of them are only fetched, and the ISOs only taken from the storage, pushed: a promotion, which
  publishes to a stage the commit another one published, builds nothing, and fails rather than build what the storage
  misses, like the builds pruned since.

- What our packages need at runtime is in the lock, in `runtime`, pinned like the build roots: dnf resolves it with
  them, before building, for all the architectures, for each of our binary packages alone, on an empty system,
  without weak dependencies, from their specs and what their builds found. Publishing only gets those pinned
  packages, checked against their SHA-256, and fails while some of ours cannot be installed: a snapshot only changes
  with the lock, and `xcraft lock --update` updates these packages too, for security fixes. `[publish] extra` in
  `xcraft.toml` adds upstream packages for convenience, like `mc`, with what they need, and `[iso] installer` those
  of the installer.
- The source RPMs of the upstream packages come from the AlmaLinux vaults, which keep all versions (the
  `source_baseurl` of the repositories): those of the binaries' metadata, as some binaries do not have the versions
  of their sources.
- The upstream RPMs and source RPMs must be signed with the keys of the repositories, those of the build
  environment, as dnf checks them when installing but not when downloading. The RPMs must also match the sha256 of
  the metadata of their repositories.
- Each package of the snapshot must install from the packages of its architecture alone: a self-contained repository.
- The build repository has what rebuilding our packages needs, with `base`: the packages of the build environment
  image and of the build roots, as resolved, but the upstream ones ours replace, like `kernel-headers`. Each of its
  packages must install from it and `base`. `[publish] build = false` in `xcraft.toml` leaves it out, and so does
  `xcraft iso`, which does not need it.
- Our source RPMs are those of the first architecture: built from the same sources, those of the others differ by
  their build times only.
- The ISOs are made like `xcraft iso` makes them, for the architectures with ISOs, x86_64 and aarch64, when `[iso]
  installer` is configured: those cached, by `xcraft iso` with the same key, or in the remote storage, by `xcraft push
  isos`, are taken rather than made from the snapshot. Those of the architectures the host emulates are only taken,
  never made, as the programs of their packages run to make them: CI makes them on the host of each architecture,
  once built, and pushes them, or, in dev mode, hands their cache over, `.xcraft/cache/isos/<arch>/`, to the host
  publishing. They are signed like the snapshot: their repository, and their files, with a detached signature made
  once with them, by the key their key covers, cached and pushed with them, the same in all the publications, like
  the signatures of the builds. Those pushed are copied in the storage with their signatures, without downloading
  them. Their checksums are in `SHA256SUMS`, as `sha256sum` writes them, with its detached signature, so that
  `sha256sum -c SHA256SUMS` checks a download.
- All snapshots are kept. The storage stores identical files once: our packages are copies of the pushed builds, and
  the upstream ones and the sources of the previous snapshot, so a snapshot only costs what changed.

Publication signs the hashes of the locally checked snapshot. Files are reused by server-side S3 copy only when a
trusted remote manifest has hashes matching the local snapshot, or, for the upstream packages, when the metadata of the
upstream copy, unsigned, has them with their pinned SHA-256, name, version, architecture and file name: forged, it can
only make the publication copy other contents, which the hosts reject, as they check the packages against the
published metadata and their signatures. Those copies are not downloaded again during publication; downloads still
check each payload against its signed manifest.
The `latest` tree only gets the files that changed: its manifest is removed while they are copied, so that a
publication failing then leaves it incomplete, and the next one copies all its files.

A snapshot published to the storage is assembled without the packages the storage has: the latest snapshot's, whose
metadata entries are reused by SHA-256 (the lock's for the upstream binaries, its trusted manifest's for the others),
and our builds signed by the snapshot's key, in `builds/<fingerprint>/`, of which only those missing there are
signed again from those of another key, and pushed. Only the other
packages are downloaded, have their signatures checked and are indexed: those of the storage were when they entered
it. The checks of what our packages need at runtime, and of self-contained repositories, use their metadata only. Our
snapshots' metadata is compressed with gzip; an older snapshot's zstd one is only read from Python 3.14, so the first
publication after it may download everything once. The packages of an ISO's architecture are downloaded only when the
ISO must be made, neither cached nor in the storage.

What the latest snapshot of a stage was made from is in a signed tree of its own,
`.xcraft/inputs/<repos|isos>/<release>/<stage>/latest/inputs.json`, with the files of the snapshot. On a matching
checkout, while the latest snapshot has these files still, `xcraft publish` reuses it by server-side copies into a new
dated snapshot, without downloading RPMs, assembling repositories or rebuilding ISOs. Its input keys cover package
builds, locked upstream packages, assembly recipes, architecture, signing identity and relevant repository/ISO
configuration. Changed inputs take the normal assembly path. Older snapshots need one full publication to acquire the
input record. It has the commit of the publication too, a reused snapshot's as well: `<commit>-dirty` from a checkout
with changes, and with jj, the commit of its working copy, which git sees as changes, or its parent's while it is
empty. `xcraft maint start` makes the maintenance branch of a release from that of its latest stable snapshot, see
[Maintenance branches](builds.md#maintenance-branches).
`--local` and an explicit `--output` still materialize the snapshot locally.

`xcraft publish repos` only publishes the repositories, and `xcraft publish isos` the ISOs, under a snapshot name
used by neither within the selected stage. `xcraft publish --local` only makes the snapshot in `.xcraft/publish/`,
its ISOs in its `iso/`, of one architecture, like the other commands, unless `--arch all` is given.
A snapshot published to the storage is of all the architectures, unless `--arch` is
given: it replaces the latest one, whose other architectures would be gone. In CI, after `xcraft push builds`,
`xcraft publish --locked` fails rather than change the lock.

`xcraft publish --to release --key <release key>` publishes a release, in the release bucket, `[remote.release]`:
the builds of the main bucket, fetched from there, signed again by the release key, into `builds/<release key>/`,
and pushed there, once, like the copies of the upstream packages; the release bucket has nothing else, nothing
signed by another key, and is never read for the builds. With `--fetch-only`, nothing is built: a promotion of what
the main bucket has. Its ISOs, which have the key of their packages, are made for it, natively, by `xcraft iso --final
--key <release key>`, and pushed there by `xcraft push isos --to release`, before the publication, which takes them:

```toml
[remote.release]
url = "https://releases.example/"
s3_endpoint = "https://s3.example/"
bucket = "xcraft-release"
build_keys = ["<fingerprint of the release key>"]
```

`xcraft publish --dev ID` publishes in dev mode, like the CI of a pull request: its snapshot has the development
builds of `ID`, see [development builds](builds.md#development-builds), which are files of the stage only, never
pushed as builds, and the final builds and upstream packages the main bucket has, copied from it server-side, when
publishing to another bucket on the same S3 service, like the dev one, rather than uploaded again. All of them keep
the signatures of their builds, the development ones by the key of the command, like the PR key, master's by CI's:
the stage has the public key of each signer, `RPM-GPG-KEY-<fingerprint>`, next to its own, `RPM-GPG-KEY`, and its ISOs
import them all. Its snapshots are never reused, as each run builds its development builds again.
Each snapshot published to the storage has a `.repo` file for the hosts of each architecture,
`xcraft-<stage>-<arch>.repo`: its repository, with its keys, and in dev mode, those of the repositories of
`xcraft.toml` too, whose packages keep their signatures; the latest snapshot of the stage has those of the last one;
and its contents, `contents.json`, its sets of packages, which `xcraft contents --diff-stage` compares the checkout
with, see contents-design.md. `--summary FILE` writes
in `FILE`, as Markdown, where the snapshot and its ISOs are published, how a host uses its repository, and the links
of the files of its development builds, like for the comment of a pull request.
`xcraft unpublish --stage s` removes a stage, all its snapshots and ISOs, and what xcraft keeps of them, like the CI
of a pull request once closed: its files are its own copies, which nothing else refers to.
`xcraft prune` removes the builds of the remote storage whose packages no snapshot of the repositories has, of any
stage, dated or latest, pushed more than `--keep-days` days ago, 15 by default, like a weekly job of CI with the key
of the bucket: what is published stays a starting point for a fix, its builds kept as long as its snapshots, see
`snapshot_days`, and the grace period covers a publication failing, its builds waiting for the next one. A latest
snapshot without its manifest, while a cumulative one changes, keeps its builds by what it was made from. The ISOs
keep no builds of their own: they are made from the packages of a snapshot of the repositories, and one kept longer
than it is not a starting point anymore. The ISOs pushed, by `xcraft push isos`, go the same way, but those the
snapshots of the ISOs copy, dated or latest, and those pushed in the grace period: each expired snapshot would leave
its ISOs otherwise, the largest files of the storage. The files of the earlier generations of the trees go too. `-n` shows what
would go, and the space it would free. Like two publications, it must not run during one, which may copy a build it
removes.

Only one `xcraft publish` must run at a time on a storage, like in a CI job of its own: Garage has no conditional
writes, so two publishes at the same time could take the same snapshot name, and mix their files.

## Signing

Our packages are signed with the host's gpg, which holds the key, by rpmsign in a container of its own: it uses the
restricted socket of the host's gpg agent, made for that, so the key never leaves the host, nor its HSM, and is never
available to the builds, which run the code of the specs.

- **Every build is signed once made**, once rpmbuild is gone, by the key of the command: `--key` or `$XCRAFT_KEY`,
  or else `[sign]`, or else the developer's key. Without any, the builds of dev mode make the developer's key, as
  `xcraft key create` does: an Ed25519 key without passphrase, in its own keyring, `.xcraft/key/`, apart from the
  developer's own keys, as it is only for test hosts, and trusted by nobody; `--final` fails instead. A build keeps the
  public key of its signer, `RPM-GPG-KEY`, and the cache and the storage keep the builds by signer,
  `builds/<fingerprint>/`: the builds use those of any key, and a build pushed or published by another key is signed
  again, into a copy. The repositories of development builds have the public key, and their `.repo` file checks the
  packages and the repository's metadata (`gpgcheck`, `repo_gpgcheck`).
- **Published packages** are signed with the key of `[sign]` in `xcraft.toml`:

  ```toml
  [sign]
  key = "<fingerprint>"   # used through gpg, so it can be in an HSM, like with gnupg-pkcs11-scd
  gnupghome = ""          # gpg's keyring, the default one when empty
  ```

  Each build is signed once by key, and kept in `.xcraft/cache/builds/<fingerprint>/` and in the remote storage, in
  `.xcraft/builds/<fingerprint>/`: signatures have a date, so signing again would not give the same files, which the
  storage could not store once. The snapshot's packages are checked against the public key, its `repomd.xml` are signed, and
  it has the key, as `RPM-GPG-KEY`. `--key <fingerprint>` overrides `[sign].key`, and `--dev-key` explicitly selects the
  developer's key even when `[sign]` exists. Without `[sign]` or `--key`, publishing uses the developer's key by
  default. Trees signed with that key are trusted by the same checkout without adding it to `remote.main.build_keys`.

The upstream packages are checked against the keys of their repositories. In final mode, the snapshot signs them
again with its key, so that the hosts and the ISOs trust that key alone: once by key, kept in
`.xcraft/cache/upstream-signed/<fingerprint>/<sha256 of the original>/` and in the remote storage, in a repository by
key and architecture, `.xcraft/upstream-signed/<fingerprint>/<arch>/`, which the next snapshots copy from, while the
upstream copy keeps the originals. Their source packages keep their signatures. In dev mode, all the packages keep
theirs, and the ISOs trust the keys of the upstream repositories too.
