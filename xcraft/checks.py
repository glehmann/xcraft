"""The checks of the builds, and what they found, recorded in the lock.

A build is checked against what was resolved for it, and what was parsed from
its spec.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from rich.markup import escape

from xcraft.cache import BuildError, cached_build
from xcraft.container import BUILDENV
from xcraft.display import changes, console
from xcraft.lock import Locked, load_lock, save_lock
from xcraft.project import write_atomically
from xcraft.resolution import Resolved, prepare, resolve, resolve_rpm_deps
from xcraft.specs import is_debug

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project


def check_resolved(project: Project, name: str) -> Resolved:
    """Return what is resolved for the build of `name`, but raise when its build root cannot be resolved, or has
    upstream packages with the names of ours.

    Those are packages of ours its build should get, but for those of the image
    in the builds of the packages ours need. It happens with requirements xcraft
    cannot see in the specs: files, versions.
    """  # noqa: D205
    resolved = resolve(project)
    if error := resolved.errors.get(name):
        msg = f"its build root cannot be resolved: {error}"
        raise BuildError(msg)
    owners = {binary.name: package.name for package in project.packages.values() for binary in package.binaries}
    image, deps = set(resolved.image), project.deps[name]
    if shadowed := sorted(
        f"{n} (ours from {owners[n]})"
        for n in resolved.buildroots[name]
        if n in owners and (n not in image or owners[n] in deps)
    ):
        msg = f"its build root has the upstream {', '.join(shadowed)}: add a BuildRequires on their name to its spec"
        raise BuildError(msg)
    return resolved


class BuildRootChangedError(BuildError):
    """Raised when what a build found in the lock changed its build root: it is built again, in the new order."""


def _check_build_root(project: Project, name: str, installed: dict[str, Locked]) -> None:
    """Raise when dnf installed, in the build root of `name`, other packages than those resolved for it."""
    resolved = resolve(project)
    expected = {f"{n} ({resolved.packages[n].evr})" for n in resolved.buildroots[name]}
    expected |= {f"{n} (ours)" for n in resolved.ours[name]}
    if differences := changes(
        expected, {f"{n} ({'ours' if p.repo == 'xcraft' else p.evr})" for n, p in installed.items()}
    ):
        msg = f"its build root is not the one resolved for it: {', '.join(differences)}"
        raise BuildError(msg)


def record_dynamic(project: Project, name: str, tree: Path) -> bool:
    """Record in the lock the dynamic BuildRequires the build of `name` asked for, and return whether they changed."""
    path = tree / "buildrequires.txt"
    static = set(project.parsed[name].buildrequires)
    lines = path.read_text().splitlines() if path.exists() else []
    dynamic = sorted({r for r in lines if r not in static and not r.startswith("rpmlib(")})
    lock = load_lock(project)
    if dynamic == lock.buildrequires.of(project.arch).get(name, []):
        return False
    lock.buildrequires.record(project.arch, project.first, name, dynamic)
    save_lock(project, lock)
    project.refresh()
    prepare(project)  # for the upstream packages they need
    return True


def _check_made(project: Project, name: str, tree: Path, *, suffix: str) -> None:
    """Raise when the build of `name` did not make the binary packages parsed from its spec for the architecture.

    Their names, versions and architectures, and the provides and requirements their spec declares, those rpm
    generates apart, and the debug packages too, which rpm splits while building. The release `suffix`, %autorev, is
    at the start of %dist, wherever the spec has it, or at the end of the release of the package defining %dist.
    """
    dist = project.parsed[name].dist

    def suffixed(text: str) -> str:
        return text.replace(dist, f"{suffix}{dist}") if dist else text

    made = {n: b for n, b in json.loads((tree / "made.json").read_text()).items() if not is_debug(n)}
    parsed = {b.name: b for b in project.parsed[name].binaries if not is_debug(b.name)}
    problems = changes(set(parsed), set(made))
    for n in sorted(made.keys() & parsed.keys()):
        b, m = parsed[n], made[n]
        evr = suffixed(b.evr) if dist and dist in b.evr else f"{b.evr}{suffix}"
        if m["arch"] != b.arch or m["evr"] != evr:
            problems.append(f"{n}-{m['evr']}.{m['arch']} rather than {evr}.{b.arch}")
        problems += [
            f"{n} without the {field} {', '.join(missing)}"
            for field in ("provides", "requires")
            if (missing := sorted({suffixed(c) for c in getattr(b, field)} - set(m[field])))
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


def _record_new_rpm_deps(
    project: Project, builds: dict[str, Path], keys: dict[str, str], context: str, *, quiet: bool
) -> None:
    """Keep with the `builds` what rpm generated for their binary packages, in `context`, but for those which cannot
    be recorded yet, shown unless `quiet`: they are recorded again next time, once what they need is there.
    """  # noqa: D205
    current = [rpm for n, k in keys.items() if (build := cached_build(project, n, k)).exists() for rpm in _rpms(build)]
    binaries = {name: [rpm.name.rsplit("-", 2)[0] for rpm in _rpms(build)] for name, build in builds.items()}
    parsed = {b.name for p in project.parsed.values() for b in p.binaries}
    result = resolve_rpm_deps(project, sorted(b for bs in binaries.values() for b in bs if b in parsed), current)
    for name, build in builds.items():
        if failed := sorted(set(binaries[name]) & result.errors.keys()):
            for binary in [] if quiet else failed:
                console.print(
                    f"[yellow]cannot record what rpm generated for {binary}[/]: {escape(result.errors[binary])}"
                )
            continue
        recorded = {
            "context": context,
            "requires": {b: result.requires[b] for b in binaries[name] if b in result.requires},
            "provides": {b: result.provides[b] for b in binaries[name] if b in result.provides},
        }
        write_atomically(build / "rpm-deps.json", json.dumps(recorded))


def record_rpm_deps(project: Project, names: list[str], keys: dict[str, str], *, quiet: bool = False) -> bool:
    """Record in the lock what rpm generated for the binary packages of the cached builds of `names`, with `keys`,
    and return whether the lock changed.

    What a build generated is kept with it, for the same locked versions and requirements of our specs. What cannot
    be recorded yet is shown, unless `quiet`.
    """  # noqa: D205
    if not (builds := {n: build for n in names if (build := cached_build(project, n, keys[n])).exists()}):
        return False
    lock = load_lock(project)
    wanted = sorted({r for p in project.parsed.values() for r in p.requirements})
    context = hashlib.sha256(
        json.dumps([(BUILDENV / "resolve.py").read_text(), lock.sources, wanted]).encode()
    ).hexdigest()

    def found(build: Path) -> dict | None:
        recorded = json.loads(path.read_text()) if (path := build / "rpm-deps.json").exists() else {}
        return recorded if recorded.get("context") == context else None

    if new := {name: build for name, build in builds.items() if not found(build)}:
        _record_new_rpm_deps(project, new, keys, context, quiet=quiet)
    # what dry runs do too, without the lock of the project otherwise
    with project.lock():
        lock = load_lock(project)
        before = lock.model_dump()
        for build in builds.values():
            recorded = found(build) or {}
            for field in ("requires", "provides"):
                for binary, values in recorded.get(field, {}).items():
                    getattr(lock, field).record(project.arch, project.first, binary, values)
        if lock.model_dump() == before:
            return False
        save_lock(project, lock)
    project.refresh()
    return True


def check_build(project: Project, name: str, tree: Path, *, suffix: str) -> bool:
    """Record the dynamic BuildRequires of the build of `name` in `tree`, check what it installed and made, with its
    release `suffix`, and return whether the lock changed.
    """  # noqa: D205
    changed = record_dynamic(project, name, tree)
    installed = json.loads((tree / "buildroot.json").read_text())["installed"]
    try:
        _check_build_root(project, name, {n: Locked.model_validate(p) for n, p in installed.items()})
    except BuildError as e:
        if changed:
            msg = f"{e}, with the dynamic BuildRequires it asked for, now in the lock: built again after what they need"
            raise BuildRootChangedError(msg) from None
        raise
    _check_made(project, name, tree, suffix=suffix)
    return changed
