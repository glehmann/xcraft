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

The hash of a package directory is the SHA-256 of, for each file below it, symbolic links followed, but its `origin`,
in the order of their paths:

```text
<path relative to the directory> \0 <True|False: whether its owner may run it> \0 <the SHA-256 of its content, 32 bytes>
```

That is what the builds get, like git: the rest of the file modes depends on the umask, and who may run a file on the
mounts, with the ACLs. A symbolic link brings the files it points to, wherever they are, like in the build tree: those
pointing out of the project make the key depend on the machine, which xcraft import refuses. The empty directories are
not in it, nor in git. The spec, its patches, and its `sources` manifest, which has the SHA-256 of the archives, so
the archives are covered without being in git. See `buildkey.directories()`, with `xcraft.specs.hash_dir()`, which
the cache of the parsed specs uses too.

## The recipe

The recipe is what makes the builds of all the packages, besides the upstream packages and the packages: the SHA-256
of the JSON, keys sorted, of an array of:

1. **The scripts of the builds**: `recipe_key()`, the SHA-256 of the concatenation of the template of the repository
   of our builds, `REPO_FILE`, and of `build.sh`, `offline.py`, `record.py`, `made.py` and `rpmdeps.py`, of
   `xcraft/buildenv/`. A change of them which makes the same packages, like a fix of how a build fails, maps its hash
   to the earliest one making the same packages, in `_SAME_BUILDS`, to keep the builds.
2. **The image**: the `Containerfile` of the build environment, as text.
3. `base_image` of `xcraft.toml`: the base image, pinned by digest.
4. The packages installed in the image: those of `packages` of `xcraft.toml`, and those xcraft needs, sorted.
5. `macros` of `xcraft.toml`: the RPM macros of the builds.
6. `release_suffixes` of `xcraft.toml`.
7. `extra_upstream` of `xcraft.toml`.
8. The repositories of `xcraft.toml`, all of them, whatever their architectures: their settings, as in `xcraft.toml`.

The versions of the upstream packages of the image are not in the recipe, but in [the lock](#the-lock). See
`buildkey.recipe()` and `buildkey.recipe_key()`.

## The lock

For each architecture, what `xcraft.lock` has for the package, an array of:

1. **The image**: the upstream source packages of the image of the architecture, by name, with their locked versions.
2. **The build root**: the upstream source packages of its build root, but those of the image, by name, with their
   locked versions.
3. **Its dynamic BuildRequires**, those its `%generate_buildrequires` asks for, as recorded by its builds.
4. **What rpm generated** for the binary packages of the packages it is built against, directly or not, on that
   architecture: by binary package, an array of the requirements they resolve to, and of their capabilities our specs
   require. They are what the build root is resolved from, with the BuildRequires of the spec, which the package
   directory covers.

The build root and the image are resolved from the specs and the lock by dnf, for each architecture, on any machine:
the lock has their versions, so the key covers what the build environment installs, without resolving anything. See
`buildkey.lock_inputs()`.

## The dependencies

The packages of ours a package is built against are those whose binary packages its build root installs: those of
its BuildRequires, of its dynamic BuildRequires, and what they require, see `xcraft.graph.build_deps()`, and those
replacing upstream packages of the image, like `kernel-headers`, in all the builds but those of the packages they need.
The build key of a package covers their build IDs, with theirs, so a change of a package changes the keys of all the
packages built against it, directly or not.

The dependencies are those of all the architectures: a package built against another one on one architecture only,
with `%ifarch`, has it as a dependency on all of them, for the same key everywhere. See `buildkey.dependencies()`.

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
of an array of the build keys of the packages of its component, sorted by name, and of the cache keys of the packages
it is built against on any architecture, like for the build key, out of its component, sorted by name: the same on
all the architectures. A development build depends on the development builds it
is built against, which the build keys do not cover. A reused build keeps its build key, as the build of the main
branch. See `buildkey.development_key()`, and `xcraft.cache.build_keys()`, which chooses between both.

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

- **The upstream repositories do not change a version**: the lock has the versions of the upstream source packages,
  not the binary packages, nor their SHA-256, which dnf chooses and checks from the repositories, with the scripts of
  the build environment resolving them, like `resolve.py`. A version published again with other contents, or another
  choice of binary packages of the same versions, is not a change of the keys.
- **The URLs of `extra_upstream` do not change their RPMs**: they are covered by URL, like the base image by digest.
- **The order of `macros` and of the repositories in `xcraft.toml` does not change the builds**: macros are expanded
  when used, and the repositories have the same priority.
- **`source_baseurl` of the repositories** is covered, though only publishing uses it: excluding it changes all the
  keys, for the next change rebuilding all the packages anyway.

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
