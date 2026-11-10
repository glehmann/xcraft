# Design: remote builders

Status: implemented for `xcraft build`, in `xcraft/builders.py`: the builder of the architecture of the command. To
do: `xcraft clean --builders`, the checks of `xcraft doctor`, the final builds, and the builds of several architectures
at the same time.

## The problem

A machine builds natively for its own architecture only: another one is built under emulation, with podman and
qemu-user-static (see [How it works](how-it-works.md)). That is fine for the small packages, but slow for the large
ones, like the kernel, xen or xapi, and their builds keep the network, which qemu-user cannot isolate. So a developer
on x86_64 cannot try a change of a large package on aarch64, or of its dependencies, in a reasonable time: only the CI
builds them natively, once the change is pushed, for the final builds only. And a slow machine, like a laptop,
takes long to build the large packages of its own architecture too.

## Goals

- Build the packages of an architecture on a remote host of that architecture, natively, from the checkout as it is,
  uncommitted changes included, like the local builds: another architecture than the local one, rather than under
  emulation, or the local one, on a faster machine.
- The same builds as local ones: the same keys, numbers and development build suffixes, cached locally once made, for
  `xcraft repo`, `publish`, `iso` and `boot` to use them as if they were built locally.
- The remote host keeps its caches between the builds: compiler and dune caches, images, pinned packages.
- Nothing to do on the remote host but having podman, uv and an SSH access.

## Why it works

The build keys do not depend on the machine, its path, nor the architecture (see [build keys](build-keys.md)): a build
of aarch64 made on an aarch64 host has the key and the number the emulated one would have had. Their inputs are the
files of the checkout, the locks and `xcraft.builds`, and the recipe of the version of xcraft. So a copy of them on the
remote host, with the same xcraft, builds the same packages.

## Not podman remote

`podman --remote`, over SSH, runs the containers on the remote host, but the builds bind mount local paths: the
package directory, the source cache, the repositories of the pinned packages and of our builds, the compiler and dune
caches, the build trees. On a remote podman, they are the paths of the remote host. Every mount would need a copy,
through all the code running containers. Running xcraft itself on the remote host keeps the builds as they are.

## Running xcraft on the remote host

The builders are the developer's machines, not the project's: they are configured out of the checkout, never in
`xcraft.toml`, in the configuration of the user, `$XDG_CONFIG_HOME/xcraft/config.toml`, `~/.config/xcraft/` by
default, for all their checkouts:

```toml
[builders]
aarch64 = "ssh://builder@arm64.example"
x86_64_v2 = "ssh://builder@x86.example"
```

`$XCRAFT_BUILDERS`, like `aarch64=ssh://arm64.example,x86_64_v2=`, overrides it, an empty host for none, like in a
script or for one command. Each gives a host by architecture, as given to ssh, or as `ssh://[user@]host[:port]`, like
`xcraft repo --host`. The builds of that architecture are then made on it: those of another architecture than the
local one, rather than under emulation, and those of the local one, like from a laptop to a faster machine.
`xcraft build` builds the architecture of the command, `--arch`, or the host's, on its builder, if any, or locally
with `--local`, under emulation for another architecture. The builder itself always builds with `--local`: a builder
whose configuration names a builder, or itself, would otherwise call it again, without end.

The configuration of the user is a file of its own, for what is the developer's, like the builders: not to mix with
`xcraft.toml`, the project's, nor with the state of the checkout, `.xcraft/`, which `xcraft clean` cleans. A
configuration of the checkout, ignored by git, would serve the settings differing between checkouts of one developer,
which the builders do not.

Through one SSH connection, with its control socket, like `xcraft repo --host`:

0. **The builder.** Before copying anything, xcraft checks that the host has podman, gpg, git, rsync, flock and uv,
   and that its podman is rootless, as xcraft runs it, failing with what it lacks otherwise. git above all: without
   it, the copy would have nothing to number its builds against, which would be final ones rather than development
   ones, silently, under the keys the builder gives. gpg would fail only once built, when signing.
1. **The checkout.** The working tree is copied with rsync, in `~/.cache/xcraft/builders/<project id>/` on the host:
   the files git has, and the untracked ones not ignored, which the keys cover, with `--delete`, but not `.xcraft/`.
   The project ID tells apart the checkouts of a developer, and the user those of the developers sharing a host.
2. **The state.** `.xcraft/dev-builds.json`, the counters of the development builds, for the same suffixes, and the
   developer's key, see [Signing](#signing). The locks and `xcraft.builds` are in the working tree.
3. **xcraft.** The host runs the same xcraft as the checkout: `uv tool run --from <the same source>`, its version, or
   the commit of its git repository, or, for a development version, a wheel of the local one, built and copied to the
   host when it changed; or the command of `$XCRAFT_BUILDER_XCRAFT`, for an xcraft installed there otherwise. Before
   building, both compare the hash of the recipe, `buildkey.recipe()`, which the hidden `xcraft keys --recipe` gives
   there: another xcraft would make builds of other keys, which fails the command.
4. **The build.** `xcraft build --arch aarch64 <packages> --dev <ident> --local`, with the options of the command,
   over SSH, its output streamed, with `XCRAFT_PROJECT` set, and `--maintenance` naming the branch the builds are
   numbered against when the checkout names one, like a bookmark of jj, which the copy, without `.jj/`, cannot tell.
   The host fetches what it needs as any checkout: the source archives, the final builds of the dependencies and the
   resolutions from the remote storage, its credentials not needed for that; the compiler caches too. It keeps its
   caches: the next build of the kernel finds its objects.
5. **The results.** The build trees of aarch64, in `.xcraft/cache/builds/<fingerprint>/aarch64/`, are copied back
   with rsync, with the logs of the builds, `.xcraft/dev-builds.json` and `xcraft.builds`, with what the builds
   recorded, see [Recording](#recording). `xcraft keys` gives the cache keys of the builds there, which the local side
   checks came back: without preparing the project, nor building the image of the host, which a slow machine would
   take long to. The local commands find the builds in the cache, under their keys.

A build failing on the host fails the local command, with its log copied back too, for `xcraft log`.

## Signing

The builds are cached by the key signing them, `cache/builds/<fingerprint>/`, and the local commands trust their own
key, or the developer's. Two ways:

- **The developer's key on the host.** The key of development builds, made by `xcraft key create`, has no
  passphrase, and signs nothing but development builds: copied to the host, in the state of the remote checkout, the
  builds come back signed by it, as if made locally. The host must be trusted with it, like a test host.
- **Signed again once back.** The host signs with a key of its own, and the local side signs the builds again with
  the developer's key, as `signed_build()` does for the builds of another key. The host never gets the key, but each
  build is copied and signed again.

The first one is simpler, and the key of development builds is not a release key: it is the one taken, the second one
left for later, for hosts shared more widely, if needed.

## Recording

A build may record what it found in `xcraft.builds`, like the requirements rpm generated, which can change the keys of
the packages built against it. The host writes them in its copy of `xcraft.builds`; the local side takes the entries
of the architecture of the host back: its `xcraft.builds` replaces the local one, which the lock of the project,
held during the whole build, keeps from changing meanwhile. With `--locked`, the builder fails when it would change.

## Concurrency and cleanup

- A command building on a host holds a lock there, `<project id>.lock` next to the remote checkout, with flock: two
  commands of the same checkout build there one at a time, those of other checkouts or developers at the same time.
  Locally, it holds the lock of the project, as the local builds do.
- The images, the pinned packages and the dnf caches of the host are shared by its checkouts, as on any machine.
- `xcraft clean --builders` removes the remote checkout and its caches on the hosts; `xcraft clean` on the host itself
  works as usual.

## Security

- The host gets the checkout as it is, uncommitted changes included: under embargo, it must be one of the private
  hosts, see [the embargo design](embargo-design.md).
- It gets the developer's key of development builds, with the first way of signing.
- It never gets S3 credentials: it pushes nothing, the results come back over SSH.

## What it is not

- **Not CI.** The final builds of the main branch are still made by CI, natively, and pushed to the remote storage:
  the remote builders are for the development builds of a developer, and their final builds, like the local ones.
- **Not a pool.** One host by architecture: no scheduling between several hosts, nor between the local machine and
  its builder, but by the open question below.
- **Not through the remote storage.** The development builds are never pushed, so they come back over SSH.

## The changes

| Change | Where | Status |
|---|---|---|
| The configuration of the user, `[builders]`, `$XCRAFT_BUILDERS`, `--local` | `builders.py`, `commands/build.py` | done |
| The copy of the checkout and its state, the run over SSH, the copy of the results | `builders.py`, new | done |
| The same xcraft on the host, by its recipe, and its builds checked once back, by the keys it gives | `builders.py`, `commands/build.py` | done |
| The developer's key copied | `builders.py` | done |
| `xcraft.builds` taken back | `builders.py` | done |
| The SSH helpers of `xcraft repo --host` shared | `hosts.py`, new | done |
| The lock of the remote checkout | `builders.py` | done |
| `xcraft clean --builders` | `commands/clean.py` | to do |
| `xcraft doctor` checking the builders: SSH, podman rootless, uv | `commands/doctor.py` | to do |
| The final builds, with `--final` or `--key`, refused on a builder for now | `commands/build.py` | to do |

## Decisions

- **Signing:** the developer's key is copied to the host, see [Signing](#signing); the builds signed again once back
  are for later, if hosts get shared more widely.
- **The commands:** `xcraft build` only. `xcraft repo`, `iso`, `boot` and `publish` build first, and could use the
  builders the same way, through the same code of the builds, but may not need to: a developer builds, then uses the
  builds, which are in the cache. To see once used.
- **xcraft under development:** a wheel of the local one, built and copied to the host when it changed.
- **The small packages:** all the packages of the builder's architecture are built on it, without a threshold
  keeping the small ones local: once measured, a native build on an arm64 builder is much faster than the same one
  under emulation, even with the copies to the host and back.
