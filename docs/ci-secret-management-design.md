# Design: CI secret management

Status: proposal, but point 1 of part 1, done: the `main` environment of xcp-ng-dist, which only `master` deploys
to, has the CI key and the credentials of the main bucket, and the job of `build.yml` takes it on `master` only, the
other branches running it without; the secrets of the pull requests, of the dev bucket and the PR key, stay
repository secrets, without an environment of their own. The four parts are independent: the first changes nothing
for the users of the storage and the packages, and can be done now; the second and third each list options, with a
recommendation; the fourth gives the releases a bucket of their own.

## Today

xcp-ng-dist has three workflows:

- `build.yml` builds the packages natively on x86_64_v2 and aarch64, for every push and the pull requests of forks.
  On pushes to `master` only, the same job then imports `SIGNING_PRIVATE_KEY` into a temporary keyring, and with
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` runs `xcraft push --to main`, `xcraft push ccache --to main` and,
  on x86_64_v2, `xcraft publish --stage incoming`, all with the same key, `--key $SIGNING_KEY_FINGERPRINT`.
- `checks.yml` checks the lock, without secrets.
- `lock-update.yml` proposes the updates of the lock, with the `GITHUB_TOKEN` of the run, allowed to write the
  repository and the pull requests.

CI never writes the dev bucket: the developers do, with keys of their own. The secrets are repository secrets, the
workflows have `permissions: {}`, check out without persisting credentials, pin their actions by commit, and give the
secrets to the steps needing them only.

### What each secret gives

| Who has it | What they can do |
|---|---|
| The S3 key of the main bucket | Delete anything, replace the `index.html` pages, store junk. Not make readers use a forged build or compiler cache: their manifests must be signed by a key of `build_keys`. Not a forged source archive: it must match the committed `sources` manifests. Not forged packages on the hosts: they check the signatures of the packages and of the repositories' metadata. |
| The signing key | Sign builds, compiler caches, packages, snapshots and ISOs that every checkout and every XCP-ng host trusts. |
| Whoever can push a branch | Both: a branch can change `build.yml`, and the `master` conditions with it, and the repository secrets are given to any workflow of the repository. |
| Code run by the build, the specs and their build tools | Nothing, as long as the containers isolate it: the builds have no network, and the key is imported after them. Escaping a container gives the runner's account, which has passwordless sudo on GitHub's runners, and so the secrets imported later in the same job. |

So the signing key matters most, then who can trigger the steps using the secrets; a stolen S3 key mostly costs
availability, which backups cover.

## 1. Enhancements without change in behaviour

1. **A GitHub environment for the secrets**, `main`, which only `master` can deploy to, with the three secrets
   moved there from the repository secrets. A branch changing the workflow then cannot get them: GitHub refuses the
   jobs of an environment to the other branches. An environment applies to a whole job, which leads to the next
   point.
2. **Separate build and upload jobs.** The build job keeps running on every push, without secrets; an upload job,
   on `master` only, in the `main` environment, starts on a clean runner, gets the builds, the sources and the
   compiler caches of the build job as artifacts, imports the key and pushes. A container escape during the builds
   then gets nothing, as the key is never on that runner. The upload job still runs xcraft, which parses the specs
   in containers to compute the keys of the builds: an upload-only `xcraft push`, from the keys the build job
   recorded, would remove that, but is not needed for the split. The compiler caches can be large, the kernel's in
   particular: their artifacts are to be measured.
3. **Branch protection of `master`**, by a ruleset: changes through reviewed pull requests, no force push, and the
   workflows owned by the maintainers, with `CODEOWNERS`. The environment only protects the secrets as much as
   `master` is protected.
4. **The S3 key with the rights it needs only**: read and write on the main bucket, without the owner right, which
   allows changing the settings of the bucket, like its website configuration. No admin token of Garage in CI.
5. **A backup of the main bucket**, append-only, out of reach of the CI's key, like a daily `rclone copy` to another
   storage: Garage has neither versioning nor object locks, so a deletion cannot be undone otherwise. The source
   archives whose URL is gone only exist there. With part 4, the release bucket needs it first: the main bucket can
   be made again from the sources and the commits, the published releases cannot.
6. **A signing subkey in CI, the primary key offline.** The secret becomes the subkey alone (`gpg
   --export-secret-subkeys`): the primary key, which certifies and revokes, stays offline. xcraft already accepts a
   signature by a subkey of a key of `build_keys`, as gpg reports the primary key's fingerprint too, and RPM and dnf
   accept them as well. A leak is then fixed by revoking the subkey and making a new one, without changing the
   fingerprint in `xcraft.toml` nor the key installed on the hosts. For that, xcraft must reject the signatures of
   revoked or expired keys, which it does not check yet: it only looks for `VALIDSIG`, which gpg may report for them
   too, along with `REVKEYSIG` or `EXPKEYSIG`, to check.

## 2. Signing

The goal: a key the hosts trust is never in a place where code from the specs runs, nor where a workflow change can
reach it, and cannot be copied even from the machine using it.

### Hardware keys

With the key in a hardware security module, signing needs the device: whoever compromises the machine using it can
sign while they control it, but cannot copy the key, and loses the ability once found out or once the device is
unplugged. A key file, in a CI secret or on a signing host, gives them signatures forever, until it is revoked
everywhere. Every signature can also be logged on the machine using the device, with what it signed, for an audit.

xcraft already signs through gpg and its agent: the RPMs by rpmsign in a container using the restricted socket of
the host's agent, the manifests, metadata and ISOs by gpg directly. So a key in a device works without change in
xcraft, by `[sign] key` or `--key`, its fingerprint:

- **An OpenPGP card**, like a YubiKey or a Nitrokey 3: gpg uses it natively, through scdaemon. One signing key, RSA
  4096, or ed25519, which recent versions of RPM accept.
- **A PKCS#11 HSM**, like a YubiHSM 2 or a Nitrokey HSM 2: gpg uses it through `gnupg-pkcs11-scd`. The YubiHSM 2
  holds several keys, and logs its operations itself, in a log the host cannot rewrite; its FIPS variant is
  validated at FIPS 140 level 3, tamper-resistant, which only matters if a certification is required.
- **A cloud KMS**, whose keys are made and kept in the provider's HSMs, not exportable: see option D below.

The device needs a PIN, which the agent keeps, cached for the duration of a signing session, for unattended
signing; a card can require the PIN for each signature (`forcesig`), which only suits manual releases. The devices
are slow, from a few to around one signature per second, by key size and device: the RPMs are signed once each, the
signed builds being kept in `signed/`, so a full rebuild signs a few hundred files, and a publish a handful of
metadata. A spare device with the same key, or a backup of the key made before importing it, kept offline, covers the
loss of a device.

GitHub's runners cannot use a device: the signing moves to a machine of ours, or to a cloud KMS.

### Options

| | Where the key is | What a compromise gives | Changes |
|---|---|---|---|
| **A.** Today, improved by part 1 | A CI secret, a subkey, in the upload job of `master` | Signing forever, until the subkey is revoked | None |
| **B.** Two keys | The CI key, as in A, used by the workflow for the builds, the compiler caches and `incoming`; the release key on a hardware key, used by a release manager, a person, on their own machine, for the other stages | For the CI key, as in A: the hosts of the published stages do not trust it, but its forged builds get into the next publication, see below | `build_keys` with both; the publications of the other stages run by hand, with an S3 key of the release bucket, part 4, on the release manager's machine |
| **C1.** A signing host publishing | The CI key as in A, for the builds and compiler caches the workflow pushes; the release key on a hardware key, on a host of ours, which publishes the repositories and ISOs | For the CI key, as in B. Of the host: release signatures while compromised only | The host; xcraft unchanged |
| **C2.** A signing host pulling everything from GitHub | A hardware key on a host of ours, which fetches the artifacts of the builds of `master`, pushes the builds and compiler caches, and publishes the repositories and ISOs | Of the host: signing while compromised only. Of GitHub: nothing | The host; an xcraft push from the artifacts, without building |
| **D.** A cloud KMS, reached by OIDC | The provider's HSM; the upload job gets temporary cloud credentials for the signing only | Signing while an upload job of `master` runs | A cloud account; a bridge from gpg to the KMS, to prototype |
| **E.** OpenBao, reached by OIDC | An HSM of ours, used by OpenBao's Transit engine; the upload job gets an OpenBao token for the signing only | As in D | OpenBao to operate, with its HSM; a program making OpenPGP signatures from Transit's, to write |

GitHub's runners cannot use a hardware key: in B, it is only used by the release manager, plugged into their machine.

**C1, a signing host publishing:** the workflow keeps pushing the builds and the compiler caches, signed with the
CI key, with the S3 key, as in B. The host runs `xcraft publish`, to the main bucket at each new commit of `master`
for the `incoming` stage, or to the release bucket, part 4, when a release manager asks for the others, with the
release key in its device, and an S3 key of its own, to write the repositories and the ISOs. It is B with the release
manager replaced by a service, and the CI key and the S3 key of the workflow stay on GitHub.

**C2, a signing host pulling everything:** the workflow only makes artifacts of the builds, the sources and the
compiler caches. The host asks GitHub for the completed runs of the build workflow on `master`, checks their
repository, workflow, branch and commit with GitHub's API, downloads their artifacts, checks their digests, signs and
pushes them with xcraft, then publishes as in C1: no inbound connection, no runner exposed to the workflows of the
repository, and only a token to read the artifacts, on our side. As the host pushes, it holds the S3 key of the main
bucket, and the workflows do not need it anymore: neither a signing key nor the S3 key are on GitHub (part 3, A). The
device can hold both keys, the CI one and the release one, or a single one. xcraft needs a push of what the build job
staged, as it must not build nor run the specs on that host: see [Pushing without the specs](#pushing-without-the-specs).

**What the release key signs.** In B and C1, the release key cannot be stolen, but it signs what the builds signed by
the CI key contain: whoever steals the CI key can push forged builds, which the next publication signs. In C2, the
host only signs the artifacts of runs of `master`, as GitHub's API reports them: forging them needs a change merged
in `master`, or the compromise of a runner during a build.

A self-hosted runner limited to the upload job does the same with less code, but takes the jobs GitHub gives it: the
workflows of the pull requests of forks can ask for any runner by its labels. It needs a runner group limited to the
repository and to the workflow of the upload, where the plan of the organization allows it, the approval of the
workflows of outside contributors, one job at a time, and a cleaning after each job.

**D, a cloud KMS,** signs digests, not files: gpg must use it as a key, through `gnupg-pkcs11-scd` and the PKCS#11
library of the provider, like Google's `kmsp11`, to check, so that the signatures stay OpenPGP ones, which RPM and dnf
verify. Its access is configured in the provider for the repository, the workflow and `master` only, so the OIDC
claims are checked by the provider, not by a service of ours.

D protects the key as well as C2: both keep it in a non-exportable HSM, certified FIPS 140 level 3 for the cloud
ones and the FIPS variant of the YubiHSM 2. It is not safer than C2, though, for what gets signed: the signing
requests come from a GitHub runner, in a job which also runs xcraft, and xcraft parses the specs in containers to
compute the keys of the builds, so a container escape in the upload job can sign anything while the job runs. It also
adds the cloud account to what is trusted, as its administrators can grant the signing. Its real advantage is that we
have no machine to secure. With an upload job which neither builds nor parses the specs, pushing what the build job
staged as C2 does (see [Pushing without the specs](#pushing-without-the-specs)), D would be on par with C2, and the choice between them is whether we would rather depend on a cloud account or
operate a host. The S3 key stays on GitHub with D, unless part 3 removes it otherwise.

**E, OpenBao,** the open-source fork of Vault, is D on our side: its JWT authentication checks the OIDC token of the
upload job, by repository, workflow and branch, and since its version 2.7, its Transit engine can use a key in an
HSM, through its PKCS#11 KMS plugin, without storing it. But Transit's signatures are raw RSA or ECDSA ones, not
OpenPGP ones: a program of ours would wrap them in OpenPGP signatures, used by rpmsign as its `%__gpg_sign_cmd`, and
by xcraft instead of gpg; it is code to write, and sensitive. The OpenPGP plugin written for Vault,
`vault-gpg-plugin`, makes OpenPGP signatures, but with keys stored in OpenBao, not in an HSM, and is not known to run
on OpenBao. To investigate: whether that plugin could use OpenBao's keys in an HSM, otherwise the size of the
wrapping program, with a prototype against Transit and SoftHSM. It has the same limits as D, for what gets signed.

### Pushing without the specs

`xcraft push` computes the keys of the builds, which needs the specs parsed and the build roots resolved, in
containers, to find the builds to upload: the cached build `.xcraft/cache/builds/<arch>/<pkg>/<key>/`, and its tree in
the storage, `builds/<arch>/<pkg>/<key>`. The sources and the compiler caches need no key: the archives come from the
committed `sources` manifests, and a compiler cache is found by the name of its package. So the push stage needs the
trees to upload and where they go, not the keys, and the build job, which knows them, can give exactly that:

1. In the build job, `xcraft push --to main --output DIR` does what `xcraft push` does, but writes in `DIR`, in the
   layout of the storage, what the main bucket misses, which it checks by its public reads, without credentials: the
   current builds, the compiler caches, as tarballs of the entries of the last build only, as only that job knows
   them, and the source archives. It neither signs nor uploads anything. `DIR/staged.json` lists the trees, with
   their files and their SHA-256, and the source archives.

   ```
   DIR/builds/<arch>/<pkg>/<key>/RPMS/...
   DIR/ccache/<arch>/<pkg>/ccache.tar
   DIR/sources/<sha256>/<file>
   DIR/staged.json
   ```

2. `xcraft push --from DIR --to main --key <fingerprint>` reads `xcraft.toml` only, for the storage and the key,
   without preparing the project: no spec, no container. It checks the layout of the paths, `builds/<arch>/<pkg>/<64
   hex digits>/`, `ccache/<arch>/<pkg>/` and `sources/<sha256>/<file>`, and the files against their SHA-256, skips
   the trees the storage has, never replacing one, then uploads the others, writing and signing their manifests as
   `xcraft push` does, and the archives.

`staged.json` has neither the commit nor the run: the signing host must not believe what an artifact says about
itself, and gets them from GitHub's API, for the run whose artifacts it downloads. It checks the packages of the
paths in its own checkout of that commit, by their directories, which needs no parsing. It cannot check that a key
is the one of its build, which needs the specs: the trust comes from the run of `master`, not from the content, as
in [What the release key signs](#options).

The developers keep using `xcraft push`, to the dev bucket: staging is for CI and the signing host. In xcraft, the
push of the sources, builds and compiler caches already computes a path in the storage, a local directory and its
files for each of them: `--output` copies them into `DIR` instead of uploading them, and `--from` is a loop over
`staged.json`.

Not retained:

- **Sigstore**, keyless, and GitHub's artifact attestations: RPM and dnf only verify OpenPGP signatures, of keys
  installed on the hosts. Attestations could come in addition, to record which run built what.
- **Vault**: its keys in an HSM need Vault Enterprise, and Vault is not open source anymore: E, OpenBao, is its
  open-source equivalent.

**Recommendation:** B now, the release key on a hardware key, used by a release manager for the published stages,
and the CI key a subkey as in part 1. Then C1, the release key moving to a host, for automated publications like
`incoming`, with no change in xcraft. Then C2 with a YubiHSM 2, which takes the CI key, and the S3 key as the host
pushes, off GitHub, and makes the published packages those of the runs of `master`. D, with an upload job which
does not parse the specs, is as good if a cloud account is preferred to a host to operate, but keeps the S3 key on
GitHub; E is the same without the cloud account, once its OpenPGP signatures are solved.

## 3. Pushing to the main bucket

After part 1, the S3 key only goes to the upload job of `master`, its theft costs availability, and the backup
covers it. Removing it from GitHub entirely is then worth less than the signing, but possible:

| | Where the permanent S3 key is | Limits by prefix | Expiring credentials | Effort |
|---|---|---|---|---|
| **A.** The signing host of part 2, C2, pushes what it signs | That host; GitHub has no credentials at all | No | — | That of part 2, C2 |
| **B.** A service exchanging the OIDC token of the job for a Garage key made for the run, by Garage's admin API, deleted after it | That service, with the admin rights of Garage | No | Only as long as the service deletes them: an interrupted service leaves them | A small service, and a reaper |
| **C.** A gateway exchanging the OIDC token for temporary credentials, checking them in each S3 request, signing it again for Garage | The gateway | Yes | Yes | A large service, and security-critical: it implements SigV4, its chunked payloads, multipart uploads, copies and batch deletions. No existing project does it for Garage as far as we know: MinIO's gateway mode was removed, VersityGW translates S3 to other backends, without OIDC exchange. |
| **D.** A store with OIDC and STS, `AssumeRoleWithWebIdentity`: Ceph's RGW, MinIO, SeaweedFS | The store has none to give | Yes, by its policies | Yes | A migration; Ceph is heavier to operate, MinIO's community edition has changed its distribution, SeaweedFS's support is to check |

**Recommendation:** A, with part 2's C2: nothing permanent is on GitHub anymore. Otherwise keep the key in the
environment of part 1. C and D only if uploads need limits by prefix, like letting the CI of other projects write in
the same bucket. With B, C or D, the OIDC token is only given to the upload job, `permissions: id-token: write` on
that job alone; the pull requests of forks never get one.

## 4. A bucket for the releases

The published stages, the ones hosts install, get a bucket of their own, the release bucket: written only by the
release manager, then by the signing host of part 2, C1 or C2, with an S3 key allowed to write it and to read the main
bucket, and nothing else. `[remote.release] build_keys` has the release key only. The main bucket keeps what CI writes:
the builds, the sources, the compiler caches, and the `incoming` stage, signed with the CI key; it becomes the ci
bucket, named after its writer like the others.

- **The CI's S3 key cannot touch the releases**: today it can delete or replace anything of the main bucket, the
  published stages included. Its theft then costs the main bucket's availability, which can be made again, not what
  the hosts install.
- **Trust as the use**: the hosts point at the release bucket, which only the release key signs; the signatures of the
  CI key, valid in the main bucket, mean nothing there.
- **Policies of their own**: the releases are kept and backed up first, part 1's fifth point; the main bucket is made
  again after a change of the build keys, its builds and compiler caches come and go.
- **A stable public URL**, which no rebuild nor cleanup of the main bucket touches.

xcraft copies the packages of a snapshot server-side within a bucket. Publishing to the release bucket copies the
builds of the main one: a copy between buckets, by a key reading the one and writing the other, keeps them
server-side; otherwise they go through the machine publishing, which is acceptable for a first release. The release
key signs what the builds signed by the CI key contain: see [What the release key signs](#options).

The dev bucket follows the same rule: the developers write it, each trusting their own key, and so does the CI of the
pull requests, as a developer, with the PR key, see [the CI of pull requests](pr-ci-design.md#signing-and-trust).

### Not a stage of the ci bucket

A release stage of the ci bucket, signed with the release key, would keep the CI key from forging a release: the hosts
check the signatures of the packages and of the repositories' metadata. But not from deleting it, nor replacing it
by an older release, as validly signed, which the hosts accept: Garage's S3 keys have rights by bucket, not by prefix,
see part 3. xcraft trusts the keys of `build_keys` for a whole remote, the CI key with the release key. And the
backups and retention apply by bucket. The stage names a channel, the bucket who writes it: the release bucket has
stages too.

The URL of the release repositories is in the configuration of the hosts: moving it later means reconfiguring them,
so the bucket is to decide before the first host is installed from it.

### The stages in the release bucket

The published paths keep their stage, `repos/<release>/<stage>/` and `isos/<release>/<stage>/`, with names reading
well in the URLs, like `stable`, then `testing` or `updates` next to it. Without the stage, the bucket saying
"released" already, the URLs are shorter, but a second channel, like release candidates next to the release, or
updates apart from the base, would need a mixed layout or moving the first one, and reconfiguring the hosts. xcraft
makes the same paths for all the stages, with the reuse of the previous snapshot, the pages and the cleanup built on
them: an optional stage would touch all of them, and the tests of both layouts.

### Mirrors and atomic publication

Each publication makes a dated snapshot, `repos/<release>/<stage>/<date>/`, and copies it as `latest/`, the same for
`isos/`: every snapshot has all the packages, copied server-side. Garage deduplicates their blocks, but over HTTP
each copy is a file of its own: a mirror following the pages downloads the whole repository for each snapshot, and
again each time `latest/` changes. rsync would keep hard links, which S3 and HTTP do not have.

And a publication is not atomic: `_upload()` copies the changed files of `latest/` in the order of their names,
`repomd.xml` among the others, and deletes the files of the previous snapshot in the same publication. A host updating
meanwhile can read metadata whose packages are not copied yet, or lose a package being downloaded.

**A pool of packages.** The packages go once in `repos/<release>/pool/<arch>/`, by their file names, never changed;
a snapshot only has its `repodata/`, whose packages are located in the pool by relative paths, `../../pool/...`:

- the mirrors download each package once, then the metadata of each snapshot, a few MB: a new release costs its new
  packages only;
- publishing a snapshot copies no package, and `latest/` is metadata only;
- the storage does not depend on Garage's deduplication anymore.

Checked on AlmaLinux 10, over HTTP, with the RPMs in `repos/9.0/pool/x86_64_v2/` and `repodata/` only in
`repos/9.0/stable/<date>/base/x86_64_v2/` and `latest/`: createrepo_c writes the locations given by `--pkglist` as they
are, `../../../../pool/x86_64_v2/...`, and `dnf install` takes them, resolving the `..` itself, so the server only gets
clean paths, `/repos/9.0/pool/x86_64_v2/...`. The mirrors copying the tree as it is, by rsync, `wget --mirror` or
`rclone` over HTTP, keep those paths valid. `dnf reposync` does not: it refuses a package out of the directory of the
repository, "outside of safe write path", with or without `--norepopath` and `--download-metadata`, a check against
hostile metadata without an option to lift it. `xml:base` does not help, as it can only give an absolute URL, which
would send the hosts of the mirrors back to ours. So the mirrors sync the tree, as XCP-ng's do by rsync, to confirm;
those needing reposync could get a view of `latest/` with its packages, one copy of the repository rather than one by
snapshot.

**Atomic as S3 allows.** S3 and Garage replace an object atomically, a reader getting the old one or the new one, but
nothing spans several objects. So a publication writes what no reader sees first, and switches with one object:

1. the new packages, in the pool: new names, which nothing refers to yet; an interrupted upload breaks nothing;
2. the metadata of the new snapshot, named after their checksums by createrepo, `<sha256>-primary.xml.gz` and so on,
   in its new directory, which nothing refers to yet;
3. in `latest/`, the new metadata files, then `repomd.xml` last: the switch, a reader getting the previous repository
   or the new one, both complete;
4. the metadata files of `latest/` the new `repomd.xml` does not refer to are deleted at the next publication, not
   this one: a client which read the previous `repomd.xml` just before still finds them. The pool's packages are never
   deleted, but by a retention of its own, so a host never loses a package during its update.

The signature of `repomd.xml`, `repomd.xml.asc`, is another object: right after the switch, a reader can get one
without the other, and dnf then refuses the metadata until its next refresh. Writing the signature right after
`repomd.xml` keeps that window to one request; plain S3 cannot make the two atomic. The ISOs have the identifier of
their build in their name, `xcp-ng-<version>-<id>-<arch>.iso`, so new ones are new files, and only `SHA256SUMS` and
its signature are replaced, with the same window.

The mirrors get the same guarantee by syncing the pool before the metadata: the pool's files never change, so a sync
interrupted, or done twice, never leaves a mirror with metadata whose packages are missing. The ISOs are not in the
pool, being downloads rather than repositories: the dated directories still copy them, and mirrors can keep only
`latest/` of `isos/`.

**`.xcraft/` in the release bucket.** Publishing also writes into the `.xcraft/` of the bucket: the builds signed by
the publishing key, once each as signatures have a date, which the snapshots, or the pool, copy their packages from;
the trees of the ISOs; the inputs of the latest snapshots, to republish an unchanged one by copies; the public keys.
The first two are about as large as the releases. The public HTTP endpoint serves the files but does not list the
directories, listing by the S3 API needing credentials: the mirrors follow the `index.html` pages xcraft writes, like
`wget --mirror` or `rclone` with its HTTP backend. An option of the remote, like `[remote.release] hidden =
[".xcraft/"]`, leaves `.xcraft/` out of the root page, and writes no page under it: the mirrors then only find
`repos/`, `isos/` and their pages. The files under `.xcraft/` stay readable, signed, public data, and xcraft reads
them by their paths and signed manifests, not through the pages. Moving them out of the release bucket would leave it
only a few small files there, but changes the publication and the ISOs, and needs the ci bucket to trust the release
key for those trees: not worth it while hiding them is enough.

The pool and the order of the writes apply to every bucket publishing, the ci bucket's `incoming` stage included,
which they make cheaper to publish too.

## What it does not cover

- **A compromised `master`**: whoever can merge to it gets the CI key and the S3 key through its workflows. The
  review of the pull requests, and part 1's branch protection, are the protection.
- **Honest builds**: a signature says which key signed, not that the build is the one of the sources. Reproducible
  builds, compared across builders, would.
- **`lock-update.yml`**: its pull requests, created with `GITHUB_TOKEN`, do not trigger the other workflows, which
  needs a GitHub App or a deploy key, another secret to manage.

## Sources

- [GitHub environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)
- [GitHub OIDC](https://docs.github.com/en/actions/concepts/security/openid-connect)
- [Security of self-hosted runners](https://docs.github.com/en/actions/concepts/runners/self-hosted-runners#self-hosted-runner-security)
- [Garage S3 compatibility](https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/)
- [OpenBao 2.7: keys in an HSM](https://openbao.org/community/release-notes/2-7-0/), [its JWT authentication](https://openbao.org/docs/auth/jwt/), [vault-gpg-plugin](https://github.com/LeSuisse/vault-gpg-plugin)
- [YubiHSM 2](https://docs.yubico.com/hardware/yubihsm-2/hsm-2-user-guide/), [Google Cloud KMS PKCS#11 library](https://cloud.google.com/kms/docs/reference/pkcs11-library)
- [GnuPG and smartcards](https://www.gnupg.org/howtos/card-howto/en/smartcard-howto.html), [gnupg-pkcs11-scd](https://github.com/alonbl/gnupg-pkcs11-scd)
- [Remote storage design](remote-storage-security-design.md): the buckets, and the signed trees.
