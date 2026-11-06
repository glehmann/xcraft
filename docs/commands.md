# Commands

| Command | Description |
|---|---|
| `xcraft build [<pkg>...]` | Build packages, all by default, and the packages they need, skipping what is already built |
| `xcraft build <pkg> --no-deps` | Build only the given packages |
| `xcraft build <pkg> --dependents` | Also build the packages built against them |
| `xcraft build <pkg> --dry-run` (`-n`) | Show what would be built, and how long it took last time |
| `xcraft build [<pkg>...] --fetch=needed\|none` | Only fetch from the remote storage the builds the packages to build need, like CI, which then only builds what the storage misses; or fetch nothing, and build what is not cached locally |
| `xcraft build <pkg> -v` | Show the build log while building |
| `xcraft build <pkg> --locked` | Fail the builds that would change the lock: new upstream packages, or what a build found, for CI |
| `xcraft build --ids [--locked]` | Only update the build IDs in `xcraft.builds`, without building, or check them |
| `xcraft build [<pkg>...] --dev ID` / `--final` | Development builds with another identifier than `dev`, or `$XCRAFT_DEV`; or final builds, without development suffixes (see [Development builds](builds.md#development-builds)) |
| `xcraft build [<pkg>...] --nocheck` | Without running the tests of the specs, their `%check`, for faster iterations; also for `repo`, `iso` and `boot`, not with `--final` (see [Builds without their tests](builds.md#builds-without-their-tests)) |
| `xcraft repo <pkg>... [--dependents]` | When they have development builds, a repository of them alone, for test hosts |
| `xcraft repo <pkg>... --host h [--upgrade]` | The same, copied to test hosts over ssh, `ssh://[user@]host[:port]` for another port, and upgraded there |
| `xcraft repo <pkg>... --vm [--upgrade]` | The same, for the VM `xcraft boot` installed, running |
| `xcraft publish [repos\|isos] [--stage s] [--locked] [--local] [--unsigned] [--dev-key] [--arch a]` | Publish a snapshot of the distribution, its repositories and its ISOs, or only those given, for all the architectures or `--arch`, as the stage's latest one; a `--local` one is of the host's architecture by default |
| `xcraft iso [--netinstall] [--output f] [-V id] [--locked] [--unsigned] [--dev ID \| --final] [--nocheck]` | Make the installation ISO, from a local snapshot (see [Installation ISO](iso.md)) |
| `xcraft boot [--iso f] [--disk f] [--headless] [--unsigned] [--dev ID \| --final] [--nocheck]` | Build the ISO and boot its installer in a local QEMU VM, creating a virtual disk if missing |
| `xcraft vm start [--background]` | Start the VM again from its installed disk, without the ISO, with the options of `xcraft boot` |
| `xcraft vm <command>` | Inspect a VM, read its console, send input, save diagnostics, or stop it |
| `xcraft vm destroy [--yes]` | Stop the VM and delete its session, with its disk, for a new installation |
| `xcraft vm ssh [<command>...]` / `xcraft vm ssh-copy-id` | Log in as root in the VM, or run a command in it; install your SSH key there |
| `xcraft key create` / `xcraft key show [--export]` | The developer's key, which signs the development builds |
| `xcraft push` | Upload everything the remote storage misses: the source archives and the builds of all packages, with the `index.html` pages of their directories, and the resolutions |
| `xcraft push sources [<pkg>...] [--check] [--to main\|dev]` | Upload cached sources, or check the selected bucket; defaults to dev |
| `xcraft push builds [<pkg>...]` | Upload the current builds missing from the remote storage, for CI |
| `xcraft push isos [--unsigned] [--to main\|dev]` | Upload cached full and netinstall ISOs matching the checkout |
| `xcraft push resolutions` | Upload the resolutions of the project as it is, which new checkouts fetch rather than resolving, for CI |
| `xcraft push index` | Make the `index.html` pages of all the directories of the remote storage |
| `xcraft push ccache [<pkg>...]` | Update the compiler caches of the remote storage with those of the packages, for CI |
| `xcraft fetch` | Download everything the caches miss: the current builds of all packages, then their source archives |
| `xcraft fetch sources [<pkg>...]` | Download the source archives missing from the source cache, from the remote storage, or else from the source of the spec with their URL |
| `xcraft fetch builds [<pkg>...]` | Download the current builds missing from the build cache |
| `xcraft fetch ccache [<pkg>...]` | Add to the local compiler caches of the packages the entries of the remote storage |
| `xcraft lock` | Show the lock of upstream packages, the same for all the architectures, and the lock of the builds |
| `xcraft lock --outdated [<pkg>...]` | Show what `--update` would change, without changing it, and fail then, for CI |
| `xcraft lock --update [<pkg>...]` | Update locked packages, all or some (binary or source names), to the latest version all the architectures have, looked for natively, on any machine |
| `xcraft reuse <pkg>... [--dep <upstream>]` | Reuse the builds of the main branch despite changes, but to the packages' own directories, or despite updates of the `--dep` upstream packages only (see [Reusing builds](builds.md#reusing-builds)) |
| `xcraft repo [<pkg>...] [--output dir]` | Build packages, all by default, and what they need, put them in a repository, and check that everything they require is available |
| `xcraft shell <pkg>` | Open a shell in the build tree of a package, given by name or by a path in its directory, like `xcraft shell .` in it, with its build dependencies installed and its sources prepared, where `rpmbuild -ba SPECS/<spec>` builds it, without the network like the builds, the exact command shown before the prompt |
| `xcraft log <pkg> [--step build] [--grep regex] [--last/-n n]` | Show the log of the last build of a package, in `$PAGER` on a terminal |
| `xcraft log <pkg> -f` | Follow the log of a running build, until it ends |
| `xcraft graph <pkg> [--dependents] [--upstream] [--flat]` | Show the packages a package is built against as a tree, or the packages built against it; `--upstream` adds the upstream binary packages; `--flat` only lists them, ours in build order |
| `xcraft status [<pkg>...]`, or `st` | Show the version of each package, or of the given ones, with its build number and development build suffix, and whether it is built, or queued by a running build |
| `xcraft bump <pkg>... [-m message]` | Bump the release of packages, with a changelog entry, empty to fill in without `-m` |
| `xcraft bump <pkg> --version v [-m message]` | Update a package to a new version, with the archive of its `Source0`, and the packages of the same version; the changelog entry is `Update to <v>` by default |
| `xcraft clean [<pkg>...] [--builds] [--ccache] [--dune-cache]` | Remove the build trees and outdated builds, and optionally all builds, compiler caches and caches of dune, of the given packages or all; the source archives are kept |
| `xcraft history <pkg>` | Show the commits changing a package, with `git log`: its directory, and its lines in `xcraft.builds` |
| `xcraft patchset [repo] [--package p] [--base ref] [--first n]` | Update the patches of a package with the commits of the current git branch, from the developer's repository |
| `xcraft import <pkg or srpm>... [--branch b] [--url u] [--repo r]` | Import the history of packages from their packaging repositories, or source RPMs, as the git branches `import/<pkg>/<branch>` to merge, new packages into `base/`, or `<r>/` |
| `xcraft doctor [--fix]` | Check host prerequisites, including QEMU, UEFI firmware and KVM, even outside a project. Inside a project, also check architecture emulation and remote storage. With `--fix`, show and confirm commands to install missing tools and configure rootless podman and emulation, using the host distribution's package manager |
| `xcraft import --all` | Import again the packages which have import branches of histories, not of source RPMs only, on their branches |
| `xcraft --trace <command>` | Show stack traces on errors |
