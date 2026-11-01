# Design: the CI of pull requests

Status: proposal. The PR signing key exists (`CC3128112B8E6023E6A13F33F6DE1632420D9C0B`, the secret
`PR_SIGNING_PRIVATE_KEY` and the variable `PR_SIGNING_KEY_FINGERPRINT` of xcp-ng-dist); nothing else is implemented.
The compiler caches have three approaches, compared at the end, without a choice yet.

## Today

`build.yml` of xcp-ng-dist builds the packages of every push, natively on x86_64_v2 and aarch64, and of the pull
requests of forks, without secrets. Only master pushes: its builds, its compiler caches and its incoming snapshot, to
the main bucket, the ci bucket below, signed with the CI key. So:

- a pull request only shows whether its packages build: not whether the repositories they make are installable, nor
  whether the ISO is made, which only master's publish checks;
- what a pull request builds is not shared: a tester builds it again, and so does each push of the pull request,
  without the compiler caches of its own previous builds;
- the compiler caches only come from master, whatever the branch a pull request targets.

## Goals

- Check what a pull request changes as master does: the packages, the repositories and the ISOs.
- Share its builds and repositories, to install and test them on hosts, under names that cannot be mistaken for
  master's, nor trusted where master's are.
- Build each push of a pull request with the compiler caches of its previous pushes, and of the branch it targets.
- Remove what a pull request left in the storage once it is closed.
- Keep the developers' work offline: nothing of this is needed, nor used by default, out of CI.
- Give nothing to the code of forks.

## The workflow of a pull request

For a pull request of a branch of xcp-ng-dist, #123 targeting `8.3` here, on each push, on each architecture:

1. `xcraft build --locked --dev pr123 --fetch=needed`: the packages it changes get development builds,
   `<release>+b<n>~<counter>.pr123`, the others are fetched as master's; see [the counter](#the-counter).
2. `xcraft publish --local`: the snapshot, checked to be installable, and the ISOs on x86_64. Pushes to master do it
   too, before publishing: the repositories and the ISOs are built explicitly, not only by the publish of master.
3. With the PR key and the write credentials of the pr bucket: `xcraft push --to pr`, the compiler caches, see
   [the compiler caches](#the-compiler-caches), and `xcraft publish --to pr --stage pr123`, for the testers to
   install from `repos/9.0/pr123/latest/` of the pr bucket. `xcraft push` never pushes development builds today, only
   final ones with `--final`: it would push those of a given `--dev`, to the pr bucket only.

The PR key is trusted in `[remote.pr] build_keys` only: nothing it signs is accepted from the other buckets, nor by
the hosts installing master's snapshots or the releases. As [the secret management](ci-secret-management-design.md)
notes, the repository secrets are given to the workflows of any branch pushed to the repository; the PR key and the pr
bucket's write credentials, a Garage key of their own allowed on that bucket only, cost little there. Master's key and
credentials belong in an environment only master's workflow gets.

### The buckets

Each bucket has one writer and one trusted key, see
[the release bucket](ci-secret-management-design.md#4-a-bucket-for-the-releases):

| Bucket | Writer | `build_keys` | What it has |
|---|---|---|---|
| pr | the CI of the pull requests | the PR key | their development builds, compiler caches and stages, until they are closed |
| dev | the developers | their keys | their builds and stages |
| ci, today main | the CI of `master` | the CI key | the builds, the sources, the compiler caches, the `incoming` stage |
| release | the release manager, then the signing host | the release key | the published stages, kept |

The main bucket becomes the ci bucket: with pr, dev and release, each bucket is named after who writes it, and "main"
would read as the bucket, which it is not anymore once the releases have theirs, and as the main branch, which xcraft
also calls so. Its Garage bucket, `xcraft`, keeps its data: Garage gives a bucket several aliases, like `xcraft-ci`
next to `xcraft`. xcraft has the names `main` and `dev` in its code, `Destination` and the order the storages are read
in: with pr and release, the remotes become names of `xcraft.toml` with a role, which ones are read by default and in
what order, which ones only on request, rather than names of the code.

A pr bucket rather than the dev one: the PR key and the developers' keys do not trust each other's builds, the CI's
credentials cannot delete the developers' data, and the cleanup of a pull request, or an expiry of the whole bucket if
Garage's version has one, cannot hit a developer's `--dev pr123` builds. Out of CI, xcraft reads the pr bucket only
when asked, like `xcraft fetch --from pr`: a developer testing a pull request locally.

### The counter

The `<counter>` of a development build grows with each build of other contents of the package, so that a host
upgrades from one push of the pull request to the next. xcraft keeps it in `.xcraft`, locally: a developer iterates
offline, as now, and that stays the default.

On CI, `.xcraft` is gone between runs, and the cache of the actions is evicted, which would give a counter again to
other contents: the same release for two builds. So `xcraft build --dev pr123 --counters-from pr` takes, for each
component, the greatest counter of the `*.pr123` builds the pr bucket has, plus one, and fails when it cannot read
the bucket: never a counter given again. Out of CI, the option is never needed.

### Closing a pull request

A workflow on `pull_request: closed` runs a new command, like `xcraft remote prune --to pr --dev pr123`, deleting:

- the stage, `repos/9.0/pr123/` and `isos/9.0/pr123/`;
- the development builds of `pr123`, and their signed copies;
- the compiler caches of the pull request, as its approach lays them out.

The sources stay: they are those of the committed manifests, which master may merge. A pull request whose closing
is missed only costs space, in a bucket of temporary contents.

### Forks

The `pull_request` runs of forks have no secrets, and the buckets are read publicly. A fork's pull request:

- fetches the compiler caches of the branch it targets, which is always one of xcp-ng-dist;
- builds with `--dev pr123` and a local counter, and checks the repositories and the ISOs with `publish --local`;
- pushes nothing, and has no compiler cache of its own: its branch name could also be one of ours.

`pull_request_target`, which has the secrets, must not build them: the build runs the code of the pull request, its
scriptlets and build tools. If forks are to be published later, two workflows: the `pull_request` run uploads its
builds as artifacts, and a `workflow_run` one, with the secrets but none of the fork's code, signs them with the PR key
and publishes them, once a maintainer labels the pull request. Their compiler caches are never pushed: an entry crafted
for another name would poison the builds using it.

## The compiler caches

[The sync](ccache-sync-design.md) shares one signed tree by package and architecture, `.xcraft/ccache/<arch>/<pkg>/`,
an object by ccache entry, pushed by master's CI and fetched by a build without a local cache. Two kinds of entries
matter here:

- the results, named by the hash of their inputs: one of a name is always valid for it;
- the ccache manifests, which change in place: ccache adds the results of the new combinations of included files it
  sees. Two trees can have another one of the same name; either is valid, each only knowing other results.

### Contexts

A context is the trees of a branch: master's and those of the long-lived branches, like `8.3`, in the ci bucket,
signed with the CI key, and those of the pull requests in the pr bucket, signed with the PR key:

```text
ci bucket     .xcraft/ccache/master/<arch>/<pkg>/
              .xcraft/ccache/8.3/<arch>/<pkg>/
pr bucket     .xcraft/ccache/<branch>/<arch>/<pkg>/
```

The contexts of the pull requests are named after their branch, their head, rather than their number: the branch a
pull request targets, its base, is then the name of a context, even when it is the branch of another pull request.
The workflow knows both from GitHub, `github.head_ref` and `github.base_ref`, and gives them to xcraft:
`--ccache <head> --ccache-base <base>`. A tree is only used signed by a key its bucket trusts.

Out of CI, xcraft keeps one local cache by package, fetched when missing from master's context, as now, unless a
context is given.

### Stacked pull requests

#124, of the branch `feat-b`, targets `feat-a`, the branch of #123, which targets `8.3`:

```text
feat-b (#124, pr)  ->  feat-a (#123, pr)  ->  8.3 (ci)  ->  master (ci)
```

Once #123 is merged, GitHub retargets #124 to `8.3`, and the context of `feat-a` is deleted with #123: until its next
push, #124's base is gone, which must cost cache misses only, never a failure.

### Approach A: layers, with their bases recorded

Each push of a context records its base next to its trees, `.xcraft/ccache/<context>/base`, signed like them. A fetch
follows the bases from the head's context, to master at most, and takes the entries of all the layers, those of the
nearer ones first, for the ccache manifests of the same name. A push uploads the entries the build used or made which
the layers below do not have, by name for the results, by content for the ccache manifests, and lists in its manifest
only those.

- Storage and transfers: a context only has what its pull request changed; nothing is copied when it starts.
- Fresh: each fetch reads the bases as they are, with master's latest entries, and #123's for #124.
- A missing base, like `feat-a` once merged, stops the chain there: xcraft goes on with master.
- More to get right: the manifests of several layers, read and checked together, the order of the ccache manifests,
  the chain, with a limit against loops, and a push which depends on the bases it read.

### Approach B: a full copy of the base when a context starts

The first push of a context copies the trees of its base, server-side, as xcraft already copies the snapshots it
reuses, then pushes its build's entries as master does. A fetch reads one context, the head's, or else its base's.

- Simple: one tree to read and check, no chain, the base used once.
- Storage: low, as Garage deduplicates the blocks, and the entries are compressed again the same way, at level 9.
- Requests: a copy by entry, of all the packages of the base: thousands for the kernel, xen and qemu, minutes for the
  first push of each pull request.
- Stale: the copy is the base when the pull request started; master's later entries, and those of #123 for #124,
  never come, even when the pull request is rebased on them.
- A deleted base goes unnoticed, even when the pull request should now follow another.

### Approach C: a copy by package, at its first build in the context

As B, but a package's trees are copied from the base only at the first build of the package in the context, the
packages a pull request does not change being fetched as builds rather than compiled. A fetch reads the package's tree
of the head's context, or else of its base's: one fallback, without a recorded chain.

- Requests: only for the packages the pull request compiles, once each.
- Simple as B, but for the fallback of the packages it did not copy yet.
- Stale as B, for the packages it copied.

### Comparison

| | A: layers | B: full copy | C: copy by package |
|---|---|---|---|
| Fetch | all the layers, in order | one tree | one tree, or the base's |
| First push of a pull request | its new entries only | a copy by entry of the whole base | a copy by entry of the packages it builds |
| Storage | its new entries | deduplicated by Garage | deduplicated by Garage |
| Base's later entries | yes | no | no, for the copied packages |
| Stacked pull requests | followed through the recorded bases | the base when the context started | the base when the package was copied |
| Base deleted or retargeted | cache misses until the next push | unnoticed | unnoticed for the copied packages |
| Code | the most: layers, chain, manifest order | the least | between |

In all three, a context is deleted with its pull request, signed with the PR key in the pr bucket, and never written
by forks; out of CI, nothing changes.

## Open questions

- The approach of the compiler caches.
- Whether Garage's version has an expiry of the objects of a bucket, for the pr bucket.
- The command deleting what a pull request left, and whether master keeps a stage of its own, like `incoming`.
- The labels publishing the builds of forks, if wanted.
- Whether the long-lived branches, like `8.3`, get master's workflow, pushing to the ci bucket.
