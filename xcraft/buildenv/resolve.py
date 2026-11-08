"""Write as JSON the build environment image and the build roots of an architecture, resolved from the lock.

Run as root, natively, with the dnf python API, for any architecture: ``resolve.py <input> <output>``, with the
repositories of the architecture in /etc/yum.repos.d/. Nothing is installed: dnf only resolves, against the metadata
of the repositories, with the upstream packages of the locked versions of their sources only, and a repository of our
binary packages, written from their specs.

The input has:
- ``arch``, ``suffixes``, the release suffixes of the rebuilds for an architecture, ``sources``, the locked version of
  each upstream source package, ``base``, the packages of the base image, ``image``, the packages installed over them,
  and ``rpms``, the paths of binary packages of ours, built, and of extra upstream ones;
- ``ours``, our binary packages, and ``builds``, for each package of ours, its ``buildrequires`` and the ``allowed``
  binary packages of ours, of its dependencies, to resolve its build root;
- ``record``, for built binary packages of ours, the ``requires`` and ``provides`` their specs declare, and ``wanted``,
  the capabilities our specs require, to resolve what rpm generated for them;
- ``versions``, upstream source packages to give the available versions of;
- ``runtime``, with ours, what the published repositories need: the ``extra`` upstream packages published, the
  ``installer``'s, the ``iso`` packages of the repository of the ISO, and the ``tools`` making the ISO.

The output has the locked binary ``packages``, the ``image``, the upstream ``buildroots`` and the ``ours`` binary
packages of each build root, the ``requires`` and ``provides`` of the recorded binary packages, the ``errors``, by
package, ``image`` for the image, the upstream packages of the ``runtime``, with its ``runtime_errors``, and of the
``tools``, besides the image's, with their ``tools_error``, the ``unlocked`` source packages needed, the
``versions`` of those and of the given ones, oldest first, the packages of the ``sets`` of the ISO, ``iso`` and
``install.img``, and ``why`` each package is in each set, see Resolver.why().
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import quoteattr

# pyright: reportPossiblyUnboundVariable=false
try:
    import dnf
    import hawkey
except ImportError:  # out of the build environment, for the tests of the helpers
    pass
if TYPE_CHECKING:
    from collections.abc import Sequence

    from dnf.package import Package
    from dnf.query import Query

OPS = {"<": "LT", "<=": "LE", "=": "EQ", ">=": "GE", ">": "GT"}
OURS = ["xcraft-ours", "@commandline"]
"""The repositories of our packages: written from the specs, and built."""
CCACHE_COMPILERS = ["gcc", "clang"]
"""The compilers ccache wraps, which the build roots having them get ccache with, see build.sh: g++ needs gcc, and
clang++ is clang's."""


def canonical(sourcerpm: str, suffixes: list[str]) -> tuple[str, str]:
    """Return the name and the version-release of a source package, without the release `suffixes` of an
    architecture.
    """
    name, version, release = sourcerpm.removesuffix(".src.rpm").rsplit("-", 2)
    vr = f"{version}-{release}"
    for suffix in suffixes:
        vr = vr.replace(suffix, "")
    return name, vr


def entry(dep: str) -> str:
    """Return the repodata entry of a dependency, like ``foo >= 1:2-3``, or a rich one, like ``(a if b)``."""
    name, op, evr = (dep, "", "") if dep.startswith("(") else (*dep.split(" ", 2), "", "")[:3]
    if not op:
        return f"<rpm:entry name={quoteattr(name)}/>"
    epoch, _, vr = evr.rpartition(":")
    version, _, release = vr.partition("-")
    attrs = f' flags="{OPS[op]}" epoch="{epoch or 0}" ver={quoteattr(version)}' + (
        f" rel={quoteattr(release)}" if release else ""
    )
    return f"<rpm:entry name={quoteattr(name)}{attrs}/>"


def primary(ours: list[dict]) -> bytes:
    """Return the primary metadata of a repository of our binary packages `ours`, without the packages."""
    packages = []
    for b in ours:
        epoch, _, vr = b["evr"].rpartition(":")
        version, release = vr.rsplit("-", 1)
        deps = "".join(
            f"<rpm:{kind}>{''.join(entry(d) for d in b[kind])}</rpm:{kind}>"
            for kind in ("provides", "requires", "recommends")
            if b[kind]
        )
        nevra = f"{b['name']}-{b['evr']}.{b['arch']}"
        packages.append(
            f'<package type="rpm"><name>{b["name"]}</name><arch>{b["arch"]}</arch>'
            f'<version epoch="{epoch or 0}" ver={quoteattr(version)} rel={quoteattr(release)}/>'
            f'<checksum type="sha256" pkgid="YES">{hashlib.sha256(nevra.encode()).hexdigest()}</checksum>'
            f'<location href="{nevra}.rpm"/><format><rpm:sourcerpm>{b["package"]}.src.rpm</rpm:sourcerpm>'
            f"{deps}</format></package>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<metadata xmlns="http://linux.duke.edu/metadata/common"'
        f' xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="{len(packages)}">{"".join(packages)}</metadata>\n'
    ).encode()


def write_repo(path: Path, ours: list[dict]) -> None:
    """Write in `path` the metadata of a repository of our binary packages `ours`, without the packages."""
    metadata = primary(ours)
    compressed = gzip.compress(metadata, mtime=0)
    (path / "repodata").mkdir(parents=True)
    (path / "repodata/primary.xml.gz").write_bytes(compressed)
    (path / "repodata/repomd.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<repomd xmlns="http://linux.duke.edu/metadata/repo">'
        '<data type="primary">'
        f'<checksum type="sha256">{hashlib.sha256(compressed).hexdigest()}</checksum>'
        f'<open-checksum type="sha256">{hashlib.sha256(metadata).hexdigest()}</open-checksum>'
        f'<location href="repodata/primary.xml.gz"/><size>{len(compressed)}</size>'
        f"<open-size>{len(metadata)}</open-size>"
        "</data></repomd>\n"
    )


def versioned_names(dep: str) -> list[str]:
    """Return the names a dependency `dep` refers to with a version, like ``foo >= 1.0``, or in a rich dependency, like
    ``(pkgconfig(foo) >= 1.0 or bar)``: all its words then, with the parentheses of its syntax stripped, not those of
    the names, which the keywords and the versions do not match.
    """
    if not dep.startswith("("):
        return [words[0]] if len(words := dep.split()) == 3 else []  # noqa: PLR2004, name, operator, version
    names = []
    for word in dep.split():
        name = word
        while name.startswith("(") and name.count("(") > name.count(")"):
            name = name[1:]
        while name.endswith(")") and name.count(")") > name.count("("):
            name = name[:-1]
        names.append(name)
    return names


def _providers(query: Query, dep: str) -> list[Package]:
    """Return the packages of `query` providing `dep`: by name, capability, or file; none of rpmlib, nor for a rich
    dependency, which is not walked.
    """
    if dep.startswith("rpmlib("):
        return []
    try:
        found = query.filter(name=dep) or query.filter(provides=dep)
        return list(found or (query.filter(file=dep) if dep.startswith("/") else []))
    except hawkey.Exception:
        return []


class Resolver:
    """dnf, on the repositories of the architecture of `data`, with the upstream packages of the locked versions only,
    and ours.
    """

    def __init__(self, data: dict) -> None:
        """Load the repositories, with the upstream packages of the other versions excluded."""
        self.data = data
        base = self.base = dnf.Base()
        base.conf.read()
        base.conf.installroot = tempfile.mkdtemp()
        base.conf.substitutions["arch"] = data["arch"]
        base.conf.substitutions["basearch"] = dnf.rpm.basearch(data["arch"])
        base.read_all_repos()
        # at the same path each time, for the same directory in the dnf cache, shared by the containers
        ours = Path("/xcraft-ours")
        write_repo(ours, data["ours"])
        repo = base.repos.add_new_repo("xcraft-ours", base.conf, baseurl=[ours.as_uri()], priority=1)
        # read again, even when cached in the same second, like that of another project resolved just before: dnf
        # keeps it then, even with metadata_expire=0
        repo._repo.expire()  # noqa: SLF001, like dnf expire-cache
        base.fill_sack(load_system_repo=False)
        self.built = {p.name: p for p in base.add_remote_rpms(data["rpms"])}
        self.versions: dict[str, set[str]] = {}
        sources: dict[str, tuple[str, str]] = {}  # by source RPM, which many binaries share
        excluded = []
        for p in base.sack.query().available().filter(reponame__neq=OURS):
            if (sourcerpm := p.sourcerpm) not in sources:
                sources[sourcerpm] = self.canonical(sourcerpm)
            source, vr = sources[sourcerpm]
            self.versions.setdefault(source, set()).add(vr)
            if source in data["sources"] and vr != data["sources"][source]:
                excluded.append(p)
        base.sack.add_excludes(excluded)
        # the latest of each name, of all its architectures
        latest = base.sack.query().available().filter(reponame__neq=OURS).latest()
        self.binaries: dict[str, Package] = {}
        for p in latest:
            if (other := self.binaries.get(p.name)) is None or p.evr_cmp(other) >= 0:
                self.binaries[p.name] = p
        # those of several architectures in that version, like the multilib packages: the lock pins one by name
        self.multiarch = {
            p.name for p in latest if p.arch != self.binaries[p.name].arch and p.evr_cmp(self.binaries[p.name]) == 0
        }
        self.ours = {p.name: p for p in base.sack.query().available().filter(reponame="xcraft-ours")}
        self.needed: set[str] = set()
        """The upstream source packages of what was resolved."""

    def canonical(self, sourcerpm: str) -> tuple[str, str]:
        """Return the name and the version-release of a source package, as locked."""
        return canonical(sourcerpm, self.data["suffixes"])

    def add(self, dep: str) -> None:
        """Add a requirement to the goal, like dnf builddep: by provides, or else by file."""
        selector = dnf.selector.Selector(self.base.sack)
        selector.set(provides=dep)
        if not selector.matches() and dep.startswith("/"):
            selector = dnf.selector.Selector(self.base.sack)
            selector.set(file=dep)
        if not selector.matches() and not dep.startswith("("):
            msg = f"nothing provides {dep}: when a package of ours does, build it first"
            raise dnf.exceptions.Error(msg)
        self.base._goal.install(select=selector)  # noqa: SLF001, like dnf builddep

    def unmet_weak(self, packages: list[Package]) -> list[Package]:
        """Return the providers of the weak dependencies of `packages` they do not have, like
        exclude_from_weak_autodetect of dnf: they are not installed as weak dependencies after them.
        """
        names = {p.name for p in packages}
        available = self.base.sack.query().available()
        providers = [p for p in available.filter(supplements=packages) if p.name not in names]
        for r in {r for p in packages for r in p.recommends}:
            found = available.filter(provides=r)
            if not any(p.name in names for p in found):
                providers.extend(found)
        return providers

    def resolve(
        self,
        packages: list[Package],
        deps: list[str],
        *,
        weak: bool,
        installed: Sequence[Package] = (),
        exclude_from_weak: Sequence[Package] = (),
    ) -> list[Package]:
        """Return the packages to install for `packages`, and the requirements `deps`, on the `installed` ones, which
        those replacing them, by obsoleting them, can leave out, like xcp-ng-release almalinux-release.
        """
        base = self.base
        base.reset(goal=True)
        base.conf.install_weak_deps = weak
        base.conf.exclude_from_weak = [str(p) for p in exclude_from_weak]  # which resolve() takes
        # There is no system repository: these packages are available, never already installed.
        for items, optional in ((installed, True), (packages, False)):
            for p in items:
                selector = dnf.selector.Selector(base.sack)
                selector.set(pkg=[p])
                base._goal.install(select=selector, optional=optional)  # noqa: SLF001, like dnf package_install
        for dep in deps:
            self.add(dep)
        base.resolve()
        result = list(base.transaction.install_set)
        self.needed.update(self.canonical(p.sourcerpm)[0] for p in result if p.reponame not in OURS)
        return result

    def image(self, errors: dict[str, str]) -> list[Package]:
        """Return the packages of the image, like the Containerfile installs them: without weak dependencies, and
        without ours.
        """
        ours = self.base.sack.query().filter(reponame=OURS)
        self.base.sack.add_excludes(ours)
        # all those of the base image too: the checks of the image and of the build roots allow pinned packages only
        wanted = [*self.data["image"], *self.data["base"]]
        try:
            if missing := sorted({n for n in wanted if n not in self.binaries}):
                errors["image"] = f"not in the repositories, in the locked version: {', '.join(missing)}"
                return []
            return self.resolve([self.binaries[n] for n in wanted], [], weak=False)
        except dnf.exceptions.Error as e:
            errors["image"] = str(e)
            return []
        finally:
            self.base.sack.remove_excludes(ours)

    def buildroot(
        self, image: list[Package], build: dict, unmet_weak: list[Package], swappable: set[str]
    ) -> tuple[list[Package], set[str]]:
        """Return the build root of the `build` of a package of ours, on the `image`, like dnf builddep, with the weak
        dependencies, but the `unmet_weak` ones of the image, and with ours rather than the upstream packages of their
        names, of the image or of the build root, but those of ours not `swappable`, which need the package; and the
        upstream packages of the build root ours replaced.
        """
        allowed = set(build["allowed"])
        # those of ours replacing the image's, like kernel-headers, in the builds of the packages needing them
        packages = [self.ours[p.name] for p in image if p.name in allowed]
        installed = [p for p in image if p.name not in allowed]
        deps, swapped, kept = list(build["buildrequires"]), set(), set()
        root = self._buildroot(packages, deps, installed, allowed, unmet_weak)
        while True:
            # like build.sh, ccache with the compilers it wraps
            if {p.name for p in root} & set(CCACHE_COMPILERS) and "ccache" not in deps:
                deps.append("ccache")
                root = self._buildroot(packages, deps, installed, allowed, unmet_weak)
                continue
            # those of ours replacing the upstream ones the build root comes with, like kernel-headers with gcc
            if not (new := {p.name for p in root if p.reponame not in OURS and p.name in swappable} - allowed - kept):
                return root, swapped
            more = [*packages, *(self.ours[n] for n in sorted(new))]
            try:
                root = self._buildroot(more, deps, installed, allowed | new, unmet_weak)
            except dnf.exceptions.Error:
                # those ours cannot replace yet, like for a file they are not known to have: see checks.resolved_root()
                kept |= new
                continue
            allowed, swapped, packages = allowed | new, swapped | new, more

    def _buildroot(
        self,
        packages: list[Package],
        deps: list[str],
        installed: list[Package],
        allowed: set[str],
        unmet_weak: list[Package],
    ) -> list[Package]:
        """Return the packages to install for `packages` and `deps` on the `installed` ones, with only the `allowed`
        packages of ours.
        """
        others = self.base.sack.query().filter(reponame="xcraft-ours").filter(name__neq=list(allowed))
        self.base.sack.add_excludes(others)
        try:
            return self.resolve(packages, deps, weak=True, installed=installed, exclude_from_weak=unmet_weak)
        finally:
            self.base.sack.remove_excludes(others)

    def runtime(
        self, extra: list[str], installer: list[str], iso: list[str], errors: dict[str, str]
    ) -> tuple[list[str], dict[str, tuple[list[Package], list[str]]]]:
        """Return the upstream packages our binary packages, the extra upstream RPMs, and the `extra` upstream packages
        need, each resolved alone, on an empty system, without weak dependencies, as some of ours conflict with others,
        and the packages of the `installer`, and those of the `iso` repository, each together: some only need others
        along with another, like (libsss_sudo if sudo); and the packages of each group resolved, with its packages, by
        its name: the package, install.img or ISO.
        """
        packages = {**self.binaries, **self.ours, **self.built}  # like dnf install, ours rather than upstream ones
        roots = [n for n in {*self.ours, *self.built} if not n.endswith(("-debuginfo", "-debugsource"))]
        groups = [
            *((n, [n]) for n in sorted({*roots, *extra})),
            *((name, group) for name, group in (("install.img", installer), ("ISO", iso)) if group),
        ]
        needed, resolved = set(), {}
        for name, group in groups:
            try:
                installed = self.resolve(
                    [packages[n] for n in group if n in packages], [n for n in group if n not in packages], weak=False
                )
                needed |= {p.name for p in installed if p.reponame not in OURS}
                resolved[name] = (installed, group)
            except dnf.exceptions.Error as e:
                errors[name] = str(e)
        return sorted(needed), resolved

    def why(
        self, packages: list[Package], roots: list[tuple[str, str, list[str]]], *, weak: bool
    ) -> dict[str, list[str]]:
        """Return why each of the `packages` is there, by name, as [kind, by, requirement]: by a breadth-first walk
        from the `roots`, of their kind, like ours, extra or image, or buildrequires by a package of ours, and of their
        requirements, then of the requirements of the packages reached, kind requires, by their names, and with
        `weak`, of their recommendations, kind recommends, to their providers among the `packages`. The first way found
        only: those required by several get one.
        """
        query = self.base.sack.query().filterm(pkg=packages)
        found: dict[str, list[str]] = {}
        reached: list[Package] = []

        def reach(dep: str, how: list[str]) -> None:
            for p in _providers(query, dep):
                if p.name not in found:
                    found[p.name] = how
                    reached.append(p)

        for kind, by, deps in roots:
            for dep in deps:
                reach(dep, [kind, by, dep])
        for p in reached:  # growing as it goes: breadth-first
            for kind, deps in (("requires", p.requires), ("recommends", p.recommends if weak else [])):
                for dep in map(str, deps):
                    reach(dep, [kind, p.name, dep])
        return found

    def tools(self, image: list[Package], tools: list[str]) -> tuple[list[str], str]:
        """Return the upstream packages installed with the `tools` on the `image`, without weak dependencies, but the
        image's, or why they cannot be.
        """
        ours = self.base.sack.query().filter(reponame=OURS)
        self.base.sack.add_excludes(ours)
        try:
            packages = [self.binaries[n] for n in tools if n in self.binaries]
            installed = self.resolve(
                packages, [n for n in tools if n not in self.binaries], weak=False, installed=image
            )
        except dnf.exceptions.Error as e:
            return [], str(e)
        finally:
            self.base.sack.remove_excludes(ours)
        return sorted({p.name for p in installed} - {p.name for p in image}), ""

    def generated(self, image: list[Package], name: str, declared: dict) -> tuple[list[str], list[str]]:
        """Return the packages the generated requirements of the built package `name` resolve to, on the `image`, with
        its packages too, which the published repositories need, and its generated capabilities and files the specs
        require, but those it `declared`.
        """
        built = self.built[name]
        installed = self.resolve([built], [], weak=False, installed=image)
        others = self.base.sack.query().filter(pkg=installed).filter(name__neq=name)
        generated = [r for r in built.requires if str(r) not in declared["requires"]]
        wanted = set(self.data["wanted"])
        return (
            sorted({p.name for r in generated for p in others.filter(provides=r) or others.filter(file=str(r))}),
            # and the files, which the metadata of ours, from the specs, does not have
            sorted(
                {str(p) for p in built.provides if str(p).split(" ", 1)[0] in wanted} - set(declared["provides"])
                | wanted & set(built.files)
            ),
        )

    def versioned(self) -> dict[str, str]:
        """Return the binary packages of ours a package of the repositories, upstream or ours, a build of ours, or what
        is published refers to with a version, or in a rich dependency, by any of their names, with their
        epoch-version-releases: their other versions may resolve otherwise, which xcraft.resolution checks before
        using what this resolved for others.
        """
        providers: dict[str, set[str]] = {}  # the names of ours, and their capabilities, to those having them
        for p in self.ours.values():
            for name in [p.name, *(str(c).split(" ", 1)[0] for c in p.provides)]:
                providers.setdefault(name, set()).add(p.name)
        relations = ("requires", "recommends", "suggests", "supplements", "enhances", "conflicts", "obsoletes")
        deps = [str(d) for p in self.base.sack.query().available() for r in relations for d in getattr(p, r)]
        # and the build requirements of ours, and the requests of what is published, not in the metadata of packages
        deps += [r for build in self.data.get("builds", {}).values() for r in build["buildrequires"]]
        runtime = self.data.get("runtime", {})
        deps += [*runtime.get("extra", []), *runtime.get("installer", []), *runtime.get("iso", [])]
        found = {b for dep in deps for name in versioned_names(dep) for b in providers.get(name, ())}
        return {name: self.ours[name].evr for name in sorted(found)}

    def by_version(self, vrs: set[str]) -> list[str]:
        """Return the versions-releases `vrs`, oldest first."""

        def compare(a: str, b: str) -> int:
            return hawkey.split_nevra(f"x-0:{a}.noarch").evr_cmp(hawkey.split_nevra(f"x-0:{b}.noarch"), self.base.sack)

        return sorted(vrs, key=functools.cmp_to_key(compare))


def _sha256(package: Package) -> str:
    """Return the SHA-256 of the RPM of `package`, which the lock pins it by: the repositories give it."""
    if not package.chksum:
        sys.exit(f"no checksum of {package} in the metadata of its repository: the lock cannot pin it")
    return package.chksum[1].hex()


def _why_build(
    resolver: Resolver, data: dict, image: list[Package], roots: dict[str, list[Package]]
) -> dict[str, dict]:
    """Return why each package of the image and of the build `roots`, by package of ours, is there, as the set build."""
    packages = {p.name: p for p in [*image, *(p for root in roots.values() for p in root)]}
    starts = [("image", "", [p.name for p in image])]
    starts += [("buildrequires", name, data["builds"][name]["buildrequires"]) for name in sorted(roots)]
    return {"build": resolver.why(list(packages.values()), starts, weak=True)}


def _published_sets(
    resolver: Resolver, groups: dict[str, tuple[list[Package], list[str]]], extra: list[str]
) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Return the packages of the sets of the ISO, from the `groups` of the runtime, and why each package of the
    published sets is there: base, of all the groups, of ours, the `extra` upstream packages, and of the ISO, and iso
    and install.img.
    """
    kinds = {"ISO": ("iso", "iso"), "install.img": ("install.img", "installer")}
    published = {p.name: p for packages, _ in groups.values() for p in packages}
    starts = [
        (kinds[g][1] if g in kinds else "extra" if g in extra else "ours", "", group)
        for g, (_, group) in groups.items()
    ]
    why = {"base": resolver.why(list(published.values()), starts, weak=False)}
    sets = {}
    for group, (name, kind) in kinds.items():
        if group in groups:
            packages, roots = groups[group]
            sets[name] = sorted(p.name for p in packages)
            why[name] = resolver.why(packages, [(kind, "", roots)], weak=False)
    return sets, why


def main() -> None:
    """Resolve the input of the command line into its output."""
    data = json.loads(Path(sys.argv[1]).read_text())
    resolver = Resolver(data)
    result: dict = {"buildroots": {}, "ours": {}, "errors": {}, "requires": {}, "provides": {}, "runtime_errors": {}}
    image = resolver.image(result["errors"])
    result["image"] = sorted(p.name for p in image)
    unmet_weak = resolver.unmet_weak(image)
    owners = {b["name"]: b["package"] for b in data["ours"]}
    # the packages of ours each build may get: those of its dependencies, built before it
    needed = {name: {owners[b] for b in build["allowed"] if b in owners} for name, build in data["builds"].items()}
    roots = {}  # the packages of the build roots, for why
    for name, build in data["builds"].items():
        # ours of other packages than those needing it
        swappable = {b for b, owner in owners.items() if owner != name and name not in needed.get(owner, set())}
        try:
            installed, swapped = resolver.buildroot(image, build, unmet_weak, swappable)
        except dnf.exceptions.Error as e:
            result["errors"][name] = str(e)
            continue
        roots[name] = installed
        # with the upstream packages ours replace, like the image has those ours replace in the builds
        result["buildroots"][name] = sorted({*(p.name for p in installed if p.reponame not in OURS), *swapped})
        result["ours"][name] = sorted(p.name for p in installed if p.reponame in OURS)
    for name, declared in data["record"].items():
        try:
            result["requires"][name], result["provides"][name] = resolver.generated(image, name, declared)
        except dnf.exceptions.Error as e:
            result["errors"][name] = str(e)
    result["why"] = _why_build(resolver, data, image, roots) if roots else {}
    if runtime := data.get("runtime"):
        result["runtime"], groups = resolver.runtime(
            runtime["extra"], runtime["installer"], runtime["iso"], result["runtime_errors"]
        )
        result["sets"], why = _published_sets(resolver, groups, runtime["extra"])
        result["why"] |= why
        if runtime["tools"]:
            result["tools"], result["tools_error"] = resolver.tools(image, runtime["tools"])
    locked = data["sources"]
    result["packages"] = {
        name: {"evr": p.evr, "arch": p.arch, "source": source, "repo": p.reponame, "sha256": _sha256(p)}
        for name, p in resolver.binaries.items()
        if (source := resolver.canonical(p.sourcerpm)[0]) in locked
    }
    if multiarch := sorted(resolver.multiarch & result["packages"].keys()):
        sys.exit(f"upstream packages of several architectures, which the lock cannot pin: {', '.join(multiarch)}")
    result["unlocked"] = sorted(resolver.needed - locked.keys())
    if "builds" in data:  # see xcraft.resolution._unversioned()
        result["versioned"] = resolver.versioned()
    result["versions"] = {
        s: resolver.by_version(resolver.versions[s])
        for s in {*result["unlocked"], *data["versions"]}
        if s in resolver.versions
    }
    Path(sys.argv[2]).write_text(json.dumps(result))


if __name__ == "__main__":
    main()
