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
- ``versions``, upstream source packages to give the available versions of.

The output has the locked binary ``packages``, the ``image``, the upstream ``buildroots`` and the ``ours`` binary
packages of each build root, the ``requires`` and ``provides`` of the recorded binary packages, the ``errors``, by
package, ``image`` for the image, the ``unlocked`` source packages needed, and the ``versions`` of those and of the
given ones, oldest first.
"""

import functools
import gzip
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

try:
    import dnf  # ty: ignore[unresolved-import]
    import hawkey  # ty: ignore[unresolved-import]
except ImportError:  # out of the build environment, for the tests of the helpers
    pass

OPS = {"<": "LT", "<=": "LE", "=": "EQ", ">=": "GE", ">": "GT"}
OURS = ["xcraft-ours", "@commandline"]
"""The repositories of our packages: written from the specs, and built."""


def canonical(sourcerpm: str, suffixes: list[str]) -> tuple[str, str]:
    """Return the name and the version-release of a source package, without the release `suffixes` of an
    architecture.
    """  # noqa: D205
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


class Resolver:
    """dnf, on the repositories of the architecture of `data`, with the upstream packages of the locked versions only,
    and ours.
    """  # noqa: D205

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
        upstream = base.sack.query().available().filter(reponame__neq=OURS)
        self.names = {p.name for p in upstream}
        self.versions: dict[str, set[str]] = {}
        excluded = []
        for p in upstream:
            source, vr = self.canonical(p.sourcerpm)
            self.versions.setdefault(source, set()).add(vr)
            if source in data["sources"] and vr != data["sources"][source]:
                excluded.append(p)
        base.sack.add_excludes(excluded)
        upstream = base.sack.query().available().filter(reponame__neq=OURS)
        self.binaries = {p.name: p for p in sorted(upstream, key=functools.cmp_to_key(lambda a, b: a.evr_cmp(b)))}
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

    def unmet_weak(self, packages: list) -> list:
        """Return the providers of the weak dependencies of `packages` they do not have, like
        exclude_from_weak_autodetect of dnf: they are not installed as weak dependencies after them.
        """  # noqa: D205
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
        packages: list,
        deps: list[str],
        *,
        weak: bool,
        installed: list | tuple = (),
        exclude_from_weak: list | tuple = (),
    ) -> list:
        """Return the packages to install for `packages`, and the requirements `deps`, on the `installed` ones, which
        those replacing them, by obsoleting them, can leave out, like xcp-ng-release almalinux-release.
        """  # noqa: D205
        base = self.base
        base.reset(goal=True)
        base.conf.install_weak_deps = weak
        base.conf.exclude_from_weak = [str(p) for p in exclude_from_weak]  # which resolve() takes
        for p in installed:
            base.package_install(p, strict=False)
        for p in packages:
            base.package_install(p)
        for dep in deps:
            self.add(dep)
        base.resolve()
        result = list(base.transaction.install_set)
        self.needed.update(self.canonical(p.sourcerpm)[0] for p in result if p.reponame not in OURS)
        return result

    def image(self, errors: dict[str, str]) -> list:
        """Return the packages of the image, like the Containerfile installs them: without weak dependencies, and
        without ours.
        """  # noqa: D205
        ours = self.base.sack.query().filter(reponame=OURS)
        self.base.sack.add_excludes(ours)
        # those of the base image the repositories do not have stay as they are
        wanted = [*self.data["image"], *(n for n in self.data["base"] if n in self.names)]
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

    def buildroot(self, image: list, build: dict, unmet_weak: list) -> list:
        """Return the build root of the `build` of a package of ours, on the `image`, like dnf builddep, with the weak
        dependencies, but the `unmet_weak` ones of the image.
        """  # noqa: D205
        allowed = set(build["allowed"])
        # those of ours replacing the image's, like kernel-headers, in the builds of the packages needing them
        packages = [self.ours[p.name] for p in image if p.name in allowed]
        # only ours of its dependencies, which are built before it
        others = self.base.sack.query().filter(reponame="xcraft-ours").filter(name__neq=list(allowed))
        self.base.sack.add_excludes(others)
        try:
            return self.resolve(
                packages, build["buildrequires"], weak=True, installed=[p for p in image if p.name not in allowed],
                exclude_from_weak=unmet_weak,
            )  # fmt: skip
        finally:
            self.base.sack.remove_excludes(others)

    def generated(self, image: list, name: str, declared: dict) -> tuple[list[str], list[str]]:
        """Return the packages the generated requirements of the built package `name` resolve to, but those of the
        `image`, and its generated capabilities and files the specs require, but those it `declared`.
        """  # noqa: D205
        built = self.built[name]
        installed = self.resolve([built], [], weak=False, installed=image)
        others = self.base.sack.query().filter(pkg=installed).filter(name__neq=[name, *(p.name for p in image)])
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

    def by_version(self, vrs: set[str]) -> list[str]:
        """Return the versions-releases `vrs`, oldest first."""

        def compare(a: str, b: str) -> int:
            return hawkey.split_nevra(f"x-0:{a}.noarch").evr_cmp(hawkey.split_nevra(f"x-0:{b}.noarch"), self.base.sack)

        return sorted(vrs, key=functools.cmp_to_key(compare))


def main() -> None:
    """Resolve the input of the command line into its output."""
    data = json.loads(Path(sys.argv[1]).read_text())
    resolver = Resolver(data)
    result: dict = {"buildroots": {}, "ours": {}, "errors": {}, "requires": {}, "provides": {}}
    image = resolver.image(result["errors"])
    result["image"] = sorted(p.name for p in image)
    unmet_weak = resolver.unmet_weak(image)
    for name, build in data["builds"].items():
        try:
            installed = resolver.buildroot(image, build, unmet_weak)
        except dnf.exceptions.Error as e:
            result["errors"][name] = str(e)
            continue
        result["buildroots"][name] = sorted(p.name for p in installed if p.reponame not in OURS)
        result["ours"][name] = sorted(p.name for p in installed if p.reponame in OURS)
    for name, declared in data["record"].items():
        try:
            result["requires"][name], result["provides"][name] = resolver.generated(image, name, declared)
        except dnf.exceptions.Error as e:
            result["errors"][name] = str(e)
    locked = data["sources"]
    result["packages"] = {
        name: {"evr": p.evr, "arch": p.arch, "source": source, "repo": p.reponame, "sha256": p.chksum[1].hex()}
        for name, p in resolver.binaries.items()
        if (source := resolver.canonical(p.sourcerpm)[0]) in locked
    }
    result["unlocked"] = sorted(resolver.needed - locked.keys())
    result["versions"] = {
        s: resolver.by_version(resolver.versions[s])
        for s in {*result["unlocked"], *data["versions"]}
        if s in resolver.versions
    }
    Path(sys.argv[2]).write_text(json.dumps(result))


if __name__ == "__main__":
    main()
