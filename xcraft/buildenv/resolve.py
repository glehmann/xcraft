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

import dnf  # ty: ignore[unresolved-import]
import hawkey  # ty: ignore[unresolved-import]

data = json.loads(Path(sys.argv[1]).read_text())
OPS = {"<": "LT", "<=": "LE", "=": "EQ", ">=": "GE", ">": "GT"}


def canonical(sourcerpm: str) -> tuple[str, str]:
    """Return the name and the version-release of a source package, without the release suffixes of an architecture."""
    name, version, release = sourcerpm.removesuffix(".src.rpm").rsplit("-", 2)
    vr = f"{version}-{release}"
    for suffix in data["suffixes"]:
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


def write_ours(path: Path) -> None:
    """Write the metadata of a repository of our binary packages, without the packages."""
    packages = []
    for b in data["ours"]:
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
    primary = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<metadata xmlns="http://linux.duke.edu/metadata/common"'
        f' xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="{len(packages)}">{"".join(packages)}</metadata>\n'
    ).encode()
    compressed = gzip.compress(primary, mtime=0)
    (path / "repodata").mkdir(parents=True)
    (path / "repodata/primary.xml.gz").write_bytes(compressed)
    (path / "repodata/repomd.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<repomd xmlns="http://linux.duke.edu/metadata/repo">'
        '<data type="primary">'
        f'<checksum type="sha256">{hashlib.sha256(compressed).hexdigest()}</checksum>'
        f'<open-checksum type="sha256">{hashlib.sha256(primary).hexdigest()}</open-checksum>'
        f'<location href="repodata/primary.xml.gz"/><size>{len(compressed)}</size><open-size>{len(primary)}</open-size>'
        "</data></repomd>\n"
    )


base = dnf.Base()
base.conf.read()
base.conf.installroot = tempfile.mkdtemp()
base.conf.substitutions["arch"] = data["arch"]
base.conf.substitutions["basearch"] = dnf.rpm.basearch(data["arch"])
base.read_all_repos()
ours = Path(tempfile.mkdtemp())
write_ours(ours)
base.repos.add_new_repo("xcraft-ours", base.conf, baseurl=[ours.as_uri()], priority=1, metadata_expire=0)
base.fill_sack(load_system_repo=False)
built = {p.name: p for p in base.add_remote_rpms(data["rpms"])}
ours_repos = ["xcraft-ours", "@commandline"]
upstream = base.sack.query().available().filter(reponame__neq=ours_repos)
names = {p.name for p in upstream}
locked = data["sources"]
versions: dict[str, set[str]] = {}
excluded = []
for p in upstream:
    source, vr = canonical(p.sourcerpm)
    versions.setdefault(source, set()).add(vr)
    if source in locked and vr != locked[source]:
        excluded.append(p)
base.sack.add_excludes(excluded)
upstream = base.sack.query().available().filter(reponame__neq=ours_repos)
binaries = {p.name: p for p in sorted(upstream, key=functools.cmp_to_key(lambda a, b: a.evr_cmp(b)))}
ours_by_name = {p.name: p for p in base.sack.query().available().filter(reponame="xcraft-ours")}
needed: set[str] = set()


def add(dep: str) -> None:
    """Add a requirement to the goal, like dnf builddep: by provides, or else by file."""
    selector = dnf.selector.Selector(base.sack)
    selector.set(provides=dep)
    if not selector.matches() and dep.startswith("/"):
        selector = dnf.selector.Selector(base.sack)
        selector.set(file=dep)
    if not selector.matches() and not dep.startswith("("):
        msg = f"nothing provides {dep}: when a package of ours does, build it first"
        raise dnf.exceptions.Error(msg)
    base._goal.install(select=selector)  # noqa: SLF001, like dnf builddep


def unmet_weak(packages: list) -> list:
    """Return the providers of the weak dependencies of `packages` they do not have, like exclude_from_weak_autodetect
    of dnf: they are not installed as weak dependencies after them.
    """  # noqa: D205
    names = {p.name for p in packages}
    available = base.sack.query().available()
    providers = [p for p in available.filter(supplements=packages) if p.name not in names]
    for r in {r for p in packages for r in p.recommends}:
        found = available.filter(provides=r)
        if not any(p.name in names for p in found):
            providers.extend(found)
    return providers


def resolve(
    packages: list, deps: list[str], *, weak: bool, installed: list | tuple = (), exclude_from_weak: list | tuple = ()
) -> list:
    """Return the packages to install for `packages`, and the requirements `deps`, on the `installed` ones, which those
    replacing them, by obsoleting them, can leave out, like xcp-ng-release almalinux-release.
    """  # noqa: D205
    base.reset(goal=True)
    base.conf.install_weak_deps = weak
    base.conf.exclude_from_weak = [str(p) for p in exclude_from_weak]  # which resolve() takes
    for p in installed:
        base.package_install(p, strict=False)
    for p in packages:
        base.package_install(p)
    for dep in deps:
        add(dep)
    base.resolve()
    installed = list(base.transaction.install_set)
    needed.update(canonical(p.sourcerpm)[0] for p in installed if p.reponame not in ours_repos)
    return installed


result = {"image": [], "buildroots": {}, "ours": {}, "errors": {}}
image = []
ours_packages = base.sack.query().filter(reponame=ours_repos)
base.sack.add_excludes(ours_packages)
# those of the base image the repositories do not have stay as they are
wanted = [*data["image"], *(n for n in data["base"] if n in names)]
if missing := sorted({n for n in wanted if n not in binaries}):
    result["errors"]["image"] = f"not in the repositories, in the locked version: {', '.join(missing)}"
else:
    try:
        # like the Containerfile, without weak dependencies, and without ours
        image = resolve([binaries[n] for n in wanted], [], weak=False)
        result["image"] = sorted(p.name for p in image)
    except dnf.exceptions.Error as e:
        result["errors"]["image"] = str(e)
base.sack.remove_excludes(ours_packages)
image_weak = unmet_weak(image)
for name, build in data["builds"].items():
    allowed = set(build["allowed"])
    # those of ours replacing the image's, like kernel-headers, in the builds of the packages needing them
    packages = [ours_by_name[p.name] for p in image if p.name in allowed]
    # only ours of its dependencies, which are built before it
    others = base.sack.query().filter(reponame="xcraft-ours").filter(name__neq=list(allowed))
    base.sack.add_excludes(others)
    try:
        # like dnf builddep, with the weak dependencies
        installed = resolve(
            packages, build["buildrequires"], weak=True, installed=[p for p in image if p.name not in allowed],
            exclude_from_weak=image_weak,
        )  # fmt: skip
    except dnf.exceptions.Error as e:
        result["errors"][name] = str(e)
        continue
    finally:
        base.sack.remove_excludes(others)
    result["buildroots"][name] = sorted(p.name for p in installed if p.reponame not in ours_repos)
    result["ours"][name] = sorted(p.name for p in installed if p.reponame in ours_repos)

requires, provides = {}, {}
for name, declared in data["record"].items():
    try:
        installed = resolve([built[name]], [], weak=False, installed=image)
    except dnf.exceptions.Error as e:
        result["errors"][name] = str(e)
        continue
    others = base.sack.query().filter(pkg=installed).filter(name__neq=[name, *result["image"]])
    generated = [r for r in built[name].requires if str(r) not in declared["requires"]]
    requires[name] = sorted(
        {p.name for r in generated for p in others.filter(provides=r) or others.filter(file=str(r))}
    )
    # and the files, which the metadata of ours, from the specs, does not have
    provides[name] = sorted(
        {str(p) for p in built[name].provides if str(p).split(" ", 1)[0] in data["wanted"]} - set(declared["provides"])
        | set(data["wanted"]) & set(built[name].files)
    )
result["requires"], result["provides"] = requires, provides


def by_version(vrs: set[str]) -> list[str]:
    """Return the versions-releases `vrs`, oldest first."""
    return sorted(
        vrs,
        key=functools.cmp_to_key(
            lambda a, b: hawkey.split_nevra(f"x-0:{a}.noarch").evr_cmp(hawkey.split_nevra(f"x-0:{b}.noarch"), base.sack)
        ),
    )


result["packages"] = {
    name: {"evr": p.evr, "arch": p.arch, "source": canonical(p.sourcerpm)[0], "repo": p.reponame,
           "sha256": p.chksum[1].hex()}
    for name, p in binaries.items()
    if canonical(p.sourcerpm)[0] in locked
}  # fmt: skip
result["unlocked"] = sorted(needed - locked.keys())
result["versions"] = {s: by_version(versions[s]) for s in {*result["unlocked"], *data["versions"]} if s in versions}
Path(sys.argv[2]).write_text(json.dumps(result))
