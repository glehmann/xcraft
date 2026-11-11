# Dependencies and the lock

## Dependencies

A package depends on two kinds of packages, handled differently.

**Our packages**, the ones of the project, are known to xcraft from the parsed specs. A package depends on one of ours
when:

- one of its `BuildRequires` is the name, or an explicit `Provides`, of a binary package of ours;
- or, transitively, one of those binaries requires at runtime another binary of ours, since dnf installs it too in
  the build root.

Only the runtime requirements of the binaries actually needed count: `qemu` needs `xen` for `xen-libs-devel`, even
though another binary of `xen`, `xen-dom0-tools`, requires `qemu`. `xcraft graph <pkg>` shows these dependencies.

The specs do not have what rpm generates while building: the requirements of what our packages link against, their
automatic provides (sonames, `pkgconfig()`...), and their files. The builds record them in the lock, for the packages
built against them (see [The lock](#the-lock)). A build root with an upstream package with the name of one of ours,
instead of ours, fails the build: a `BuildRequires` on the name of the package fixes it.

They decide:

- the build order: our dependencies are built first, and a failed build skips the packages needing it;
- the cache keys: the key of a package includes the keys of its dependencies, so changing a package rebuilds the
  packages built against it;
- what the builds get: the builds of our packages are in a local repository with `priority=0`, above the other
  repositories, so dnf takes our package whenever we built one, even when a repository has a newer version (like
  AlmaLinux's ocaml 5.2, while we build 4.14).

**Everything else** comes from the repositories of the build environment: for XCP-ng, AlmaLinux 10 (BaseOS, AppStream,
CRB, extras, devel) and its EPEL, which get updates, the security fixes in particular. dnf resolves them, in the
locked versions only (see below): before building, to know the build roots, and in each build, where `dnf builddep`
installs the `BuildRequires`. A build environment is therefore made of the locked packages and of ours: a package of
ours that is not built, with `--no-deps` for instance, is not available.

To see them anyway, `xcraft graph --upstream` resolves the requirements between the locked packages, in the build
environment, and shows those our builds need: the providers of our `BuildRequires`, and of the runtime requirements
of our binaries installed in the build roots, with what they require. `xcraft graph --dependents python3` thus
shows our packages built against `python3`, through the upstream packages requiring it.

## The lock

`xcraft.lock`, a TOML file next to `xcraft.toml`, is committed with the specs. It is the same for all the
architectures, and has, one line per package for readable diffs:

- `[sources]`: one version of each upstream source package installed in the build environment image, in a build root,
  with the tools making the ISO, or signing, or published, for the whole tree, and of these only. On each
  architecture, dnf only sees the binary packages of that version, but the `release_suffixes` of `xcraft.toml`, like
  `[".alma_altarch"]` of the packages AlmaLinux rebuilds for x86_64_v2.
- `[image]`, by architecture, the upstream source packages of the build environment image, and `[roots]`, for each
  package of ours, those of its build root but the image's, shared by the architectures but for their own entries, in
  `[roots.<arch>]`: as resolved, for all the architectures, by the commands resolving them, which record them when
  they change.
- `[runtime]` and `[iso]`, by architecture: the upstream binary packages published, and those of the tools making the
  ISO, but the image's, resolved and recorded with the build roots.

`xcraft.builds`, next to it, is committed too: by package of ours, its build ID, see [Build IDs](builds.md#build-ids), and what
its builds found, which the specs do not have, each line starting with the name of its package, for line-based merges
and `jj log -r "diff_lines(regex:'^xen\.', 'xcraft.builds')"`, or `xcraft history xen`:

- `requires`, for each binary package of ours, the packages the requirements rpm generated for it resolve to, like
  `lzo` for `xen-dom0-libs`, which links against it: `xen.requires."xen-dom0-libs" = ["lzo"]`;
- `provides`, the capabilities and files rpm generated for it that a spec of ours requires, but those its spec
  declares, like its own name, with its version as `%{evr}`, or `%{version}`: `pkgconfig(xenctrl) = %{version}`;
- `buildrequires`, for each package with `%generate_buildrequires`, its dynamic `BuildRequires`.

An architecture which finds others has its own entries, which replace the shared ones, like `xen.aarch64.requires`.
So a bump of a package changes its spec and its `build` and `key` lines only.

**Build roots, resolved.** Before building, dnf resolves the build environment image, from the base image and
`packages` of `xcraft.toml`, and the build root of each package, from its `BuildRequires`, against the upstream
repositories, in the locked versions, and a repository of the metadata of our binary packages, from their specs and
what their builds found, without installing anything. It only reads metadata, so it runs natively, for all the
architectures, in the base image of the host, with the specs parsed for each architecture, like `rpmbuild --target`:
the keys of all the architectures are known on any machine, before building there. Upstream source packages needed
but not locked yet get locked, at the latest version all the architectures have, and the lock is committed with the
change of the spec; with `--locked`, for CI, it fails instead. See also [the design](lock-design.md).

**Checked builds.** A build only gets the builds of its dependencies. It records what only a build can know: its
dynamic `BuildRequires`, before installing them, and what rpm generated for its binary packages, which a build fetched
from the remote storage records too: each binary package as soon as its requirements resolve, those needing packages
of ours not built yet once they are, so that the builds against the others get what they need. It then checks that dnf installed the build root resolved for it, with what it
recorded, and that it made the binary packages parsed from its spec for the architecture, with the provides and
requirements the spec declares. A build of a package needing one of ours it was not built after is planned again. With
`--locked`, a build recording something fails.

**Updates.** The lock only changes otherwise with `xcraft lock --update`, which takes the latest version all the
architectures have of all the locked source packages, or of some of them, given by binary or source package name, to
get updates and security fixes: when the updates of an architecture are late, the others wait for it. It looks for
them natively, on any machine. `xcraft lock --outdated` shows what it would change, without changing it, and fails
then: a scheduled CI job can run it to notify of the updates, or run the update and propose it, like the one of
xcp-ng-dist.

Since the cache key of a package covers the locked versions of its build root, an update rebuilds the packages whose
build root has an updated package, and the packages built against them.

Rebuilt packages get new build numbers, which `xcraft lock --update` writes, see [Build IDs](builds.md#build-ids), so the
hosts get them. When an update does not need shipping rebuilds, like most ABI compatible updates, unlike static
linking, OCaml module hashes or soname changes, `xcraft reuse --dep` keeps the builds, see
[Reusing builds](builds.md#reusing-builds).

**Limits.** What rpm generates for a package of ours, and its dynamic `BuildRequires`, are only known after its first
build: until then, the keys of the packages built against it miss them, and that build corrects them. A file of ours a
spec requires is only known once the package installing it is built: until then, the build roots needing it cannot be
resolved, which their builds say.

**Checking.** `xcraft repo` runs `dnf repoclosure` on the built packages, with all their requirements, generated
ones included, against the local repository and the build environment's ones: it reports what nothing provides.
