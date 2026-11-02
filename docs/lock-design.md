# Design: the locks of the dependencies and of the builds

Status: implemented, the lock of the dependencies in format 4, and [the lock of the
builds](#the-lock-of-the-builds-xcraftbuilds), with the build IDs and what the builds record.

## What the locks are for

The builds of our packages, the image they are built in, the published repositories and the ISOs depend on upstream
packages: AlmaLinux's and EPEL's. Their repositories change every day. **The lock of the dependencies**,
`xcraft.lock`, decides which upstream packages they get, so that:

- a build gets the same upstream packages on any machine, today or in a year: what the build keys cover, see
  build-keys.md;
- an update is a commit, reviewed like any other, and its impact known before building: the packages whose build
  roots change, so whose keys and build IDs change;
- every architecture gets the same versions, from one lock, computed on any machine.

It is the only source of the upstream versions: the repositories are pools to take the locked packages from, see
upstream-mirror-design.md for our copy of them.

**The lock of the builds** says which build of each package of ours a commit has, its build ID, and what its builds
found that its spec does not say, which the resolution needs before building: one file, `xcraft.builds`. Up to lock
format 3, the build IDs were in `.buildid/`, and what the builds found in `xcraft.lock`, see [Problems](#problems).

# The lock of the dependencies: `xcraft.lock`

## What it has

One file, for all the architectures, written by xcraft only, one line by entry for readable diffs and merges:

| Table | What | Written by |
|---|---|---|
| `[sources]` | the version-release of each upstream source package used, the same on all the architectures | `prepare()`, for those needed and not locked yet; `xcraft lock --update` |
| `[binaries.<arch>]` | the pin of each upstream binary package used on `<arch>`: its EVR, architecture, the SHA-256 of its RPM, and its source package | `prepare()`, for those not pinned yet; `xcraft lock --update`, again |
| `[image]` | by architecture, the upstream binary packages of the build environment image | `prepare()` |
| `[roots]`, `[roots.<arch>]` | by package of ours, the upstream binary packages of its build root, but the image's | `prepare()` |
| `[runtime]` | by architecture, the upstream binary packages of the published repositories | `prepare()` |
| `[iso]` | by architecture, those of the tools making the ISO, but the image's | `prepare()` |

and `version`, the format, `lock.FORMAT`.

They say what we take from upstream. What the builds of our packages found, which the specs do not say, is in the lock
of the builds, see [What the builds record](#what-the-builds-record).

### Versions: `[sources]`

One version by upstream source package: on each architecture, the resolution only sees the binary packages whose
source package has that version, from the metadata of the repositories, but the suffixes of `release_suffixes`, like
AlmaLinux's `.alma_altarch` rebuilds for x86_64_v2, see `resolve.canonical()`. So nothing is to align between the
architectures.

A source package needed and not locked yet, like for a new BuildRequires, is locked in the latest version all the
architectures have, or else the latest one of the first having it, see `lock.choose_versions()`.

### Pins: `[binaries.<arch>]`

Each upstream binary package used on an architecture, by name: `<evr> <arch> <sha256> <source>`, from the metadata of
its repository when it was pinned. A pin stays until `xcraft lock --update` pins its source package again, even when
the repositories publish other contents of the same version: the builds get the RPM of the pin, or fail. A resolution
giving another version or architecture of a pinned package fails too, rather than pinning it.

The pinned RPMs are downloaded once by machine, into a store the projects share, checked against their SHA-256 there
and before use, and installed from a repository of the pins only, see `xcraft.pinned`. Our copy of the upstream
packages keeps those the repositories drop, see `xcraft.upstream_copy`.

A table has one pin by name: another architecture of the same name, like a multilib i686 package, needs another
format.

### What is pinned: `[image]`, `[roots]`, `[runtime]`, `[iso]`

What is installed, by name, each pinned:

- `[image]`: the packages of `packages` of `xcraft.toml` and those xcraft needs, over the base image, without weak
  dependencies, like the Containerfile installs them;
- `[roots]`: for each package of ours, what `dnf builddep` installs for its BuildRequires and dynamic BuildRequires,
  but the image's and ours, with what our packages it gets require;
- `[runtime]`: what the published repositories need for our packages, the extra ones of `[publish]`, and those of the
  installer of `[iso]`, each resolved alone, the installer's together;
- `[iso]`: the tools making the ISO, for x86_64 only: the ISO boots with isolinux, on BIOS, and GRUB's x86-64 CD
  image, on UEFI, and has memtest86+, of x86 only. An arm64 ISO would boot GRUB's arm64 one, on UEFI only, with
  xorriso, mtools and dosfstools pinned for aarch64. What the installer installs is in `[runtime]`, on every
  architecture.

They are resolved by dnf, without installing anything, natively for every architecture, against the upstream
repositories in the locked versions, our copy of them, and a repository of the metadata of our binary packages,
written from their specs and what their builds recorded, see `xcraft.resolution` and `buildenv/resolve.py`. The
results are cached by what they are resolved from.

### What the builds record

In the lock of the builds, see [`xcraft.builds`](#the-lock-of-the-builds-xcraftbuilds). The resolution of the build roots, before building, needs what only builds know:

- the packages the requirements rpm generates resolve to, like `lzo` for `xen-dom0-libs`, which links against it: a
  package built against `xen-dom0-libs` gets `lzo`. By name, not by capability: many have hashes, like `ocaml(Stdlib)
  = <hash>`, which change on rebuilds;
- the capabilities rpm generates that our specs require, like `pkgconfig(xenctrl)`: the metadata of ours, from the
  specs, does not have them;
- the dynamic BuildRequires: rpm only knows them by running the script of the spec, in a build.

The builds record them, from the headers of what they made and the source packages `rpmbuild -br` made, without the
release suffixes of the builds, so they do not depend on build numbers, nor the provides the specs declare. A build
made elsewhere and fetched records them too, before it is used. The shared entries are those of the first architecture
of the configuration, and of the others while it has none; an entry of `<arch>` replaces the shared one there, when it
differs, see `lock.Recorded`.

What a build records can change its build root: it is then not cached, and built again after what it needs now.

## How it is written

- **`prepare()`**, which the commands run first: it locks the source packages needed and not locked yet, resolves the
  images, the build roots, what is published and the ISO tools of all the architectures, records them, pins the
  packages not pinned yet, and drops the entries of packages and binary packages gone.
- **The builds** record what they found, in the lock of the builds, then `prepare()` again for what it needs.
- **`xcraft lock --update [packages]`** locks the latest versions all the architectures have, and pins their binary
  packages again, even of the same versions; **`--outdated`**, for CI, says whether it would change versions;
  **`--upgrade`** upgrades a lock of an older format, dropping what it has without pins, which `prepare()` resolves
  and pins again, and moving what the builds recorded in it, and `.buildid/`, to the lock of the builds.
- **`--locked`**, for CI, forbids any change: of the lock, and of the build IDs, see buildid.py.

The lock is changed from the lock as read, and saved only if it did not change meanwhile, by another command, under
the lock of the project: changed again from the new one otherwise, see `resolution._saved()`.

## How it is read

- **The resolution**, for the versions, with the records of the lock of the builds.
- **The image** has its pins, `pins.list`, and a dnf versionlock of them, and installs from the repository of the
  pins: other pins make another image.
- **The builds** install from the repository of the pins of their image and build root, and check what they installed
  against the pins, by name, version and architecture, see `checks.check_build()`.
- **The build keys** cover, for each architecture, the pins of the image and of the build root, and from the lock of
  the builds, the dynamic BuildRequires and the records of the binary packages of ours it is built against, see
  `buildkey.lock_inputs()`. So
  an update changes the keys of the packages it reaches, and their build IDs, see buildid.py; `xcraft reuse --dep`
  keeps their builds instead, for an update like an ABI compatible fix.
- **Publishing and the ISOs** take `[runtime]` and `[iso]`, pinned, and check them installable.
- **Our copy of the upstream packages** copies the pins, and their source packages.

# Up to lock format 3

## Build IDs: `.buildid/`

A file by package of ours, `.buildid/<package>`, one line: `<number> <key>`, or `<number> <key> accepts <key>` for a
build reused despite changes, see `xcraft reuse`. The key covers what makes the package, on all the architectures, see
build-keys.md; the number goes in its release, `+b<number>`, see buildid.py. The rules are the same since:

- a package keeps the build ID of the main branch where the current branch forks from it while its key is the same,
  and gets the next number of the fork's otherwise: the numbers the main branch gave since do not count;
- on the main branch, a changed key needs a greater number than the previous commit's, so that a number is never given
  to two builds, even by a merge conflict resolved by keeping one side;
- two branches changing the same package both take the next number, so their files conflict on merge: on purpose,
  the merge is built again, with a new number;
- `--locked`, in CI, forbids any change.

## Problems

### What our builds record is in the lock of the dependencies

`[requires]`, `[provides]` and `[buildrequires]` are about our packages, not upstream: they change when one of ours
changes. In xcp-ng-dist, `[provides]` has 105 entries, and 103 of them have the version of a package of ours:

- 97 its own name, `host-installer = 11.0.26-0.ydi.26.xcpng8.99`, which rpm gives every binary package: needed as two
  subpackages of host-installer require it;
- 5 its own name for its architecture, `libempserver(x86-64) = 1.1.1-2.xcpng8.99`;
- 1 a versioned provide of another name, `xapi-storage = 26.4.0-1.3.xcpng8.99`, of `python3-xapi-storage`;

and 2 are files, `/usr/sbin/ssmtp` and `/usr/sbin/oxenstored`. So:

- bumping a package of ours changes `xcraft.lock`, which its build records, and CI refuses until it is committed;
- two branches changing different packages of ours change the same lines of it, and may conflict;
- a diff of it mixes the choices of upstream packages, to review closely, with bookkeeping of our builds.

### The self-provides are recorded

The 102 provides of a package's own name, and of its name for its architecture, are what rpm always generates for a
binary package, from its spec: the specs as parsed have them already, like `xapi-storage = %{version}-%{release}`
that `python3-xapi-storage` declares. They were recorded as the recording compared the provides of the builds, with
their release suffixes, to those the specs declare, without.

### The build IDs are many files

`.buildid/` has a file by package, 44 in xcp-ng-dist: an upstream update, or a change of a package many others are
built against, changes dozens of them, which fill `jj status` and the diffs.

# The lock of the builds: `xcraft.builds`

`xcraft.lock` has what we take from upstream only, and `xcraft.builds`, next to it, what concerns our builds: the
build ID of each package of ours, and what its builds found, which change in the same commits.

## Format

```toml
# written by xcraft: the builds of our packages, and what they found that the specs do not say

# blktap
blktap.build = 1
blktap.key = "40a65458edaa2088a8c630126382621a91116754e4d3648931cca4e1738975a1"
blktap.requires."blktap" = ["libaio", "openssl-libs"]

# xen
xen.build = 2
xen.key = "2aff59e6ebed1aaeac4c6b79d10a107cac486fdfa2e6f0eedb52e6f3b2707827"
xen.requires."xen-dom0-libs" = ["e2fsprogs-libs", "lzo", "xen-libs"]
xen.provides."xen-devel" = ["pkgconfig(xenctrl) = %{version}"]
xen.aarch64.requires."xen-dom0-libs" = ["e2fsprogs-libs", "xen-libs"]

```

TOML, which reads it as a table by package. The layout is for merges and history, with plain line-based merges, as
jj does not use git's merge drivers:

- **each line starts with the name of its package**, as dotted keys rather than tables, so that a change says which
  package it is of, and no two lines are the same, which could make a diff pair the wrong lines;
- **a comment line with the name of the package before its lines**: a line no change of another package touches, so
  that their changes are never next to each other, which conflicts;
- **a blank line after the lines of each package**, the last one included.

`build` and `key` are the build ID, its number and its key, and `accepts`, only for a build reused, the key it is
accepted for, as `.buildid/` had them on one line; `buildrequires`, `requires` and `provides` are what the builds
recorded, as the lock had them, and `<arch>.` the entries of an architecture that differ. The lock of the
dependencies keeps `[sources]`, `[binaries.<arch>]`, `[image]`, `[roots]`, `[runtime]` and `[iso]`:
`[roots]` is by package of ours, but it is what we pin from upstream for it, which an upstream update changes for many
packages at once, and its review goes with the pins.

## Merges

Tested on a file of five packages, with git 2.54, `git merge` and `git rebase`, and jj 0.44, a merge, `jj new a b`,
and `jj rebase`, which all gave the same results:

| Changes of the two branches | Merge |
|---|---|
| two neighbour packages bumped | clean |
| the last line of a package, and the first one of the next | clean |
| a line added at the end of a package, the next one bumped | clean |
| a package added just before a package bumped, or just after one changed | clean |
| a package removed, the one before or after it bumped | clean |
| the first package bumped and one added at the start; the last one bumped and one added at the end | clean |
| two packages added in different gaps | clean |
| the same package bumped on both sides, to the same number or not, or bumped on one and reused on the other | conflict, as wanted: the build ID |
| the same package, its build ID on one side, its records on the other | clean, see below |
| an upstream update bumping all the packages, and a change of one of them | conflict on that one, built again anyway |
| two packages added in the same gap | conflict |

Without the comment lines, a package added or removed next to one bumped conflicted, and so did the last package bumped
with one added at the end without the last blank line; two blank lines instead of the comment did not help either.

A package's records changing on a branch where its build ID does not is not a case in practice: what a build records
changes with what it builds, so with its key. If it happens anyway, the merge mixes a build ID with records of another
build, which the keys of the packages built against it cover: `--locked` refuses it, until built again.

The one loss, against a file by package: two branches adding packages that sort into the same gap conflict, which is
resolved keeping both. Otherwise, xcraft reads a file in conflict without the lines of the packages the conflicts touch:
building gives their build IDs again and records what their builds find, like it wrote the files of `.buildid/` in
conflict again; `--locked`, in CI, refuses a merge not built again.

## History

A change of a package names it on each line, so its history is that of its directory and of its lines:

```
jj log -r "files('base/xen') | diff_lines(regex:'^xen\.', 'xcraft.builds')"
```

as tested, with `diff_lines()`, which replaces `diff_contains()` in jj 0.44. git finds the changes of the lines with
`git log -G '^xen\.' -- xcraft.builds`, but cannot list them with those of the directory in one command: `-G` filters
all the commits. `xcraft history xen` lists them both, with git, like the rest of xcraft, for those who do not use jj,
and since jj's revset aliases cannot build those patterns from a name.

## The versions of ours, out of the records

- The provides the specs declare are not recorded, the self-provides included: the specs as parsed have
  `<name> = <evr>`, and `<name>(<isa>) = <evr>` for those of an architecture, like rpm makes them, and the recording
  compares them without the release suffixes of the builds, see `builds.recordable()`.
- A recorded capability with the EVR of its binary package, or its version, has it as `%{evr}`, or `%{version}`, put
  back when reading, see `builds.locked_packages()`: like `pkgconfig(xenctrl) = %{version}`.

So a bump of a package of ours changes its spec and its `build` and `key` lines, as its key changes, but nothing else:
what it recorded only changes when what it generates changes, like a new library linked, or when one of our specs starts
requiring something it provides.

## Writing and checking

- The builds write their package's lines, and `update()` of buildid.py the `build`, `key` and `accepts` lines, from the
  file as read, under the lock of the project; `prepare()` drops the packages and binary packages gone.
- A merge conflict of the file, git's or jj's, drops the lines of the packages it touches when read: `update()` gives
  their build IDs again, and their builds record again.
- `--locked` forbids any change of either lock.
- `xcraft lock` shows both.

## Keys and build IDs

The keys cover the same inputs, by architecture, but the records as `xcraft.builds` has them: without the provides
the specs declare, and with the placeholders, which the build IDs of the packages built against cover. So the keys of
the packages built against those with such records change once: the move goes with build IDs numbered from 1 again,
on a main branch rewritten without `.buildid/`, and all the packages built again. A bump still changes the keys of the
packages built against the one bumped, through its build ID.

## Migration

Lock format 4: `xcraft lock --upgrade` moves `.buildid/` and the three tables to `xcraft.builds`, without the
provides the specs declare and with the versions as `%{evr}` or `%{version}`, removes `.buildid/`, and keeps the rest
of the lock as it is: no pin changes. The main branch where the current one forks from it is read in `xcraft.builds`
only: without it, there, the build IDs are numbered from 1; once the main branch has it, a branch forking before it
fails, to be rebased.

## Workflows, after

- **A bump of a package of ours:** its spec, and its `build` and `key` lines.
- **A new BuildRequires, of an upstream package not locked:** its spec, `xcraft.lock`, which locks and pins it, from
  `prepare()`, and its `build` and `key` lines.
- **A package of ours linking a new library:** its build records it in its lines; the packages built against it get the
  library in their build roots, in `[roots]`, and new build IDs.
- **An AlmaLinux update:** `xcraft.lock`, `[sources]`, `[binaries]` and `[roots]`, and the `build` and `key` lines of
  the packages it reaches.
- **A new architecture:** its pins in `xcraft.lock`, and its own differences, if any, in `xcraft.builds`, by its first
  builds.

# Alternatives considered

- **A file by package, `.buildid/<package>` and `.recorded/<package>.toml`:** two packages added in the same gap
  merge, but the files of the build IDs stay many, and those of the records double them; the history of a package is
  that of its files, without a regex. Its records in its `.buildid/` file would conflict with its build ID, which is
  the same with one file as the changes of a package's records come with a new build ID.
- **Tables by package, `[xen]` then `build = …`:** merges like the format chosen, but the lines of a change do not name
  their package, so its history needs the whole diff.
- **The records in the package directories**, like `base/xen/recorded.toml`: the hash of the directory, in the key,
  would cover what the build records, so a build would change its own key.
- **A git merge driver** resolving the conflicts of the lock of the builds: jj does not use them.
- **Only the versions out of the records, kept in `xcraft.lock`:** it would not change on bumps, but would still mix
  our bookkeeping with the upstream choices, and conflict between branches changing different packages of ours.
- **No records, building first:** the keys would only be known after building on each architecture, which the single
  lock of the dependencies was made to avoid.

# Open questions

- **Placeholders beyond `%{evr}` and `%{version}`:** a provide whose version follows another source, like a bundled
  library, stays as recorded, and changes the lock of the builds when it changes, as it should.
- **Determinism:** the resolution must give the same result everywhere. The locked versions fix the candidates, but dnf
  chooses between providers of the same capability. If one is ambiguous, `xcraft.lock` records the choice.
- **Errors of the resolution:** some dnf errors have no message, like the one of an image whose pinned packages are in
  no repository anymore: they should name the packages, and a failed resolution should not be cached.
