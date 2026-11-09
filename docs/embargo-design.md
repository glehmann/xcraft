# Design: security fixes under embargo

Status: proposed.

## The problem

A security fix under embargo is developed in private: in a private repository, with its builds in a private bucket,
tested privately on hosts with xcp-ng-tests. When the embargo ends, the fixed packages must be released at once,
without rebuilding them, and join the public repository and bucket. The fix is made on the state users have, the
latest release, not on master, which went on since, and it may change during the embargo. It must then be merged into
master too, so that master has it.

Today, this fails on the numbers. A branch forked from the released commit, R, numbers its builds against master
where it forks from it: R. If xen is `+b42` there, the fixed xen gets `+b43`, which master may have given another xen
since: two different packages with the same NEVRA, which dnf does not move between. Bumping the `Release:` of the spec
only moves the collision, to the next bump of master.

## Goals

- Numbers of the fixed builds which cannot collide with master's, upgrading from the release, and upgraded by master's
  next one.
- Final builds made in private, tested, then published as they are, without a rebuild.
- Nothing of the embargo reaching a public repository or bucket before its end, by mistake either.
- The fix merged into master afterwards, keeping the builds of the fix wherever master's are the same: otherwise the
  users would update the same packages twice, at the security release, and at the next one, only to come back to
  master's numbering.

## Maintenance branches

A maintenance branch is a branch of official builds, like the main branch, forked from a release. Its builds are
final ones, numbered `+b<n>.<m>`: `n`, the number of the package in the release, and `m`, counting the builds of the
package on the branch. RPM sorts `+b42` < `+b42.1` < `+b42.2` < `+b43`: the fixed builds upgrade the release's, and
the next build of master upgrades them. Master's numbers have no dot, so they never collide.

### Which branches

The branches whose names match `maintenance_branches` of `xcraft.toml`, `maint/*` by default, like `maint/9.0`, as
`origin/<name>` or else `<name>`, as for `main_branch`. The setting is the same on all the branches, so merging a
maintenance branch into master changes nothing to it.

Not the current branch's name: with jj, the HEAD of git is detached, and CI checks out a commit. The place of the
commit in the history tells, as `_on_main()` and `_fork()` of `xcraft/buildid.py` do for the main branch:

- a commit which is an ancestor of a maintenance branch is on it: final builds, numbered as below, checked by
  `--locked` as on the main branch. This is checked before the main branch: once the branch is merged into master, its
  commits are ancestors of master too;
- a commit whose fork point with a maintenance branch descends from its fork point with master, like a developer's
  branch of the fix, is numbered against the maintenance branch, with development builds, `+b42.1~1.dev`, below the
  final `+b42.1`;
- otherwise, as today, against master.

### Numbering

The number of a package is computed from the build IDs of the fork point, on the maintenance branch, as on master,
with another next number:

- the same key: the same build ID;
- another key: `n.1` after `n`, `n.(m+1)` after `n.m`;
- a greater epoch-version-release, like a new upstream release fixing the vulnerability: `0.1`, below master's `1`.

This needs no knowledge of the release the branch forks from, R, only of the build IDs before: the fork point of the
branch with master is not R anymore once it is merged.

Each official number must be given once: one maintenance branch at a time for a release, which all its fixes go to,
under embargo or not. Two embargoes at the same time share the branch.

## The life of a maintenance branch

The branch only moves through its own fixes, merged into it like pull requests into master, never through releases.

- **Its start.** When an embargo starts, the branch is created in the private repository from the commit of the release
  users have: that of the latest snapshot of the stable stage. See [The released commit](#the-released-commit).
- **During the embargo.** The fixes are developed on branches of it, and merged into it. The CI of the private
  repository builds each merge, `xcraft build --locked`: the packages changed and those built against them, pushing
  their final builds to the private bucket. The others keep the release's keys, and builds, fetched from the main
  bucket. A snapshot published to the private bucket, or `xcraft repo --host`, gives them to the hosts of
  xcp-ng-tests: the builds tested are those released.
- **Its end.** See [The release](#the-release), then the branch is merged into master and removed. The next embargo
  creates another one, from the release current then.

A branch living longer, for a release still supported while master is on the next major one, like `maint/8.3` next to
a 9.0 master, works the same way: created when master moves to the next major release, it gets all the fixes of that
release, and is merged into master from time to time.

**A release of master during the embargo**, with packages of the fix not fixed, takes the users above the fixed builds:
`+b43` without the fix is above `+b42.1`. Either the releases of master wait for the end of the embargo, or the fix is
taken to a new maintenance branch, created from that release, and the previous one is dropped. Its numbers were never
published, so they do not count: a number must only be unique once published.

## Keeping the embargo private

The private repository is a full repository, with CI runners and secrets: the temporary private forks of the GitHub
security advisories run no Actions.

**The private remote.** The private bucket is on the same S3 server as the public ones, so the copies are server-side.
It must not be in the committed `xcraft.toml`: the change would be merged into master with the rest. So the remotes
also come from a configuration out of git, `.xcraft/config.toml`, or the file of `$XCRAFT_CONFIG` for CI, whose
`[remote.<name>]` tables are added to those of `xcraft.toml`:

```toml
[remote.embargo]
url = "https://xcraft-embargo.example/"
s3_endpoint = "https://s3.example/"
bucket = "xcraft-embargo"
region = "garage"
private = true
build_keys = ["<fingerprint of the key of the private CI>"]
```

The private bucket is read with credentials, rather than over public http(s).

**No leak.** A private remote makes the others read-only: `push` and `publish` to them fail, like `--final` builds
pushed to the dev bucket, while the private remote is configured. The builds, the compiler caches and the
resolutions are fetched from the main bucket first, then from the private one. So are the sources: a new upstream
archive, like the release fixing the vulnerability, is pushed to the private bucket only.

**The signing key.** The private CI signs with a key of its own, in `build_keys` of the private remote, and of the main
one, committed on master beforehand: a public key reveals nothing. The builds copied keep their signatures, trusted
there.

## The release

1. **The copy.** The builds of the maintenance branch's commit which the private bucket has, and the source archives of
   its manifests, are copied to the main bucket, server-side, as they are: `xcraft push --from embargo --to main`.
   A job of a protected environment does it, with the credentials reading the private bucket and writing the main
   one.
2. **The branch, public.** The maintenance branch is pushed to the public repository. Its CI, `xcraft build --locked`,
   fetches all its builds: nothing is built.
3. **The publication.** `xcraft publish` of the branch's commit to the stable stage, as for a release of master: the
   snapshot has the fixed builds, and the release's for the rest, copied server-side, without a rebuild.
4. **The merge into master**, through a pull request, see below.

## Merging into master

Master keeps a build ID of any parent of the merge, not only of its first one, when its key is the same: the fixed
packages, when master changed none of their inputs since R, and those built against them, whose keys have the numbers
and keys of what they are built against. So the security release's packages are those of master's next release: the
users download them once, and publishing copies them.

When master changed an input of a package since R, its build differs from the fixed one anyway: the users would get a
new package with master's next release, embargo or not. It gets master's next number, the next integer above all the
numbers given, `+b43` after `+b42.1`. A lock update of a package of the image, or a change of the recipe, changes all
the keys: then the next release of master rebuilds everything anyway. `_SAME_BUILDS` and `xcraft reuse` hold as on
any branch: an ABI compatible update on master can be accepted for keeping the fixed builds.

A build ID of another parent is only kept when its number is above the first parent's: if master gave `+b44` to xen,
then got its previous key back, by a revert, keeping `+b42.1` would take its hosts down.

With `--locked`, on master, a number taken from another parent than the first must have the same key there, and the
check of the numbers given again for other keys holds for the dotted numbers too.

So the record of the official numbers stays the history of the main branch: those of the maintenance branches are in
it, once merged.

## The released commit

A maintenance branch starts from the commit of the release users have, which xcraft does not record today: the
inputs of a snapshot, `inputs.json`, have the build keys, not the commit. `xcraft publish` records the commit in the
signed inputs of the snapshot, and the CI publishing to the stable stage tags it, `release/<release>/<stage>/<date>`,
to create the branch from.

## The changes

| Change | Where |
|---|---|
| `maintenance_branches`, the place of a commit: maintenance branch, branch of one, or master | `project.py`, `buildid.py` |
| The numbers `+b<n>.<m>`, in `xcraft.builds`, `%{autorev}` and the build paths | `buildid.py`, `buildkey.py`, `build.py` |
| Master keeping the build IDs of other parents with the same key, and `--locked` checking them | `buildid.py` |
| The remotes out of git, `private`, the other remotes read-only then, credentials for reading | `project.py`, `remote.py` |
| `xcraft push --from <remote>`: server-side copies of the builds and sources of the commit | `commands/push.py`, `remote.py` |
| The commit of a snapshot in its inputs, and the release tag of CI | `publish.py`, xcp-ng-dist's workflow |

## Open questions

- Rebuilding an old commit of a maintenance branch, once the branch is merged and removed: its commits are only
  master's ancestors then. Their builds are in the storage, by their keys, but `--locked` would number them as master's.
  The release tags of the maintenance branch's releases, as maintenance branches, would keep them apart.
- The compiler caches of the private builds: pushed to the private bucket, and not copied, or copied with the builds.
- What the hosts of xcp-ng-tests need to read the private bucket: a snapshot copied to them with `xcraft repo --host`,
  or the credentials of the bucket.
