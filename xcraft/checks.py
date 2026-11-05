"""The checks of the builds, and what they found, recorded in the lock.

A build is checked against what was resolved for it, and what was parsed from
its spec.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from rich.markup import escape

from xcraft.builds import load_builds, recordable, save_builds
from xcraft.cache import BuildError, cached_build, release_suffix
from xcraft.display import changes, console
from xcraft.graph import reachable
from xcraft.lock import Locked, load_lock
from xcraft.packages import is_debug
from xcraft.project import write_atomically
from xcraft.resolution import Resolved, prepare, resolve, resolve_rpm_deps, rpm_deps_context

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project


def check_resolved(project: Project, name: str) -> Resolved:
    """Return what is resolved for the build of `name`, but raise when its build root cannot be resolved, or has
    upstream packages with the names of ours.

    Those are packages of ours its build should get, but in the builds of the
    packages ours need, and of ours. It happens with requirements xcraft cannot
    see in the specs: files, versions.
    """
    resolved = resolve(project)
    if error := resolved.errors.get(name):
        msg = f"its build root cannot be resolved: {error}"
        raise BuildError(msg)
    owners = {binary.name: package.name for package in project.packages.values() for binary in package.binaries}
    if shadowed := sorted(
        f"{n} (ours from {owners[n]})"
        for n in resolved.buildroots[name]
        if n in owners and n not in resolved.ours[name] and name not in reachable(project.spec_deps, [owners[n]])
    ):
        msg = f"its build root has the upstream {', '.join(shadowed)}: add a BuildRequires on their name to its spec"
        raise BuildError(msg)
    return resolved


class BuildRootChangedError(BuildError):
    """Raised when what a build found in the lock changed its build root: it is built again, in the new order."""


def _check_build_root(project: Project, name: str, installed: dict[str, Locked]) -> None:
    """Raise when dnf installed, in the build root of `name`, other packages than those pinned for it, by name,
    version and architecture, or than ours resolved for it: the pinned ones were checked when downloaded, see
    xcraft.pinned.
    """
    lock, arch, ours = load_lock(project), project.arch, resolve(project).ours[name]
    pins = lock.binaries.get(arch, {})
    # but those of the image ours replace
    upstream = [n for n in [*lock.image[arch], *lock.roots.of(arch).get(name, [])] if n not in ours]
    expected = {f"{n} ({pins[n].evr}.{pins[n].arch})" for n in upstream} | {f"{n} (ours)" for n in ours}
    if differences := changes(
        expected, {f"{n} ({'ours' if p.repo == 'xcraft' else f'{p.evr}.{p.arch}'})" for n, p in installed.items()}
    ):
        msg = f"its build root is not the one pinned for it: {', '.join(differences)}"
        raise BuildError(msg)


def record_dynamic(project: Project, name: str, tree: Path) -> bool:
    """Record in the lock of the builds the dynamic BuildRequires the build of `name` asked for, and return whether they
    changed.
    """
    path = tree / "buildrequires.txt"
    static = set(project.parsed[name].buildrequires)
    lines = path.read_text().splitlines() if path.exists() else []
    dynamic = sorted({r for r in lines if r not in static and not r.startswith("rpmlib(")})
    with project.lock():
        builds = load_builds(project)
        if dynamic == builds.buildrequires.of(project.arch).get(name, []):
            return False
        builds.buildrequires.record(project.arch, project.first, name, dynamic)
        save_builds(project, builds)
    project.refresh()
    prepare(project)  # for the upstream packages they need
    return True


def _check_made(project: Project, name: str, tree: Path, *, suffix: str) -> None:
    """Raise when the build of `name` did not make the binary packages parsed from its spec for the architecture.

    Their names, versions and architectures, and the provides and requirements their spec declares, those rpm
    generates apart, and the debug packages too, which rpm splits while building. The release `suffix`, %autorev, is
    where Package.suffixed() puts it, in their names too, which may include their release.
    """
    package = project.parsed[name]
    made = {n: b for n, b in json.loads((tree / "made.json").read_text()).items() if not is_debug(n)}
    parsed = {n: b for n, b in zip(package.built_names(suffix), package.binaries, strict=True) if not is_debug(n)}
    problems = changes(set(parsed), set(made))
    for n in sorted(made.keys() & parsed.keys()):
        b, m = parsed[n], made[n]
        evr = package.suffixed(b.evr, b.evr, suffix)
        if m["arch"] != b.arch or m["evr"] != evr:
            problems.append(f"{n}-{m['evr']}.{m['arch']} rather than {evr}.{b.arch}")
        problems += [
            f"{n} without the {field} {', '.join(missing)}"
            for field in ("provides", "requires")
            if (missing := sorted({package.suffixed(c, b.evr, suffix) for c in getattr(b, field)} - set(m[field])))
        ]
    if problems:
        msg = (
            f"its build did not make the packages parsed from its spec for {project.arch}: {'; '.join(problems)}: "
            "xcraft parses the spec otherwise than rpmbuild builds it"
        )
        raise BuildError(msg)


def _rpms(build: Path) -> list[Path]:
    """Return the binary packages of a build, but the debug ones."""
    return [p for p in (build / "RPMS").rglob("*.rpm") if not is_debug(p.name.rsplit("-", 2)[0])]


RECORDING = 3
"""The version of how xcraft records what rpm generated for a build, in its rpm-deps.json: changed with it, so that
the builds recorded before are recorded again. 2: the binary packages named with their releases, recorded. 3: the
packages of ours they need, by their names in the specs."""


def _record_new_rpm_deps(
    project: Project, builds: dict[str, Path], supplied: dict[str, str], context: str, *, quiet: bool
) -> None:
    """Keep with the `builds` what rpm generated for their binary packages, in `context`, resolved with the builds of
    ours `supplied`, by package and cache key, but for those which cannot be recorded yet, shown unless `quiet`: they
    are recorded again next time, once what they need is there.
    """
    current = [rpm for n, k in supplied.items() for rpm in _rpms(cached_build(project, n, k))]
    # the binary packages of the builds, by their names as built, with the release suffix, to their names in the specs
    names = {
        name: dict(
            zip(
                (package := project.parsed[name]).built_names(release_suffix(supplied[name])),
                (b.name for b in package.binaries),
                strict=True,
            )
        )
        for name in supplied
    }
    in_specs = {as_built: parsed for binaries in names.values() for as_built, parsed in binaries.items()}
    binaries = {
        name: [b for rpm in _rpms(build) if (b := rpm.name.rsplit("-", 2)[0]) in names[name]]
        for name, build in builds.items()
    }
    result = resolve_rpm_deps(project, {b: names[n][b] for n, bs in binaries.items() for b in bs}, current)
    unbuilt = _unbuilt_providers(project, supplied)
    pending: dict[str, set[str]] = {}  # the packages waiting for builds of ours, with those they wait for
    for name, build in builds.items():
        if failed := sorted(set(binaries[name]) & result.errors.keys()):
            for binary in [] if quiet else failed:
                missing = re.findall(r"nothing provides (\S+) needed by", result.errors[binary])
                if missing and all(m in unbuilt for m in missing):
                    pending.setdefault(name, set()).update(unbuilt[m] for m in missing)
                else:
                    console.print(
                        f"[yellow]cannot record what rpm generated for {binary}[/]: {escape(result.errors[binary])}"
                    )
            continue
        recorded = {
            "version": RECORDING,
            "context": context,
            # the packages of ours they need by their names in the specs too, whatever their builds
            "requires": {
                names[name][b]: [in_specs.get(r, r) for r in result.requires[b]]
                for b in binaries[name]
                if b in result.requires
            },
            "provides": {names[name][b]: result.provides[b] for b in binaries[name] if b in result.provides},
        }
        write_atomically(build / "rpm-deps.json", json.dumps(recorded))
    if pending:
        waiting, waited = sorted(pending), sorted(set().union(*pending.values()))
        console.print(
            f"[yellow]warning[/]: the dependencies rpm generated for {_enumerate(waiting)} are recorded once"
            f" {_enumerate(waited)}, which {'it needs' if len(waiting) == 1 else 'they need'},"
            f" {'is' if len(waited) == 1 else 'are'} built"
        )


def _enumerate(names: list[str]) -> str:
    """Return `names` in a sentence, like ``a, b and c``."""
    return " and ".join(filter(None, [", ".join(names[:-1]), names[-1]]))


def _unbuilt_providers(project: Project, supplied: dict[str, str]) -> dict[str, str]:
    """Return the packages of ours without a build of their current keys, not in `supplied`, by the names and the
    capabilities of their binary packages: as their specs declare them, and as their builds recorded them.
    """
    return {
        capability: name
        for name, package in project.packages.items()
        if name not in supplied
        for binary in package.binaries
        for capability in (binary.name, *(p.split(" ", 1)[0] for p in binary.provides))
    }


def _unsuffixed(project: Project, name: str, value: str, suffix: str) -> str:
    """Return `value`, what rpm generated for a build of `name`, without its release `suffix`, %autorev: at the start of
    %dist, or at the end of the release of the package defining %dist.
    """
    dist = project.parsed[name].dist
    if suffix and dist and f"{suffix}{dist}" in value:
        return value.replace(f"{suffix}{dist}", dist)
    return value.removesuffix(suffix) if suffix else value


def record_rpm_deps(project: Project, names: list[str], keys: dict[str, str], *, quiet: bool = False) -> bool:
    """Record in the lock of the builds what rpm generated for the binary packages of the cached builds of `names`, with
    `keys`, without their release suffixes, nor the provides their specs declare, and return whether it changed.

    What a build generated is kept with it, for the same version of the recording, RECORDING, and what its requirements
    are resolved with, the builds of ours included: see rpm_deps_context(). What cannot be recorded yet is shown,
    unless `quiet`.
    """
    # the builds the requirements may resolve to
    supplied = {n: k for n, k in keys.items() if cached_build(project, n, k).exists()}
    if not (builds := {n: cached_build(project, n, keys[n]) for n in names if n in supplied}):
        return False
    context = rpm_deps_context(project, supplied)

    def found(build: Path) -> dict | None:
        recorded = json.loads(path.read_text()) if (path := build / "rpm-deps.json").exists() else {}
        return recorded if (recorded.get("version"), recorded.get("context")) == (RECORDING, context) else None

    if new := {name: build for name, build in builds.items() if not found(build)}:
        _record_new_rpm_deps(project, new, supplied, context, quiet=quiet)
    # what dry runs do too, without the lock of the project otherwise
    with project.lock():
        lock = load_builds(project)
        before = lock.model_dump()
        for name, build in builds.items():
            recorded = found(build) or {}
            parsed = {b.name: b for b in project.parsed[name].binaries}
            for field in ("requires", "provides"):
                for binary, values in recorded.get(field, {}).items():
                    # as the specs are parsed: the lock does not depend on the numbers of the builds
                    unsuffixed = [_unsuffixed(project, name, value, release_suffix(keys[name])) for value in values]
                    if field == "provides":
                        unsuffixed = recordable(parsed[binary], unsuffixed)
                    getattr(lock, field).record(project.arch, project.first, binary, unsuffixed)
        if lock.model_dump() == before:
            return False
        save_builds(project, lock)
    project.refresh()
    return True


def check_build(project: Project, name: str, tree: Path, *, suffix: str) -> bool:
    """Record the dynamic BuildRequires of the build of `name` in `tree`, check what it installed and made, with its
    release `suffix`, and return whether the lock of the builds changed.
    """
    changed = record_dynamic(project, name, tree)
    installed = json.loads((tree / "buildroot.json").read_text())["installed"]
    try:
        _check_build_root(project, name, {n: Locked.model_validate(p) for n, p in installed.items()})
    except BuildError as e:
        if changed:
            msg = f"{e}, with the dynamic BuildRequires it asked for, now recorded: built again after what they need"
            raise BuildRootChangedError(msg) from None
        raise
    _check_made(project, name, tree, suffix=suffix)
    return changed
