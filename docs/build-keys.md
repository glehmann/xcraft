# Build keys

A build key identifies what a build of a package is made from. Two builds with the same key are made from the same
inputs, so one can stand for the other: a build of CI is fetched rather than built again, on any machine, and a cached
build is reused rather than built again. A build key that misses an input that changes what a build makes is a bug:
it would reuse a build made from other inputs. An input it covers needlessly only costs builds.

This document specifies them; `xcraft/buildkey.py` implements this specification, a function by section, and nothing
else computes them: `xcraft.buildid` numbers the builds by them, and `xcraft.cache` caches the builds by them.
`xcraft status --inputs <package>` shows the inputs of the key of a package, as hashed, see
[Checking a key](#checking-a-key).

## The build ID

The build ID of a package is in `.buildid/<package>`, committed: its build number and its build key, like
`2 3f1c…e2a0`, and with a reused build, the key it accepts, see [Reuses](#reuses). The build number is in the
releases of its builds, see [The cache key](#the-cache-key). How the numbers are given is in `xcraft/buildid.py`; this
document is about the keys.

## The build key

The build key of a package is the SHA-256, in hexadecimal, of:

```text
<inputs> \0 <package name>
```

where `<inputs>` is the JSON of an object with, for each package of its [component](#cycles), its name, and its
inputs, an array of:

1. **The package directory**, `base/<package>/`: its hash, see [The package directory](#the-package-directory).
2. **The recipe**: its hash, see [The recipe](#the-recipe), the same for all the packages.
3. **The lock**: an object with, for each architecture of `platforms` in `xcraft.toml`, its RPM architecture, like
   `x86_64_v2`, and an array of what the lock has for the package there, see [The lock](#the-lock).
4. **The dependencies**: the packages of ours its build is built against on any architecture, see
   [The dependencies](#the-dependencies), out of its component, sorted by name, each as an array of its name, its build
   number and its build key.

The JSON has its object keys sorted, and Python's default separators, `, ` and `: `, with non-ASCII characters
escaped: `json.dumps(inputs, sort_keys=True)`. See `buildkey.key()` and `buildkey.Inputs`.

The key does not depend on the architecture running xcraft: it covers what all of them have, so the build IDs, shared
by all the architectures in `.buildid/`, are the same computed on any of them.

## The package directory

The hash of a package directory is the SHA-256 of, for each file below it, but its `origin`, in the order of their
paths:

```text
<path relative to the directory> \0 <True|False: whether its owner may run it> \0 <the SHA-256 of its content, 32 bytes>
```

A symbolic link is the file it points to, which must be a file of the project: one out of it would make the key depend
on the machine, and a link to a directory, or to nothing, is refused. The empty directories are not in it, nor in git.
The spec, its patches, and its `sources` manifest, which has the SHA-256 of the archives, so the archives are covered
without being in git. See `buildkey.directories()`, with `xcraft.specs.hash_dir()`, which the cache of the parsed specs
uses too.

The build tree has exactly what the hash covers, from the same reads of the files, and nothing more, see
`rpmbuild.prepare_tree()`: the main spec in `SPECS/`, the other files in `SOURCES/`, but the `sources` manifest, whose
archives are there instead. The files have the mode 0644, or 0755 when their owner may run them, the archives 0444, the
directories 0755, and all of them the same time, `STAGED_TIME`, 1980-01-01. A build whose package directory does not
have the hash of its key when its tree is made fails, rather than being cached under a key of other files: the same
key, the same tree, whatever the checkout, its umask, its file times, and its empty directories.

## The recipe

The recipe is what makes the builds of all the packages, besides the upstream packages and the packages: the SHA-256
of the JSON, keys sorted, of an array of:

1. **The format**: `FORMAT`, the version of how xcraft gives the builds what their keys cover, besides their scripts,
   like the build trees: changed with it, to build them all again.
2. **The scripts of the builds**: `recipe_key()`, the SHA-256 of the concatenation of the template of the repository
   of our builds, `REPO_FILE`, and of `build.sh`, `offline.py`, `record.py`, `made.py` and `rpmdeps.py`, of
   `xcraft/buildenv/`. A change of them which makes the same packages, like a fix of how a build fails, maps its hash
   to the earliest one making the same packages, in `_SAME_BUILDS`, to keep the builds.
3. **The image**: the `Containerfile` of the build environment, as text.
4. `base_image` of `xcraft.toml`: the base image, pinned by digest.
5. The packages installed in the image: those of `packages` of `xcraft.toml`, and those xcraft needs, sorted.
6. `macros` of `xcraft.toml`: the RPM macros of the builds.
7. `release_suffixes` of `xcraft.toml`.
8. `extra_upstream` of `xcraft.toml`: the URLs of the extra upstream RPMs, and their SHA-256, which the builds check.
9. The repositories of `xcraft.toml`, all of them, whatever their architectures: their settings, as in `xcraft.toml`,
   but `source_baseurl`, where publishing gets their source packages from.

The image has the macros, the repositories and the packages in an order of its own, by name, which their order in
`xcraft.toml` does not change, like the recipe.

The versions of the upstream packages of the image are not in the recipe, but in [the lock](#the-lock). See
`buildkey.recipe()` and `buildkey.recipe_key()`.

## The lock

For each architecture, what `xcraft.lock` has for the package, an array of:

1. **The image**: the upstream binary packages of the image of the architecture, by name, each as its pin in the lock.
2. **The build root**: the upstream binary packages of its build root, but those of the image, by name, each as its
   pin in the lock.
3. **Its dynamic BuildRequires**, those its `%generate_buildrequires` asks for, as recorded by its builds.
4. **What rpm generated** for the binary packages of the packages it is built against, directly or not, on that
   architecture: by binary package, an array of the requirements they resolve to, and of their capabilities our specs
   require. They are what the build root is resolved from, with the BuildRequires of the spec, which the package
   directory covers.

The pin of a binary package is an array of its epoch-version-release, its architecture, the SHA-256 of its RPM, and
the name of its source package: the RPM the builds get, whatever the repositories have now. For `xcraft reuse --dep`,
the pins of the binary packages of the source packages it ignores the updates of are only their architectures and
their source packages: their names stay in the image and the build root, so an update adding or removing a binary
package is not reused.

The image and the build roots are resolved from the specs and the lock by dnf, for each architecture, on any
machine, and pinned in the lock, see `resolution.prepare()`: the key covers what the build environment installs,
without resolving anything. A pin stays until `xcraft lock --update` pins it again, even when the repositories publish
other contents for it: a build then fails, rather than getting another RPM. A resolution of another version or
architecture of a pinned package fails too, rather than pinning it. See `buildkey.lock_inputs()`, and the format of
the lock, `lock.FORMAT`, which `xcraft lock --upgrade` upgrades.

The image and the build roots get the pinned packages only: they install from a repository of the pinned RPMs of
their architecture, and nothing else upstream, see `xcraft.pinned`. Each RPM is downloaded once, into a store of the
machine the projects share, kept there only when it has the SHA-256 of its pin, and checked again before a repository
of it is used, by each command: one damaged in the store is downloaded again. A dynamic BuildRequires not
pinned yet is not installed: the build records it, it is pinned, and the package built again. The packages the base
image has already, in their pinned versions, are not installed again: they are those of its digest. What the builds
installed is checked against the pins too, by name, version and architecture, as the database of rpm has them, see
`checks.check_build()`. The image has its pins before installing them, so other pins build another image, rather than
taking a layer of the cache of podman.

## The dependencies

The packages of ours a package is built against are those whose binary packages its build root installs: those of
its BuildRequires, of its dynamic BuildRequires, and what they require, see `xcraft.graph.build_deps()`, and those
replacing upstream packages of the image, like `kernel-headers`, in all the builds but those of the packages they need.
The build key of a package covers their build IDs, with theirs, so a change of a package changes the keys of all the
packages built against it, directly or not.

The dependencies are those of all the architectures: a package built against another one on one architecture only,
with `%ifarch`, has it as a dependency on all of them, for the same key everywhere.

All the inputs of the keys are read once, from one read of the lock: the dependencies come from the specs parsed for
each architecture, with what that lock recorded, rather than from what the project computed before, maybe with
another lock, and nothing is read after, from the project or the files. A lock of another commit gives its own
dependencies and image, like for `xcraft reuse`. See `buildkey.Inputs`, with `buildkey.Platform`.

The specs are parsed in the build environment image of the machine running xcraft, see `xcraft.specs`: the keys of a
lock of another commit take them as parsed now, not as they were then, in its image. A spec whose meaning depends on
the image, like on its macros, may then have another one.

## Cycles

Packages depending on each other, like a compiler and its runtime, are the strongly connected components of the
graph of the dependencies. The packages of a component have the same `<inputs>`, those of all of them, and their keys
differ only by their names: they are built again together. The components are computed in the order of the
dependencies, each after those it depends on, see `xcraft.graph.components()`.

## Reuses

A build ID can accept another key: `<number> <key> accepts <other key>`, chosen with `xcraft reuse`. A package whose
key is the accepted one keeps the build ID, and its build: the build of the main branch, made from other inputs,
stands for the current ones, by a human decision recorded in the commit. The packages built against it have the build
ID, with the key of the build, in their inputs, so they keep their builds too, unless they changed otherwise.

`xcraft reuse --dep <upstream package>` selects the packages whose only changes are updates of that package: it
compares what the lock has for them, [as in their keys](#the-lock), with the versions of that package left out, in the
current lock and in the one of the main branch.

## The cache key

A build is cached, in `.xcraft/cache/builds/<arch>/<package>/<cache key>/`, and in the remote storage, in
`.xcraft/builds/<arch>/<package>/<cache key>/`, under its cache key: its build key, then its release suffix,
`%autorev`, which its spec puts at the start of `%dist`:

- `+b<number>`, for a final build, the build of the main branch, made by CI;
- `+b<number>~<counter>.<ID>`, for a development build, of a package whose build ID is not the one of the main branch
  where the current branch forks from it, see `xcraft.devbuilds`.

The cache key of a development build, or of a build against development builds, has another hash than its build key:
the SHA-256, in hexadecimal, of `<hash> \0 <package name>`, where `<hash>` is the SHA-256, in hexadecimal, of the JSON
of an array of:

1. for each package of its component, sorted by name, an array of its build key and its development suffix, like
   `~1.dev`, or an empty string;
2. the cache keys of the packages it is built against on any architecture, like for the build key, out of its
   component, sorted by name: the same on all the architectures.

A build is against development builds when a package it is built against does not have the cache key of its final
build, `<build key>+b<number>`, even with a final suffix, like a final build against a development build. A
development build depends on the development builds it is built against, which the build keys do not cover. A reused
build keeps its build key, as the build of the main branch. See `buildkey.development_key()`, and
`xcraft.cache.build_keys()`, which chooses between both.

## What the keys do not cover

On purpose:

- **The architecture**: the builds of the architectures have the same keys, and are cached by architecture.
- **Its own build number**: the key decides it, see `xcraft/buildid.py`.
- **What its build records**: what rpm generated for its own binary packages, which the keys of the packages built
  against it cover.
- **The compiler caches**: a build with or without one makes the same packages.
- **The remote storage, the signing keys, the publication settings**: they do not change the builds.
- **The version of xcraft**, but the scripts of the builds: a change of how xcraft computes what the builds get changes
  what the keys cover, which this document specifies.

Assumed, rather than covered:

- **The pinned RPMs stay available**: the repositories may remove them, which fails the builds then, rather than
  getting other ones.

What rpm generated for a build is recorded again when what it resolves to may change: the locked versions, the
requirements and the binary packages of our specs and their capabilities, the packages of the image, and the extra
upstream packages, see `checks.record_rpm_deps()`.

## Checking a key

`xcraft status --inputs <package>...` shows, as JSON, for each package, its key, its build ID, and the `<inputs>` of
its key, as hashed:

```text
xcraft status trivial --inputs
{
 "trivial": {
  "build ID": "1 3f1c…e2a0",
  "inputs": {"trivial": ["<directory>", "<recipe>", {"aarch64": [...], "x86_64_v2": [...]}, []]},
  "key": "3f1c…e2a0"
 }
}
```

Anyone can check a key against this specification, with nothing but the inputs shown:

```text
python3 -c 'import hashlib, json, sys; shown = json.load(sys.stdin)["trivial"]
print(hashlib.sha256(f"{json.dumps(shown["inputs"], sort_keys=True)}\0trivial".encode()).hexdigest())'
```

And two keys that differ, of two checkouts or commits, show which input does, compared.
