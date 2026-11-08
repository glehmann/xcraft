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

- Our packages are built or fetched, but only fetched for the architectures the host emulates: those are built by
  native builders, which push them. The publish fails when the remote storage misses one of their builds.

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
- The ISOs are made like `xcraft iso` makes them, from the snapshot, for its x86_64 architectures, when `[iso]
  installer` is configured: their boot loaders are for x86_64. They are signed like the snapshot: their repository,
  and their files, with a detached signature. Their checksums are in `SHA256SUMS`, as `sha256sum` writes them, with
  its detached signature, so that `sha256sum -c SHA256SUMS` checks a download.
- All snapshots are kept. The storage stores identical files once: our packages are copies of the pushed builds, and
  the upstream ones and the sources of the previous snapshot, so a snapshot only costs what changed.

Publication signs the hashes of the locally checked snapshot. Files are reused by server-side S3 copy only when a
trusted remote manifest has hashes matching the local snapshot. Those copies are not downloaded again during publication;
downloads still check each payload against its signed manifest.
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
input record.
`--local` and an explicit `--output` still materialize the snapshot locally.

`xcraft publish repos` only publishes the repositories, and `xcraft publish isos` the ISOs, under a snapshot name
used by neither within the selected stage. `xcraft publish --local` only makes the snapshot in `.xcraft/publish/`,
its ISOs in its `iso/`, of one architecture, like the other commands, unless `--arch all` is given.
A snapshot published to the storage is of all the architectures, unless `--arch` is
given: it replaces the latest one, whose other architectures would be gone. In CI, after `xcraft push builds`,
`xcraft publish --locked` fails rather than change the lock.

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
