# Builds

The keys of the builds, their numbers, the reuse of builds, and the development builds.

## Build keys

Each build of a package has a **key**: a SHA-256 of everything that goes into it, the same on all the architectures.
xcraft never builds a package whose build with the same key and release suffix it already has, locally or in the
[remote storage](remote-storage.md), unless `--fetch=none`; it builds it again as soon as one of its inputs changes. A
build is named by its key and release suffix, `%{autorev}`: the number of its [build ID](#build-ids), `+b<n>`, and
its development build suffix, if any, in `.xcraft/cache/builds/<fingerprint>/<arch>/<pkg>/<key>+b<n>/`, and in the storage,
`.xcraft/builds/<fingerprint>/<arch>/<pkg>/<key>+b<n>/`, by the key signing it. A [development build](#development-builds), or one built against development builds,
is named by the hash of its key and of the names of the builds of ours it is built against instead, with its suffix.

**What it covers.** The key of a package is made of:

| Part | What it is | Changed by |
|---|---|---|
| The recipe | The Containerfile of the image, the build script, `xcraft/buildenv/build.sh`, the `.repo` file of the local repository, and the scripts recording what the builds install and isolating them from the network | A change to how xcraft builds, but one declared making the same packages |
| The build environment | The base image, pinned by digest, `packages` of `xcraft.toml`, the repositories, the RPM macros (`%dist`...), the `release_suffixes`, and the names and URLs of `extra_upstream` | Any change to these settings |
| The image | The upstream binary packages of the build environment image of all the architectures, by their pins in the lock: version, architecture and SHA-256 (see [The lock](dependencies.md#the-lock)) | An update of a package of the image, or other contents pinned for it |
| The package | Its name, and all the files of `<repo>/<pkg>/`, but `origin`: their paths, contents, and whether they are executable. The `sources` manifest covers the archives by their SHA-256 | Any change to the spec, a patch, a source file, or an archive |
| Its build root | The upstream binary packages of its build root besides the image's, on all the architectures, by their pins, and what it is resolved from besides its `BuildRequires`: its dynamic `BuildRequires`, and what rpm generated for the binary packages of ours it may have, as `xcraft.builds` records them | `xcraft lock --update` of one of them, a change of its `BuildRequires`, or what the builds found |
| Its dependencies | The build IDs, numbers and keys, of the packages of ours its build installs (see [Dependencies](dependencies.md)), like `kernel-headers`, in all the build roots | Any change to one of them, recursively |

The first three are common to all the packages: changing one of them rebuilds everything. Packages depending on each
other, a cycle, share the hash of them all, from which each one gets its own key.

**What it does not cover.** The architecture, which is the directory of the builds; the path of the checkout, the
machine, the user and the time; `origin`, and the files out of `<repo>/<pkg>/`, but those above; the upstream
packages of the source packages `xcraft reuse --dep` ignores the updates of, but their names and architectures. So the
same commit gives the same keys everywhere, on all the architectures: a build pushed by CI, or by a colleague, is
fetched by any checkout instead of being built again.
The local repository of the builds gets its own name in each checkout, `xcraft-<project id>`, for its own metadata in
the dnf cache the containers share, but the recipe the key covers has a placeholder instead.

**What a change rebuilds.**

- A spec or a patch: the package, and those built against it, directly or not (`xcraft graph --dependents <pkg>`);
- a version or a release bump: the same, as it changes the spec;
- an update of the lock: the packages with the updated package in their build root, and those built against them, or
  everything when the package is in the image, on all the architectures, even when only one has it;
- the configuration of the build environment, or a new version of xcraft changing the recipe: everything.

**Same builds.** A change to the recipe which makes the same packages, like a fix of how it fails, can keep the
builds: `_SAME_BUILDS` of `xcraft/buildkey.py` maps its hash to the one of the earliest recipe making the same packages,
which stays in the keys.
Not the packages, nor the image: what a package directory has goes in its source RPM, and what the image has can change
the builds. `test_recipe` fails on a change to the recipe in the keys, giving the new hash, to choose between both.

`xcraft build --dry-run` shows what would be built or fetched, with how many packages are cached, before building
anything. The builds only show the packages they build or fetch, with how many are cached.

**The build root is resolved.** The key is known before building, on every architecture: it covers the build root
dnf resolves from the `BuildRequires` and the lock, without building. When a build finds what changes the build roots,
like a new requirement rpm generated, `xcraft.builds` gets it, and the builds are cached under the keys computed with
it, which the next commands find. With `--locked`, for CI, such a build fails instead, as its key would not be the one
of the committed locks.

## Build IDs

Each package has a build ID, in `xcraft.builds`, to commit: a number and the key of its build, its `build` and `key`
lines, like `xen.build = 42` and `xen.key = "3f1c…e2a0"`. The number is in the releases of its builds, as `+b<n>` in `%{autorev}`, at the start of `%{dist}`:
`1+b42.xcpng8.99` for `Release: 1%{?dist}`. Every package must end its release with `%{?dist}`, or with
`%{?autorev}` for the package defining `%{dist}`. `xcraft build`, `repo` and `publish` update the build IDs, before
building, and after, with what the builds found.

A package keeps the build ID of the main branch, `main_branch` of `xcraft.toml`, `master` by default, where the
current branch forks from it, as long as its key is the same. Otherwise it gets the next number, or 1 when its
epoch-version-release, without `%{dist}`, is greater than there, like after `xcraft bump`: `xcraft.builds` records it,
as `xen.evr = "4.17.5-1"`, and its RPMs tell the builds of the new one apart already. A lower one than there fails, as
its builds could get the numbers of earlier ones, and the hosts would not upgrade to them: raise the epoch instead.
Without its `evr` line, which the build IDs given before it lack, the number is the next one: `xcraft build --ids`
adds those lines, to commit on the main branch. A branch keeps the
same number however many times it changes the package, whatever the main branch got since, and once another branch got
that number on the main branch, merging it conflicts, and computing the build IDs again gives the next one, even while
the merge is in progress. The packages built against it get new numbers too. The reason of a new number is in
the same commit: a change of the package, of the lock, or of another build ID; or a reuse, see
[Reusing builds](#reusing-builds). Out of a git repository, or without the
main branch, there is nothing to number against: the numbers stay those of the working tree, 1 for new packages. The
packages in a merge conflict of `xcraft.builds`, git's or jj's, get their lines written again; with git, `git add
xcraft.builds` then marks the conflict resolved.

The builds update the build IDs first; `xcraft build --ids` only updates them, without building, like after changing a
package or resolving a merge conflict, to commit them; `xcraft status` shows them without writing them.

With `--locked`, the builds fail instead of changing the build IDs; on the main branch, they also fail when a package
gets the number of the previous commit, its first parent, for another key, like when a merge conflict is resolved by
keeping one side: CI needs that commit, with `fetch-depth: 2` for `actions/checkout`. Official builds are only made by
CI on the main branch, so the history of its build IDs is the record of the official numbers.

## Reusing builds

Rebuilding with a new number is the default. A maintainer can instead reuse the build of the main branch, where the
current branch forks from it, despite changes, with the reason in the commit:

- `xcraft reuse <package>...` reuses the builds of the given packages, whatever changed but their own directories,
  like the packages of ours they are built against;
- `xcraft reuse --dep <upstream>...` reuses the builds of the packages whose only changes since the main branch are
  updates of the given upstream packages, binary or source names, like an ABI compatible security fix: their
  directories, `xcraft.toml` and the build IDs of our packages they are built against are those of the main branch, and
  so is what the lock has for them, but the versions of these packages.

The build ID then has the number and the key of the reused build, and the key it accepts, an equivalence chosen by
the maintainer, in its `accepts` line. It holds while the key is the accepted one: another change
gives another key, and the package a new number, as usual. The packages built against a reused one have its reused key
in theirs, so they keep their builds too, unless they changed otherwise. A reused build is never made again: it is the
one of the main branch, cached, or in the remote storage, on each architecture. Reusing builds needs the main branch,
in a git repository.

## Maintenance branches

A fix of a release, like a security fix under embargo, is made on a maintenance branch, of the patterns of
`maintenance_branches` of `xcraft.toml`, `["maint/*"]` by default, like `["maint/*", "embargo/*"]`: from the commit
of the release, while master went on. Its builds are
final ones, numbered `+b<n>.<m>`: the number of the release's build, `n`, and a counter of the builds of the branch,
`m`, from 1: `1+b42.1.xcpng8.99` upgrades the release's `1+b42`, and master's next build, `1+b43`, upgrades it. A
greater epoch-version-release gives `+b0.1`. The branches of a maintenance branch get development builds of these
numbers, `+b42.3~1.dev`.

`xcraft maint start [<release>]` makes the branch at the commit of the latest snapshot of the stable stage of the
release, `--stage` for another one, or at that of `--at`: `maint/<release>`, of the first pattern, or the name of
`--name`, like `embargo/xsa-123`, of one of them. The builds are numbered against it on it, git's current
branch, or with jj, when it is the nearest bookmark of `@` and its ancestors, like while editing the revision of
`maint/xsa-123`, or after `jj new maint/9.0`; and on its branches, whose fork point with it is more recent than with
master, or is its tip, while master went on since the release. Otherwise, like while master is still at the release,
`xcraft --maintenance maint/9.0`, or `$XCRAFT_MAINTENANCE`, numbers the builds against the branch it names, the main
one or a maintenance one: CI gives it the target of a pull request, or the branch pushed.
A commit is a maintenance branch's when it is on its
first-parent chain, but not on master's: so the maintenance branches must be merged into master with merge commits,
not fast-forwarded nor squashed, which `--locked` checks on master. Master then keeps their build IDs when the key is
the same, and its number greater, as when master changed none of the inputs of the package since the release: the
users get the same build at the next release of master. The release tags, `release/...`, keep the commits of a
maintenance branch removed since.

The maintenance branches of `embargo_branches`, like `embargo/*`, and their branches, are those of fixes under
embargo: their builds go to the private remote storage only, see [Remote storage](remote-storage.md), and
[the design](embargo-design.md), for their release.

## Development builds

While working on a package, a developer installs several builds of it on the same test host. Each one must upgrade
the previous one, and stay below the next real release. So the builds of the packages whose build ID is not the main
branch's, those changed, and those built against them, are development builds, by default: `+b<n>~<c>.dev`, below
the final build, `+b<n>`. The other packages get their final builds, fetched from the remote storage. `--dev ID`, or
`$XCRAFT_DEV`, gives another identifier than `dev`.

`<c>` is a counter of the package and number, going up when the inputs of the build or the identifier change, so that
each development build upgrades the previous ones, whatever their identifiers, while a build of the same inputs is
cached: `1+b42~1.dev`, `1+b42~2.dev`, `1+b42~3.qcow2`, then `1+b42`. The counters are in `.xcraft/dev-builds.json`,
which `xcraft clean` keeps, so they are specific to the checkout. `$XCRAFT_DEV_COUNTER` gives the counter of all the
development builds instead, like the run number of a CI workflow, which only grows, as CI keeps nothing between its
runs. `--final` makes final builds, overriding
`$XCRAFT_DEV`, but not with `--dev`. On the main branch, all the builds are final ones. The development builds are
cached apart from the final builds, never pushed to the remote storage, nor taken from it; `xcraft status` shows them,
`clean` keeps those last built, `shell` installs them, and `repo` makes a repository of them for the hosts, see below.
`build --verbose` prints the paths of their RPMs. Their binary packages are compressed with zstd at level 3, rather
than at the level 19 of the final builds: their packaging takes a third of the time, for packages a third bigger,
which stay on the machine and its test hosts.

`xcraft repo <pkg>...` puts the development builds of the given packages alone in a repository, `repo-ID/` by default,
with a `xcraft-ID.repo` file for the hosts, which keep their usual repositories for everything else. Their
dependencies are checked with our other current builds. `--dependents` adds the packages built against them.

With `--host`, as given to ssh, or as `ssh://[user@]host[:port]` for another port, and repeatable, xcraft copies the
repository to each host with rsync, through one SSH connection per host, which asks for its password once, if any, in
`/var/local/xcraft/ID/`, and installs its `.repo` file, and with `--upgrade`, it also upgrades the packages the host
has installed, and installs those obsoleting an installed package, replacing it, like `openvswitch3.7` replacing
`openvswitch`, warning about those it keeps in other builds than the repository's, like newer ones of another
identifier, and removes the `.repo` file then, for the host to keep its usual repositories only:

```
xcraft repo xen --dependents --host root@testhost --upgrade
```

For a host, the repository also has the main branch's builds of the given packages without development builds, with
a warning, to install them on a host which has other builds of them, like older ones.

`--vm` takes the VM `xcraft boot` installed for the project, running, like after `xcraft vm start`, as a host: root on
its SSH port of localhost, without checking its host key, which each installation changes. `ssh-copy-id` with that port
spares typing its password each time.

Without `--host`, `--baseurl` gives where the hosts find the repository, for instance over http; by default, it is its
directory, to copy at the same place on the hosts. Bringing a host back to an earlier state, like after a change is
dropped, is up to the developer, like restoring a btrfs snapshot.

## Builds without their tests

`--nocheck`, for `xcraft build`, `repo`, `iso` and `boot`, builds the packages without running the tests of their
specs: rpmbuild skips `%check`, with the same build roots, for faster iterations. Those builds are cached with a
`nocheck` mark: the builds without `--nocheck` make them again, with their tests, `xcraft push` never pushes them, and
the ISOs made with them are cached apart. The packages fetched from the remote storage keep their builds, made with
their tests. It cannot be used with `--final`.
