# Design: automatic rebuild releases and development builds

Status: decided, being implemented. The preferred model uses per-package build IDs and development builds by default.
The earlier automatic spec-bump proposal is kept below for comparison.

## Goal

Make release handling follow the rebuilds xcraft already computes, both for upstream lock updates and changes to our
packages. A developer should be able to edit, build, test on a host, and repeat. Each test build must upgrade the
previous one, but repeated development builds should lead to only one new official build per affected package.

Rebuilding is the default. A maintainer can explicitly retain existing builds when a dependency update does not
require rebuilding, so an ABI-compatible AlmaLinux security update can ship without rebuilding the distribution.

## Current behavior

Build keys cover the package directory, build environment, upstream build dependencies and the keys of our build
dependencies. Updating any of them invalidates the affected builds, including dependent packages transitively.

- `xcraft lock --update` updates upstream versions and records `[pending.<pkg>]` entries in `xcraft.lock`.
- `xcraft bump` changes the selected packages' releases and clears their pending entries. It does not automatically
  bump their dependents.
- `xcraft ack` only clears pending entries. It does not preserve build keys or prevent rebuilding.
- `--pre ID` produces `<release>~ID.<n>`, below the spec's release. `--test ID` produces
  `<release>.0.ID.<n>`, above it and below the next release. Their counters are local to the checkout.
- `xcraft repo --pre` numbers and publishes the explicitly selected packages, without automatically including their
  affected dependents. `--dependents` currently exists on `build`, but not on `repo`.

Consequently, a dependent can rebuild with different contents but keep its published version-release. Also,
acknowledging a libc update still leaves almost every build invalidated by the changed build environment.

## Preferred model: build IDs and default development builds

### Package releases and build numbers

Keep the spec's packaging release separate from an automatic build number, supplied through `%{autorev}`, at the
start of `%{dist}`: a spec with `Release: 3%{?dist}` produces the official builds `3+b42.xcpng8.99` and
`3+b43.xcpng8.99`. A changed package or dependency gets a new official build without editing any spec. Source changes
and intentional version/release changes still belong in the package directory, with `xcraft bump`.

This follows the bitbake prototype's approach. In `dnf-bridge/meta/classes/rpm.bbclass`, the PR service looks up a
revision using the recipe version, architecture and build hash, then the build receives `autorev +b<N>`.

Every package must end its `Release` with `%{?dist}`, or, for the package defining `%{dist}`, with `%{?autorev}`:
xcraft does not handle other packages. Builds made before the numbers are not a concern, as the distribution is still
a prototype, even though `3.xcpng8.99` sorts above `3+b1.xcpng8.99`.

### One build key for all the architectures

A package has one build key, the same on all the architectures. It covers what goes into the package on any of them:

- its package directory, and the build recipe: xcraft's, the base image, the packages and macros of the build
  environment, and its repositories;
- the upstream packages of the build environment image and of its build roots, on all the architectures, in their
  locked versions;
- the build keys and numbers of the packages of ours it is built against.

It excludes its own build number, which it decides. A change affecting one architecture only, like an aarch64-only
upstream update, rebuilds and renumbers the package on all of them, which is accepted. Packages depending on each
other, in a dependency cycle, share their key: they are rebuilt, and renumbered, together.

The lock records what the key needs, so it is computed from the lock alone, on any machine: besides the versions of
the upstream source packages, `[image]` has the upstream source packages of the image of each architecture, and
`[roots]` those of the build root of each package, on each architecture. The commands changing the lock, like the
builds recording what they found, or `xcraft lock --update`, resolve all the architectures, from their cached
resolutions, so that they stay complete; `--locked` fails when they would change.

The builds are stored, locally and in the remote storage, by architecture, under their key and release suffix:
`builds/<arch>/<pkg>/<key>+b42/`.

### Build IDs

A package's build ID is its build number with its build key. It is in `.buildid/<package>`, tracked, one line:

```text
42 3f1c…e2a0
```

The other inputs of the key are already in the repository, in the package directory, `xcraft.toml`, `xcraft.lock`,
and the other `.buildid` files: the reason of a new number is in the same commit, as their diff.

The main branch is `main_branch` in `xcraft.toml`, `master` by default. Official builds are only made by CI on the
main branch, so the first-parent history of its `.buildid` files is the record of the official numbers: no server, nor
registry in the remote storage, is needed. A package keeps the build ID of the main branch, where the current branch
forks from it, or of the main branch's tip, while its key is the same, the fork's first; otherwise it gets the next
number of both. So:

- a branch proposes the same number however many times it changes the package;
- reverting a change brings back the main branch's build ID, withdrawing the proposal;
- two unmerged branches can both propose 43: number and key on one line, both change it, so merging the second one
  conflicts, and recomputing the file resolves it to 44, the main branch's tip counting even while the merge is in
  progress.

Out of a git repository, or without the main branch, there is nothing to number against: the numbers stay those of the
working tree, 1 for new packages.

The builds check the files, like `--locked` checks the lock: in CI, every build checks that the committed files are
those xcraft computes, failing otherwise; locally, xcraft updates them. On the main branch, a key differing from the
first parent's must come with a greater number than the first parent's, so that no number is given to two different
builds, even when a merge conflict was resolved by keeping one side. This needs that commit in CI, with
`fetch-depth: 2`, and relies on the main branch's history never being rewritten, and on one CI run at a time on it,
per architecture.

### Development builds

Building defaults to development builds, of the packages whose build ID is not the main branch's: those changed, and
those built against them. The others get their final builds, fetched from the remote storage as before. A development
build has the target build number, and a suffix with a counter and an identifier, `dev` by default:

```sh
xcraft build xen                         # 3+b43~1.dev
xcraft build xen                         # cached: the same inputs
xcraft build xen                         # 3+b43~2.dev, after a change
xcraft build xen --dev qcow2              # 3+b43~3.qcow2
export XCRAFT_DEV=qcow2                  # the identifier of a shell or session
xcraft build xen --final                  # 3+b43
```

The counter is local to the checkout, by package and build number, not by identifier: it comes first, so that the
builds are ordered by time, whatever their identifiers. It goes up when the inputs of the build or the identifier
change, and only then, so a build of the same inputs is cached. It survives cleaning the builds, and withdrawing a
proposal. With the distribution suffix, as `rpm.vercmp` checks:

```text
3+b42 < 3+b43~1.dev < 3+b43~2.dev < 3+b43~3.qcow2 < 3+b43
```

`--final` builds them all as final builds, overriding `$XCRAFT_DEV`, but not with `--dev`. On the main branch, all
the builds are final ones. All the commands support the development builds: `build`, `repo`, `status`, `push builds`,
and the others. Development builds are never pushed to the remote storage, nor taken from it. They replace the test
and pre builds, `--test` and `--pre`, which are removed.

A test host gets the development builds with `xcraft repo --host --upgrade`. It warns when the host does not upgrade a
package because it has a newer one installed. Bringing a host back to an earlier state, like after a proposal is
withdrawn, is the developer's responsibility, eased by restoring a btrfs snapshot.

### Reusing builds

Rebuilding with a new number is the default. A maintainer can instead reuse the main branch's build of a package,
despite changes, like after an ABI-compatible security update:

```sh
xcraft reuse some-dependent              # despite any change but to its own directory
xcraft reuse --dep openssl               # the packages whose only changes are updates of openssl
```

`xcraft reuse <package>` reuses the build of the named packages, whatever changed but their own directories, like the
packages of ours they are built against. `xcraft reuse --dep <upstream>` selects the packages whose only changes since
the fork point are updates of the given upstream packages, binary or source names, found by comparing the lock there
with the current one: those with other changes still get new numbers.

The build ID file keeps the number and the key of the reused build, and adds the key it accepts:

```text
42 9ab4…77d1 accepts 3f1c…e2a0
```

This is an equivalence of keys, chosen by the maintainer and stored in git, like those of xcraft's own recipe changes
that make the same packages. It holds while the current key is the accepted one: another change gives another key,
and the package a new number, as usual, dropping the equivalence. The packages built against a reused one have its
reused key in theirs, so they keep their builds too, unless they changed otherwise. A reused package is never built
again: its builds are those of the remote storage, under the reused key and number, on every architecture.

There is no other decision to make on an update: the lock no longer has pending updates, and `xcraft impacts`,
`xcraft ack` and `xcraft bump --dep` are removed.

### Publishing

CI only publishes the main branch. Publishing from another branch is possible to the dev bucket, the publisher being
responsible for the numbers in their stage.

### Prior art

- openSUSE's Open Build Service gives releases a build counter kept by the server, `<checkin>.<build>`: the central
  allocation this model avoids.
- Fedora's rpmautospec computes `%autorelease` from the git history, without a server, like the numbering above,
  though from the number of commits rather than from build keys.

## Earlier alternative: automatic spec release bumps

This earlier proposal edits specs to prepare rebuild releases. It is retained for comparison; its state location and
command examples are not the preferred model above. Both its development and lock-update workflows use the same
release-proposal mechanism, without a mandatory separate release-preparation command.

### Development builds

After editing xapi's sources, patches or spec, the developer runs:

```sh
xcraft repo xapi --dependents --pre fix123 --host root@testhost --upgrade
```

The command prepares the release changes, builds the packages, creates their test repository and upgrades the host.

On its first invocation, xcraft:

1. Compares the current inputs with a recorded release baseline.
2. Finds the changed packages and affected dependents, transitively, using the build dependency graph.
3. Prepares one release bump for each package needing it. A package whose version-release has already been increased
   appropriately does not get an additional bump.
4. Adds generated changelog entries to the dependents, listing the dependency changes responsible for rebuilding.
5. Gives the affected packages development suffixes and builds them against each other.
6. Includes those packages in the repository copied to the test host. Unchanged prerequisites can reuse existing
   builds.

For example, omitting the distribution suffix:

| Package | Released RPM Release | First test | Second test | Final release |
|---|---|---|---|---|
| xapi | `2` | `3~fix123.1` | `3~fix123.2` | `3` |
| A dependent | `7` | `8~fix123.1` | `8~fix123.2` | `8` |

The developer edits and repeats the same command. The spec releases stay at `3` and `8`; only the development build
numbers increase. Each iteration upgrades the previous build within that development ID and target release, and the
final release sorts above all its pre builds.

Preparation is repeatable. Later edits refresh the reasons and generated changelog entries rather than repeatedly
bumping releases. Newly affected packages join the proposal. Generated spec changes must not themselves cause an
endless sequence of new proposals.

An illustrative dependent changelog entry is:

```text
- Rebuild against xapi <new version-release>
```

Multiple dependency changes produce one proposed release bump with their reasons combined. The developer remains
responsible for describing their own source changes in the directly modified package's changelog.

### Final releases

The release bumps and changelog entries are already in the working copy for review and commit. Building without
`--pre` produces the final RPM releases from those specs. Development RPMs are separate artifacts; they are not
renamed or published as final RPMs.

CI validates the committed release decisions rather than making further spec edits. A fresh checkout must reach the
same decisions as the developer's checkout, independently of their local build caches.

### Lock updates

```sh
xcraft lock --update openssl
xcraft impacts
```

Updating the lock also prepares release bumps for the affected packages and their dependents, with changelog entries
listing the upstream dependency changes, for example:

```text
- Rebuild for updated dependencies:
  - openssl: <old version> -> <new version>
```

An updated package of the common build environment can affect every package. The lock-update bot proposes both the
lock changes and the generated spec changes in its pull request. No development suffixes are needed for this step.

Repeated updates refresh a pending proposal with the combined changes, without stacking daily release bumps.

### Acknowledging changes

After reviewing an ABI-compatible OpenSSL security update, a maintainer can run:

```sh
xcraft ack --dep openssl
```

This accepts the updated upstream version while removing that rebuild reason from the affected packages. When a
package has no remaining rebuild reasons, xcraft undoes its automatically prepared release bump and changelog entry,
and records reuse of its existing build. The reason for accepting reuse belongs in the revision message.

If another dependency still requires rebuilding the package, its bump remains and its generated changelog lists the
remaining reasons. Acknowledging OpenSSL must not discard unrelated rebuild decisions.

The same mechanism applies to our packages:

```sh
xcraft ack some-dependent
```

This accepts reuse of that dependent's existing build for its proposed dependency changes. It does not discard the
developer's own source edits or hand-written packaging changes. Acknowledgements cover specific changes; later
changes can require a new decision.

Acknowledgements must also prevent unnecessary transitive rebuilds. Retaining a package's existing build must not,
by itself, invalidate the builds of its dependents.

For the security-update case, publication uses the newly locked AlmaLinux RPMs and the retained builds of our
packages. Installed hosts upgrade the security-fixed dependency without needing new releases of our packages.

### Release proposals and build reuse

The committed state needs to distinguish proposed rebuilds from accepted reuse. Clearing `[pending]` alone cannot
express this: the updated dependency versions still change build keys.

The proposal is to keep the release baseline, rebuild reasons and reuse decisions in versioned state associated with
`xcraft.lock`. The precise schema is not decided. It needs enough information to:

- Identify the release being updated, consistently across developers and CI.
- Distinguish automatic release/changelog edits from pre-existing or subsequent manual edits, so `ack` can undo only
  the generated changes.
- Combine and selectively acknowledge several reasons affecting one package.
- Identify the retained builds on each architecture, allowing another checkout to fetch the same artifacts.
- Scope acknowledgements to the accepted changes, without masking later changes to sources or dependencies.

Retained artifacts keep their original build identity and provenance. Reuse must not pretend they were built with
the new dependencies. When a later change requires a new build, it uses the current locked build environment and
dependencies, including the security updates.

`xcraft impacts` becomes a view of proposed rebuilds and their reasons, including updates to our own packages. Build
availability and release decisions remain distinct: finding or completing a build does not acknowledge its reasons.

### Checks

Before publishing, validate against the release baseline that each changed build is covered by an appropriate new
version-release or an explicit decision to retain the existing build. There must be no silent replacement of an
already released RPM with different contents under the same version-release.

This also covers manually edited specs, patches and imported packaging changes. `--locked` continues to forbid
commands from changing the lock during CI builds; release validation is an additional check.

### Open questions for the earlier alternative

- How is the release baseline selected, stored and advanced? It must be an explicit, reproducible reference rather
  than whatever a local cache or a mutable publication stage happens to contain.
- Which commands prepare proposals automatically beyond the development path above? In particular, how should plain
  `build`, ordinary `repo`, `bump` and imported changes interact with preparation?
- Should affected dependents be included by default for development commands, avoiding the explicit `--dependents`?
- How should xcraft detect and handle manual edits overlapping a generated release bump or changelog entry?
- What happens when a retained build is unavailable locally and remotely? The behavior must not silently replace it
  with different contents under its existing version-release.
- How should proposals and acknowledgements be reconciled across branches, architectures and dependency cycles?
- How does the lock-update bot preserve reviewed decisions? Its current daily force-push recreates the proposal from
  the default branch and can overwrite manual changes on the bot branch.
- Should development IDs be remembered for a checkout, and how should counters work across checkouts or developers
  sharing a host? Existing counters only order builds within one checkout, ID and release.
- What role should `--test` retain alongside this workflow, which prepares the next release and uses `--pre`?
