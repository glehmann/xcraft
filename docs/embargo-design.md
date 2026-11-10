# Design: security fixes under embargo

Status: implemented in xcraft: the maintenance branches and their numbers, the private remote storage, the copies of
`xcraft push --from private`, the commit of the snapshots and `xcraft maint start`. To do: the release tags of the CI
of xcp-ng-dist, its workflows for the maintenance branches, the ruleset of its public repository denying the embargo
branches, and finding the commit of a release by its contents.

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

The branches whose names match the patterns of `maintenance_branches` of `xcraft.toml`, `["maint/*"]` by default,
like `maint/9.0`, or `embargo/xsa-123` with `embargo/*` too, as `origin/<name>` or else `<name>`, as for
`main_branch`. The setting is the same on all the branches, so merging a maintenance branch into master changes nothing
to it.

Not the current branch's name: with jj, the HEAD of git is detached, and CI checks out a commit. The history tells.

A maintenance branch just made, at the release, has no commit of its own yet: only master's. A fork point which is its
tip is one of its commits, while master went on since: a branch of it is then a maintenance one, but an old branch of
master forked at the release too, whose pull request numbers it again, see below. While master is still at the
release, both are the same commit, which only the branch tells apart:
- on a maintenance branch, git's current branch, or with jj, the nearest bookmark of the main and maintenance branches
  on `@` and its ancestors, like while editing the revision of `maint/xsa-123`, which git sees as changes on master,
  the builds are numbered against it;
- `xcraft --maintenance <branch>`, or `$XCRAFT_MAINTENANCE`, numbers them against the branch it names, the main one or
  a maintenance one, rather than by the fork points: CI gives it the target of the pull request, or the branch pushed.

Once the maintenance branch has a fix, its commits tell it apart anyway.

### Whose commit it is

Being an ancestor of a branch does not tell: the commits before R are ancestors of the maintenance branch too, and
once it is merged, its commits are ancestors of master. A commit belongs to the branch whose **first-parent chain**
brought it:

- master's commits are those of its first-parent chain, R included, as it was released from master;
- a maintenance branch's commits are those of its first-parent chain which are not on master's: from after R to its
  tip. Below R, its chain is master's;
- once merged into master, they stay maintenance commits: master reaches them through the second parent of the merge,
  not through its first-parent chain. A ref must still reach them once the branch is removed: the release tags, see
  [The released commit](#the-released-commit).

A commit of a maintenance branch gets final builds, numbered as below, and checked by `--locked` as on the main
branch.

A commit of no official branch, like one of a developer's branch, is numbered against the most recent of its fork
points with master and the maintenance branches: the one which descends from the others. It is a maintenance commit
for a branch of a maintenance branch, else a commit of master, as today.

So the merges of a maintenance branch into master must be merge commits, even when master did not move: a
fast-forward would put the maintenance commits on master's first-parent chain, and a squash would replace them by one
of master's, both with dotted numbers master never gives. On GitHub: "Create a merge commit". `--locked`, on master,
checks it: a dotted number only comes from the second parent of a merge.

Each official branch's first-parent chain is one `git rev-list --first-parent`, once by command. Master's is short,
even with the imported histories, which come through merges.

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

### Development builds

A branch of a maintenance branch gets development builds, as a branch of master does: the number its package would
get once merged, and the suffix, `~<c>.<ident>`. With xen at `+b42.2` on `maint/9.0`, without the `%{dist}`:

```
1+b42.2         the latest build of maint/9.0
1+b42.3~1.dev   the developer's builds
1+b42.3~2.dev
1+b42.3         once merged into maint/9.0
1+b43           the next build of master
```

RPM sorts them in this order: `42.3~1` is above `42.2`, 3 above 2, below `42.3`, as `~` sorts before anything, and
below `43`. So a test host upgrades from each to the next one. The counter of `.xcraft/dev-builds.json` is by package
and number, `42.3` as any other, and `$XCRAFT_DEV_COUNTER` holds for the CI of the pull requests of the private
repository, `+b42.3~<run>.pr<M>`. A new epoch-version-release gives `+b0.1~1.dev`. The packages the branch does not
change keep the final builds of the maintenance branch, fetched from the private bucket. Two branches of `maint/9.0`
changing xen both get `42.3`, as two branches of master both get `43`: merging the second one conflicts, and
computing the build IDs again gives it `42.4`. Development builds are never published, so their numbers do not count.

## The life of a maintenance branch

The branch only moves through its own fixes, merged into it like pull requests into master, never through releases.

- **Its start.** When an embargo starts, the branch is created in the private repository from the commit of the release
  users have: that of the latest snapshot of the stable stage. See [The released commit](#the-released-commit).
- **During the embargo.** The fixes are developed on branches of it, and merged into it. The CI of the private
  repository builds each merge, `xcraft build --locked`: the packages changed and those built against them, pushing
  their final builds to the private bucket. The others keep the release's keys, and builds, fetched from the main
  bucket. `xcraft repo <pkg>... --host <host>`, without `--upgrade`, gives them to the hosts of xcp-ng-tests: it
  copies their repository over SSH, in `/var/local/xcraft/<ID>/`, and leaves its `.repo` file, of a `file://` URL,
  for the tests to install or upgrade the packages from it, next to the usual repositories of the host. Nothing is
  exposed over HTTP, and the builds tested are those released. `--dependents` adds the packages built against them.
  A test needing the whole repository of the distribution, rather than the packages of the fix, would need
  `xcraft publish --local` to copy its snapshot to a host the same way, with `--host`: to do when one does. The
  private bucket has no public URL; serving it over HTTP to the hosts would need an authenticating proxy in front
  of Garage, which serves its websites without authentication.
- **Its end.** See [The release](#the-release), then the branch is merged into master, with a merge commit, and
  removed. The next embargo creates another one, from the release current then.

A branch living longer, for a release still supported while master is on the next major one, like `maint/8.3` next to
a 9.0 master, works the same way: created when master moves to the next major release, it gets all the fixes of that
release, and is merged into master from time to time.

**A release of master during the embargo**, with packages of the fix not fixed, takes the users above the fixed builds:
`+b43` without the fix is above `+b42.1`. Either the releases of master wait for the end of the embargo, or the fix is
taken to a new maintenance branch, created from that release, and the previous one is dropped. Its numbers were never
published, so they do not count: a number must only be unique once published.

**A branch started from master's tip** is the usual case when that tip is the release. When it is not, the numbers
still hold: with xen at `+b43` there, not released, the fix gets `+b43.1`, and master's next build `+b44`. But the
security release then has all that master had and had not released, under master's numbers: a release of master with
the fix, rather than a fix of the release, which xcp-ng-tests tests as such. Fine when the tip was about to be released
anyway: `xcraft maint start` starts from the commit of the latest snapshot, a published release, unless `--at`.

**Without a maintenance branch**, a private branch of master numbers its builds against its fork point, as any branch:
`+b43`, which master may give to another xen meanwhile. Published as the security release, they would have the NEVRAs
of other packages, and merging the branch would only renumber it once those are out. So the fix needs a maintenance
branch, even started from master.

## The cases

The commits go from left to right, R is the released commit, and the numbers are those of xen.

**During the embargo.** The first-parent chain of `maint/9.0` is M2, M1, R, B, A: only M1 and M2 are not master's, so
they alone are maintenance commits.

```
master       A ── B ── R ── C ── D                 R: xen +b42, released; C: +b43, another change
                        \
maint/9.0                M1 ── M2                  M1: xen +b42.1, the fix; M2: +b42.2, the fix revised
```

**A branch of master.** Its fork points are D with master, and R with `maint/9.0`: D descends from R, and is master's,
so the branch is numbered against master, `+b44~1.dev`.

```
master       A ── B ── R ── C ── D
                        \         \
maint/9.0                M1 ── M2  X1 ── X2
```

**A branch of the maintenance branch.** Its fork points are R with master, and M2 with `maint/9.0`: M2 descends from
R, and is a maintenance commit, so the branch is numbered against `maint/9.0`, `+b42.3~1.dev`, then `+b42.3` once
merged into it.

```
master       A ── B ── R ── C ── D
                        \
maint/9.0                M1 ── M2
                                \
                                 Y1 ── Y2
```

**The merge into master.** The first-parent chain of master is E, D, C, R, B, A: M1 and M2 come through the second
parent of E, and stay maintenance commits. xen keeps `+b42.2` on E if its key is M2's, when master changed none of its
inputs since R: not here, C changed xen, so it gets `+b44`.

```
master       A ── B ── R ── C ── D ─────── E       E: the merge, first parent D, second parent M2
                        \                 /
maint/9.0                M1 ── M2 ───────
```

**A fast-forward**, master having not moved since R, puts M1 and M2 on master's first-parent chain, with dotted
numbers master never gives: so the merge commits are mandatory.

```
master       A ── B ── R ── M1 ── M2               M1 and M2 on master's first-parent chain
```

**After the merge, the branch removed.** The release tag of M2, published, keeps M1 and M2 reachable, as a chain of
their own: a rebuild of M2 is a maintenance build, checked as `+b42.2`.

```
master       A ── B ── R ── C ── D ─────── E ── F
                        \                 /
release/9.0/stable/…     M1 ── M2 ───────
```

**A release of master during the embargo.** The users on R2 are above `+b42.1`: the fix is taken to a new maintenance
branch, from R2, and the previous one dropped, never published.

```
master       A ── R ── C ── R2 ── D                R2: xen +b43, released, without the fix
                   \         \
maint/9.0 (dropped) M1        N1                   N1: the fix, xen +b43.1, the new maint/9.0
```

**A branch living longer.** Each merge into master, D and E, is a merge commit, and M1 to M5 stay on the chain of
`maint/8.3` only; M4 numbers after M3's `+b<n>.<m>`, on its own chain.

```
master       A ── R ── C ──────── D ──────── E ── F
                   \             /          /
maint/8.3           M1 ── M2 ── M3 ── M4 ── M5
```

## Keeping the embargo private

The private repository is a full repository, with CI runners and secrets: the temporary private forks of the GitHub
security advisories run no Actions. Its CI fetches master with the maintenance branch, and that of master fetches the
maintenance branches and the release tags: the first-parent chains tell whose commit it is, see above.

**The private remote.** The private bucket is on the same S3 server as the public ones, so the copies are server-side,
with a key reading it and writing the main one: the credentials are by server, see
[Remote storage](remote-storage.md). Its configuration is in `xcraft.toml`, like the others: it holds no secret, the
bucket is private, and its credentials are. It is used on the embargo branches only, `embargo_branches`, maintenance
branches too, and their branches, as told for their numbers, see above: elsewhere, like on master, which has the same
`xcraft.toml`, it is neither read nor written, as public CI and developers have no credentials for it.

```toml
maintenance_branches = ["maint/*", "embargo/*"]
embargo_branches = ["embargo/*"]

[remote.private]
s3_endpoint = "https://s3.example/"
bucket = "xcraft-private"
region = "garage"
build_keys = ["<fingerprint of the key of the private CI>"]
```

The private bucket is read through S3, with the credentials of its server, rather than over public http(s): it has no
`url`.

**No leak.** On an embargo branch, the commands write to no other bucket, configured or not: `push`, `publish` and
`unpublish` to main or dev fail, but the copies of `xcraft push --from private`. So the guard follows the branch: a
developer cannot forget it. The builds, the compiler caches and the resolutions are fetched from the main bucket first,
then from the private one. So are the sources: a new upstream
archive, like the release fixing the vulnerability, is pushed to the private bucket only.

**The signing key.** The private CI signs with a key of its own, in `build_keys` of the private remote, and of the main
one, committed on master beforehand: a public key reveals nothing. The builds copied keep their signatures, trusted
there.

**Git.** xcraft guards the buckets, not the pushes of git: an embargo branch pushed to the public repository, or to a
fork of it, is public, and so are the logs of its CI. So:

- **The safety net, on GitHub.** A ruleset of the public repository forbids the creation of the `embargo/**`
  branches: a push of one fails, whoever makes it. At the end of the embargo, the branch is pushed under a maintenance
  name, see [The release](#the-release): no bypass. It does not cover the forks of the developers, and it is a net for
  the mistakes, not a barrier: the same commits can be pushed under another name, which the release does on purpose.
- **A clone of its own.** The work under embargo is made in a clone of the private repository, not in one of the
  public repository with another remote: a push goes to the private repository by default. The public repository, to
  fetch master, is added without pushing: `git remote add public <url>`, then
  `git remote set-url --push public DISABLED`.
- **With jj.** No `jj git push --all`, nor `--tracked`, which push all the bookmarks; in the public clones,
  `git.private-commits = "::bookmarks(glob:'embargo/*')"` in the configuration of the repository refuses to push them.

**The checklist of the developers:**

- work in the clone of the private repository only, on an `embargo/*` branch, or a branch of one;
- never push all the branches, with `--all`;
- nothing about it in a public issue, pull request, or chat;
- no log, nor code, given to a service out of the predisclosure agreement, AI tools included, unless it allows them;
- the release, see below, rather than the branch pushed to the public repository by hand.

## The release

The embargo is lifted by a rename: `embargo/xsa-123` is pushed to the public repository as `maint/xsa-123`, a
maintenance branch of no embargo. Its commits stay the same, and so do their build IDs and keys, which no branch name is
in: the builds tested in private are those released. From the tested tip of the embargo branch, T:

1. **The copy.** In the clone of the private repository, on the embargo branch, at T: the builds of T which the
   private bucket has, and the source archives of its manifests, are copied to the main bucket, server-side, as they
   are, `xcraft push --from private --to main`, for each architecture, those signed by a key main trusts. It is
   allowed on the embargo branch, as the copy of the release. A job of a protected environment can do it, with the
   credentials reading the private bucket and writing the main one.
2. **The branch, public, renamed.** `git push public T:refs/heads/maint/xsa-123`: the push of the public repository,
   disabled in the clone of the private one, enabled for it, or from a clone of the public one, having fetched T. The
   ruleset does not deny it, which denies `embargo/**` only. Its CI, `xcraft build --locked`, fetches all its builds
   from the main bucket: nothing is built. So the copy comes first: on `maint/xsa-123`, the private bucket is not read
   anymore, and the builds missing in the main one would be built again, rather than those tested.
3. **The publication.** `xcraft publish --to main --stage stable` of `maint/xsa-123`, at T, as for a release of master,
   which the embargo branch refuses: the snapshot has the fixed builds, and the release's for the rest, copied
   server-side, without a rebuild. It records T, which the release tag, `release/<release>/stable/<date>`, gets.
4. **The merge into master**, through a pull request of `maint/xsa-123`, with a merge commit, see below.
5. **The cleanup.** `maint/xsa-123` is removed from the public repository once merged, its commits kept by the release
   tag, and `embargo/xsa-123` from the private one. The private bucket can be emptied later.

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
inputs of a snapshot, `inputs.json`, have the build keys, the locked upstream packages and the configuration, not the
commit. Three ways, together:

- **The signed inputs.** `xcraft publish` records the commit in the inputs of the snapshot, signed with the rest: the
  key of the stage vouches for it, for all the stages: that of the publication, a snapshot reused too,
  `<commit>-dirty` with changes in the checkout, and with jj, the commit of its working copy, or its parent's while it
  is empty. The commit users have is then that of the latest snapshot of the stable stage, its
  signature checked: `xcraft maint start [9.0] [--stage stable]` creates the branch at it, or at the commit of `--at`,
  for the snapshots published before.
- **The release tags.** The CI publishing to the stable stage tags the commit, `release/<release>/stable/<date>`: for
  git and people, to branch from, and to keep the commits of a maintenance branch reachable once it is removed. A tag
  can be moved or removed: it is a convenience, checked against the inputs, unless signed and protected.
- **The contents.** A commit is the release when it makes the same snapshot: the same input key of `inputs.json`,
  which `xcraft publish` already computes to reuse a snapshot. Computed for the commits of master, it finds those of
  the snapshots published before, and checks the two above when creating a branch. Several commits can match, like
  those changing the documentation only: any of them makes the same packages, the first one is taken.

## The changes

| Change | Where | Status |
|---|---|---|
| `maintenance_branches`, whose commit it is by the first-parent chains, the most recent fork point of the others | `project.py`, `buildid.py` | done |
| The numbers `+b<n>.<m>`, and `+b<n>.<m>~<c>.<ident>`, in `xcraft.builds`, `%{autorev}` and the build paths | `builds.py`, `buildid.py` | done |
| Master keeping the build IDs of other parents with the same key, and `--locked` checking them and the merge commits | `buildid.py` | done |
| `embargo_branches`, `[remote.private]` read through S3 on them only, the other remotes not written there | `project.py`, `buildid.py`, `remote.py`, `commands/__init__.py` | done |
| `xcraft push --from private`: server-side copies of the builds and sources of the commit | `commands/push.py`, `remote.py` | done |
| The commit of a snapshot in its inputs | `upload.py` | done |
| `xcraft maint start [<release>]`: the branch at the commit of the latest stable snapshot | `commands/maint.py` | done |
| `xcraft --maintenance <branch>`, `$XCRAFT_MAINTENANCE`: the builds numbered against the target of the branch | `buildid.py`, `__main__.py` | done |
| The release tags of CI, the workflows of the maintenance branches, fetching master and the maintenance branches | xcp-ng-dist's workflows | to do |
| The commit of a release by its contents, for the snapshots published before | `commands/maint.py` | to do |

## Open questions

- The compiler caches of the private builds: pushed to the private bucket, and not copied, or copied with the builds.
- What the hosts of xcp-ng-tests need to read the private bucket: a snapshot copied to them with `xcraft repo --host`,
  or the credentials of the bucket.
