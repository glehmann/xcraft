# Design: automatic rebuild releases and development builds

Status: implemented: per-package build IDs, in `xcraft.builds`, and development builds by default. The earlier
spec-bump proposal is summed up at the end.

## Goal

Make release handling follow the rebuilds xcraft already computes, both for upstream lock updates and changes to our
packages. A developer should be able to edit, build, test on a host, and repeat. Each test build must upgrade the
previous one, but repeated development builds should lead to only one new official build per affected package.

Rebuilding is the default. A maintainer can explicitly retain existing builds when a dependency update does not
require rebuilding, so an ABI-compatible AlmaLinux security update can ship without rebuilding the distribution.

## Before

The build keys covered the package directory, the build environment, the upstream build dependencies and the keys of
our build dependencies, but the releases were the specs': `xcraft lock --update` recorded pending updates,
`xcraft bump` raised the releases of the packages given, without their dependents, `xcraft ack` cleared the pending
entries without keeping any build, and `--pre` and `--test` made test builds with counters of their own. A dependent
could be rebuilt with other contents under its published version-release, and acknowledging a libc update still left
almost every build invalidated by the changed build environment.

## The model: build IDs and default development builds

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

The locks record what the key needs, so it is computed from them alone, on any machine, see
[the locks](lock-design.md): `xcraft.lock` has the versions of the upstream source packages, their binary packages
pinned by SHA-256, in `[binaries]`, and those of the image of each architecture, in `[image]`, and of the build root
of each package, in `[roots]`; `xcraft.builds` has what the builds found that the specs do not say, like their
dynamic BuildRequires. The commands changing the locks, like the builds recording what they found, or
`xcraft lock --update`, resolve all the architectures, from their cached resolutions, so that they stay complete;
`--locked` fails when they would change.

The builds are stored, locally and in the remote storage, by architecture, under their key and release suffix:
`builds/<arch>/<pkg>/<key>+b42/`.

### Build IDs

A package's build ID is its build number with its build key. It is in `xcraft.builds`, tracked, with what the builds
of the package recorded, after a comment line with its name:

```toml
# xen
xen.build = 42
xen.key = "3f1c…e2a0"
xen.evr = "4.17.5-3"
```

The other inputs of the key are already in the repository, in the package directory, `xcraft.toml`, `xcraft.lock`,
and the other build IDs: the reason of a new number is in the same commit, as their diff.

The main branch is `main_branch` in `xcraft.toml`, `master` by default. Official builds are only made by CI on the
main branch, so the first-parent history of its `xcraft.builds` is the record of the official numbers: no server, nor
registry in the remote storage, is needed. A package keeps the build ID of the main branch, where the current branch
forks from it, while its key is the same; otherwise it gets the next number, or 1 when its epoch-version-release,
without `%{dist}`, is greater than the main branch's, which tells its builds apart already. A lower one fails: its
numbers may have been given before. So:

- a branch proposes the same number however many times it changes the package;
- reverting a change brings back the main branch's build ID, withdrawing the proposal;
- a branch's numbers do not change when the main branch moves on, only when it is merged or rebased;
- two unmerged branches can both propose 43: number and key on adjacent lines, both change them, so merging the
  second one conflicts, and recomputing the build IDs resolves it to 44, the merge forking from the main branch's
  tip even while it is in progress.

Out of a git repository, or without the main branch, there is nothing to number against: the numbers stay those of the
working tree, 1 for new packages.

The builds check the build IDs, like `--locked` checks the lock: in CI, every build checks that the committed ones are
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

`--nocheck` skips the tests of the specs, `%check`, in the development builds only: the final builds a command makes,
like those of dependencies the remote storage misses, run them, and the builds without `--nocheck` make those
development builds again, with their tests.

A test host gets the development builds with `xcraft repo --host <host> --upgrade`. It warns when the host does not
upgrade a package because it has a newer one installed. Bringing a host back to an earlier state, like after a
proposal is withdrawn, is the developer's responsibility, eased by restoring a btrfs snapshot.

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

`xcraft.builds` keeps the number and the key of the reused build, and adds the key it accepts:

```toml
xen.build = 42
xen.key = "9ab4…77d1"
xen.accepts = "3f1c…e2a0"
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

## Rejected alternative: automatic spec release bumps

An earlier proposal had xcraft edit the specs: bump the release of each changed package and of its dependents, with
generated changelog entries, recorded against a release baseline, and `xcraft ack` to undo the bumps of an accepted
change, like an ABI-compatible security update. It needed versioned state of proposals, reasons and
acknowledgements, telling the generated edits from the manual ones, and reconciling them across branches,
architectures and the lock-update bot. The build IDs above get the same releases from the build keys alone, without
editing the specs: a new number for each new key, and `xcraft reuse` for an accepted change.
