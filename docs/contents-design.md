# Design: the contents of a snapshot, `xcraft contents`

Status: implemented, `xcraft contents`, with `--set`, `--json`, `--why`, `--diff`, `--diff-stage` and `--markdown`, and
the `contents.json` of the published snapshots; to do: the diff in the comment of a pull request, in `pull-request.yml` of
xcp-ng-dist. `why` costs nothing measurable: the resolution of xcp-ng-dist takes 12 s with or without it, and its cache
of an architecture grows from 575 KiB to 890 KiB.

## The need

`xcraft graph` shows what our packages are built against. Nothing shows what a commit publishes: which packages the
repositories of its snapshot have, which of them its ISO has, why an upstream package is there, and what a change does
to them, like a pull request adding a dependency to a spec, or `xcraft lock --update`. The resolution knows it already,
before building anything: it resolves what the published repositories need, to pin it in the lock. It only keeps the
unions, `[runtime]`, `[image]` and `[roots]` of `xcraft.lock`, and not why each package is there.

`xcraft contents` shows them, from the lock and the specs, without building, like `graph`:

```
xcraft contents                              # the packages of the snapshot of the checkout, by set
xcraft contents --set iso                    # the packages of one set
xcraft contents --why python3-dnf            # why a package is in each set it is in
xcraft contents --diff master                # what the checkout changes, compared with another revision
xcraft contents --diff-stage main@incoming   # or with a published snapshot, the latest one of the stage
```

## The sets

A snapshot has these sets of packages, by architecture, named after where they are:

| Set | What | Resolved from |
|---|---|---|
| `base` | the repository of the snapshot, `base/<arch>/`: what the hosts install from | all our binary packages, the `extra` upstream packages of `[publish]`, and what each needs, alone, on an empty system; and the packages of `iso` and `install.img` |
| `base/Source` | its source packages | the source packages of those of `base` |
| `build` | the build repository of the snapshot, with `[publish] build`, `build/<arch>/`: what the builds of our packages need, besides `base` | the packages of the build environment image and of the build roots of our packages, but ours and those of `base` |
| `build/Source` | its source packages | the source packages of those of `build` |
| `iso` | the repository of the full ISO, `Packages/`: what the installer installs from | `[iso] packages`, together, and what they need |
| `install.img` | the root filesystem of the installer, of both ISOs | `[iso] installer`, together, and what they need |

All without weak dependencies, like the snapshot and the ISO are made. `iso` and `install.img` are in `base`, neither
in the other. The netinstall ISO has `install.img`, and no repository. Not shown: the tools making the ISO, which are
not in it.

What the host installer installs on a host is not a set: it depends on its answers, and on the scripts of the packages.

## What the resolution records

`resolve.py` resolves the groups of the sets already: in `Resolver.runtime()`, each root of `base` alone, then the
packages of `install.img` together, and those of the ISO together; the image and each build root, for `build`. It
records, besides the unions:

- `sets`: the names of the binary packages of `iso` and `install.img`, ours and upstream, the install sets of their
  groups: `base` and `build` are known already, from `[runtime]`, `[image]` and `[roots]` of the lock, and the source
  sets from the pins of the binary packages, which have their source package;
- `why`: by set of binary packages, for each of its packages but the roots, the package bringing it in, and the
  requirement: one shortest path from the roots, by a breadth-first walk of the requirements of the packages of each
  group, to their providers in the group, in the order of the requirements. In `build`, the roots are the image, and
  the build requirements of each of our packages. A package required by several gets the first one found: `--why`
  shows one path, the one dnf would show first, not all of them.

They are in the cached resolution, `.xcraft/cache/resolved/`, and in the remote storage, like the rest of it, not in
the lock: they follow from it, and its diffs stay those of the versions. A resolution made before has neither: the
command resolves again, as for a change of `resolve.py`, which is in the key of the cache.

The versions are those of the lock, `[binaries.<arch>]`, for the upstream packages, and for ours, their
epoch-version-release and build number, without the development build suffix: what the snapshot would have once the
commit is on the main branch, so that a pull request does not show all its packages as changed by their suffixes.

## Showing the sets

By default, the packages of all the sets, grouped by source package, ours first, then the upstream ones, with the sets
each package is in: the line of a source package marks the source sets it is in, under `base` for `base/Source` and
under `build` for `build/Source`, and the lines of its binary packages the binary sets:

```
package                   version              base  build  iso  install.img
xcp-ng-deps (source)      8.99-1+b1.xcpng8.99  ✓
  xcp-ng-deps             8.99-1+b1.xcpng8.99  ✓            ✓
dnf-plugins-core (source) 4.7.0-10.el10        ✓
  dnf-plugins-core        4.7.0-10.el10        ✓            ✓    ✓
  python3-dnf-plugins-core 4.7.0-10.el10       ✓            ✓    ✓
```

then how many packages each set has, ours and upstream. `--set <set>`, like `--set iso` or `--set base/Source`, shows
one set only, as a list, one NEVRA by line, for scripts; `--json` all of them, with the sources and versions. `--arch`
like the other commands.

`--why <package>` shows, for each set the package is in, its path from a root, with the requirements:

```
base, iso: python3-dnf-plugins-core, required by dnf-plugins-core (python3-dnf-plugins-core = 4.7.0-10.el10), required
by xcp-ng-deps (dnf-plugins-core), ours
build: python3-dnf-plugins-core, in the build environment image
```

## Comparing: `--diff`

`--diff <ref>` compares the sets of the checkout with those of a git revision, like `master`, or `HEAD^1` in the CI
of a pull request, whose checkout is the merge commit: by set, the packages added, removed, and those whose version
changed, grouped by source package, ours first, with a count of each:

```
base: 2 added, 1 updated, 0 removed
  + libfoo 1.2-3.el10 (source foo), required by with-deps
  ~ xcp-ng-config 0-0.ydi.6+b1 → 0-0.ydi.7+b1
iso: 1 added
  + libfoo 1.2-3.el10 (source foo), required by with-deps
```

The sets of the revision are those of its lock and specs, read with `git show`, like the build IDs of the main branch
(`buildid._at()`), resolved with the cache of the checkout, or from the remote storage, where CI pushes the resolutions
of the main branch: comparing with it resolves nothing, but for a revision nobody resolved. A package added shows why,
from `why`, as the reviewer's first question. Only the checkout is compared: two other revisions are compared by
checking out one of them.

`--diff-stage <remote>@<stage>[/<snapshot>]` compares with a published snapshot: of the remote storage named like in
`[remote.<name>]` of `xcraft.toml`, `main` or `dev`, and the latest snapshot of the stage by default, like
`main@incoming`, `main@incoming/latest`, or `main@incoming/2026-11-08.2`: what a release would change, compared with
the last one. A snapshot has its sets, as published: `contents.json`, which `xcraft publish` writes next to its
manifest, signed with it, from the resolution, with the versions of its packages, those of its builds: the development
ones of a stage of dev mode. Its manifest must be signed by a key the storage trusts, `build_keys` of its
`[remote.<name>]`, or the key of the command, or the developer's. A snapshot published before has no such file: the
command says so, as the stages are published again with each change.

`--markdown` writes the diff as Markdown, for the comment of a pull request: `pull-request.yml` adds it to the summary
of `xcraft publish --summary`, comparing with `HEAD^1`, so that the reviewers see what a pull request changes in what
the hosts get, not only its development builds.

The debuginfo and debugsource packages of `base`, published with the others, are not shown, nor counted, nor compared:
they follow their packages, and would double the lists. An option can show them later, if needed.
