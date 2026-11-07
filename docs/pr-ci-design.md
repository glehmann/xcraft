# Design: the CI of pull requests

Status: decided, not implemented. The PR signing key exists (`CC3128112B8E6023E6A13F33F6DE1632420D9C0B`, the secret
`PR_SIGNING_PRIVATE_KEY` and the variable `PR_SIGNING_KEY_FINGERPRINT` of xcp-ng-dist). It starts simple: the dev
bucket, the compiler caches as they are, and a stage by pull request.

## Today

`build.yml` of xcp-ng-dist builds the packages of every push, natively on x86_64_v2 and aarch64, and of the pull
requests of forks, without secrets. Only master pushes: its builds, its compiler caches and its incoming snapshot, to
the main bucket, signed with the CI key. So a pull request only shows whether its packages build, not whether the
repositories they make are installable, nor whether the ISO is made, and a tester builds it again.

## Goals

- Check what a pull request changes as master does: the packages, the repositories and the ISOs.
- Publish its repositories and ISOs, to install and test them on hosts, under releases that cannot be mistaken for
  master's, nor trusted where master's are.
- Remove them once it is closed.
- Change nothing for the developers' work, offline by default.
- Give nothing to the code of forks.

## The workflow of a pull request

A workflow on `pull_request`, for the branches of xcp-ng-dist only, as the push of a branch does not tell its pull
request. For #123, on each push, on each architecture, with `XCRAFT_DEV=pr123` and
`XCRAFT_DEV_COUNTER=${{ github.run_number }}`:

1. `xcraft build --locked --fetch=needed`: the packages the pull request changes, and those built against them, get
   development builds, `<release>+b<n>~<run number>.pr123`, signed at build by the PR key, the key of the run; the
   others are master's, fetched from the main bucket, signed by the CI key.
2. `xcraft publish --locked --to dev --stage pr123 --key "$PR_SIGNING_KEY_FINGERPRINT"`: the snapshot, checked to be
   installable, and the ISOs on x86_64, in `repos/<release>/pr123/` and `isos/<release>/pr123/` of the dev bucket,
   for the testers to install from. The development builds go there in the snapshot only: they are never pushed as
   builds, nor fetched. Nothing is signed by the publication: master's builds and the upstream originals are copied
   server-side from the main bucket, which costs neither disk nor upload; the upstream packages a pull request adds,
   which the main bucket's copy does not have, are uploaded as they are, with the key of their repository among the
   stage's, as for every publication of dev mode. See [the layout of the published
   repositories](publishing-layout-design.md#the-stages-of-the-pull-requests).

The compiler caches are fetched from master's, as the builds of every branch do now, and never pushed: the next push
of the pull request starts from master's again.

### The counter

The counter of a development build orders the builds of a package: a host upgrades from one to the next. Out of CI,
xcraft keeps it in `.xcraft`, and only raises it when the inputs of the build change. On CI, `.xcraft` is gone between
runs: the counter is the run number of the workflow instead, `$XCRAFT_DEV_COUNTER`, which only grows, so each push of
the pull request upgrades the previous one, without keeping anything between the runs. Each run builds its
development builds again, as none is fetched. Running a run again keeps its number: the same release for builds of
the same inputs.

### Signing and trust

The PR key signs as a developer's key does: the development builds at build, and the metadata of the stage; master's
builds keep the CI key's signature, and the upstream packages their own. It is trusted by nobody else: not in
`build_keys` of any remote, which would make master's CI accept, from the dev bucket, a final build a pull request
planted under a key master builds. The testers
install from the stage with the keys of all its signers, the PR key, the CI key and the upstream ones, which it
publishes in a file each, see [the signatures](publishing-layout-design.md#signatures).

The PR key and the write credentials of the dev bucket are repository secrets: the workflows of any branch pushed to
the repository get them, the developers who already have the dev bucket. The CI key and the credentials of the main
bucket move to a GitHub environment, `main`, which only master deploys to: a branch changing the workflows cannot get
them. See [the secret management](ci-secret-management-design.md#1-enhancements-without-change-in-behaviour).

### Closing a pull request

A workflow on `pull_request: closed` removes the stage of the pull request, `repos/<release>/pr123/` and
`isos/<release>/pr123/`, with `xcraft unpublish --to dev --stage pr123`: it is all a pull request writes, its
development builds included, see [removing what nothing refers
to](publishing-layout-design.md#removing-what-nothing-refers-to). The stages `pr<n>` are the pull requests': the
developers do not use them.

### Forks

The `pull_request` runs of forks have no secrets: they build with `--dev pr123`, and check the repositories and the
ISOs with `xcraft publish --local`, but publish nothing. `pull_request_target`, which has the secrets, must not build
them: the build runs the code of the pull request, its scriptlets and build tools.

## What xcraft needs

- `$XCRAFT_DEV_COUNTER`: the counter of all the development builds, rather than those kept in `.xcraft`.
- `xcraft publish --dev`, with `$XCRAFT_DEV`, to the dev bucket or locally: its snapshot already has the development
  builds of local snapshots; the ISOs need them in their keys too, as `xcraft iso` has them. It publishes the
  development builds only, copying the others server-side from the main bucket, and writes nothing
  else to the bucket, as nothing of a pull request is reused. With the read of the main bucket for the dev bucket's
  key of the pull requests.
- `xcraft unpublish --to dev --stage pr123`, removing a stage.

## Rejected for now

- **A bucket of the pull requests.** The developers who may push a branch, and so get its secrets, are those of the
  dev bucket: a bucket of its own would only keep a container escape during a build from deleting the developers'
  data there, which it could not make trusted anyway.
- **A counter in the storage, or in the cache of the actions.** The run number orders the builds without either.
- **Compiler caches of the pull requests,** and their layering for stacked pull requests: the builds start from
  master's, which is enough for now. If needed later: the cache of the branch the pull request targets, copied by
  package at its first build, and written for its branch.
