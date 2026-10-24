"""Building packages in the build environment, with a cache of the results.

The result of a build is cached under a key covering everything that goes into
it: the package directory (spec, patches, ``sources`` manifest), the keys of
the packages it is built against, the build environment image and platform,
and the extra upstream RPMs.

The builds run against a local repository holding the cached results matching
the current keys, with a priority taking over the build environment's
repositories. Packages not built locally come from those repositories.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.text import Text

from xcraft import ccache, remote
from xcraft.container import BUILDENV, DNF_LOCK, ContainerError, build_image, image_tag, run, run_container
from xcraft.display import (
    activity,
    console,
    diagnostics,
    duration,
    log_step,
    print_log_line,
    read_tail,
)
from xcraft.graph import build_order, components, reachable
from xcraft.localrepo import REPO_FILE, LocalRepo, link, repo_id
from xcraft.lock import Locked, load_lock, lock_path, save_lock
from xcraft.project import write_atomically
from xcraft.resolution import Resolved, prepare, record, resolve
from xcraft.signing import dev_signer
from xcraft.sources import fetch_sources, read_manifest
from xcraft.specs import hash_dir, main_spec

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from xcraft.project import Project

RECIPE = hashlib.sha256(
    "".join(
        [
            REPO_FILE,
            *((BUILDENV / s).read_text() for s in ("build.sh", "offline.py", "record.py", "made.py", "rpmdeps.py")),
        ]
    ).encode()
).hexdigest()
"""What makes the builds, besides the image and the packages: a change to it rebuilds them all, but see _SAME_BUILDS."""
_SAME_BUILDS = {
    # offline.py: an EOF when a process dies first
    "520edd959d64e226fcbb86a704bf7dbb75e57fa262ef70da5319d4e31f3ec1bc":
        "6e4c3d480ddebb5ab8113785a8089d733175e35d369b7c50c1cea01fb65140bc",
    # record.py: without the latest versions, which another script looks for
    "9696604d19c16fc9fdebc6431bacf28b50585465cfb3148b8e041c4ff8153778":
        "520edd959d64e226fcbb86a704bf7dbb75e57fa262ef70da5319d4e31f3ec1bc",
    # build.sh and record.py: the providers of the dynamic BuildRequires too
    "e4d86b87110eac1922b1127fa00c796d08b757136324c97223ea31f34236d20b":
        "9696604d19c16fc9fdebc6431bacf28b50585465cfb3148b8e041c4ff8153778",
    # build.sh, record.py and made.py: the dynamic BuildRequires before installing them, and the packages made
    "91b1c1f54f53be472170ccaf7f1001959e51a8206718056bb0020a4d383690c5":
        "e4d86b87110eac1922b1127fa00c796d08b757136324c97223ea31f34236d20b",
    # made.py and record.py: entry points, and the dependencies of rpm headers in rpmdeps.py
    "cd1db99022f6382f7e7363906e76061130bd36e1aa129d164e8917f48cb9ea3a":
        "91b1c1f54f53be472170ccaf7f1001959e51a8206718056bb0020a4d383690c5",
}  # fmt: skip
"""Recipes making the same packages as an earlier one, by their hash: their builds keep the keys of the earlier one."""


def recipe_key() -> str:
    """Return the recipe in the build keys: the earliest one making the same packages."""
    recipe = RECIPE
    while recipe in _SAME_BUILDS:
        recipe = _SAME_BUILDS[recipe]
    return recipe


class BuildError(RuntimeError):
    """Raised when a package cannot be built."""


def next_test_suffixes(project: Project, names: list[str], kind: str, ident: str) -> dict[str, str]:
    """Return the release suffixes of the next test or pre builds of `names`, numbered by `ident` and release.

    A test build is above its release and below the next one, and a pre build
    below its release. The numbers are kept with the project's generated files,
    across `clean`.
    """
    path = project.state / "test-builds.json"
    counters: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
    suffixes = {}
    for name in names:
        package = project.packages[name]
        release = package.binaries[0].evr.removesuffix(package.dist) if package.binaries else ""
        counter = f"{name} {release} {kind} {ident}"
        counters[counter] = counters.get(counter, 0) + 1
        suffixes[name] = f".0.{ident}.{counters[counter]}" if kind == "test" else f"~{ident}.{counters[counter]}"
    path.parent.mkdir(exist_ok=True)
    write_atomically(path, json.dumps(counters, indent=1, sort_keys=True) + "\n")
    return suffixes


def inputs(project: Project) -> dict[str, str]:
    """Return the hash of the directory of each package, its input in the keys."""
    return {name: hash_dir(path) for name, path in project.package_dirs.items()}


def build_keys(
    project: Project, autorev: dict[str, str] | None = None, hashes: dict[str, str] | None = None
) -> dict[str, str]:
    """Return the cache key of each package: a SHA-256 of all that makes its build.

    That is its package directory, the build environment image and recipe,
    the versions of the upstream packages of its build root, resolved, besides
    the image's ones, which the image id covers, the `autorev` of the test
    builds, and the keys of its dependencies. Packages depending on each other
    get the key of them all. The `hashes` of the package directories are those
    of inputs(), computed now by default. The lock must be prepared: see
    resolution.prepare().
    """
    resolved, deps = resolve(project), project.deps
    hashes = hashes or inputs(project)
    image = set(resolved.image)
    base = json.dumps([image_tag(project), project.platform, project.config.extra_upstream, recipe_key()])

    def package(name: str) -> str:
        buildroot = [
            f"{upstream}={resolved.packages[upstream].evr}"
            for upstream in resolved.buildroots.get(name, [])
            if upstream not in image
        ]
        return json.dumps([name, hashes[name], (autorev or {}).get(name), buildroot])

    keys: dict[str, str] = {}
    for component in components(deps):  # the dependencies first
        h = hashlib.sha256(base.encode())
        for name in sorted(component):
            h.update(package(name).encode())
        for dep in sorted({dep for name in component for dep in deps[name]} - component):
            h.update(keys[dep].encode())
        keys |= {name: hashlib.sha256(f"{h.hexdigest()}\0{name}".encode()).hexdigest() for name in component}
    return keys


def cached_build(project: Project, name: str, key: str) -> Path:
    """Return where the build of `name` with `key` is cached."""
    return project.state / "cache/builds" / project.arch / name / key


def has_normal_build(project: Project, name: str) -> bool:
    """Return whether `name` has a cached build, of any version, besides the test builds."""
    return any(
        not (build.name.startswith(".") or (build / "autorev").exists())  # not a build in progress, or a test one
        for build in cached_build(project, name, "").glob("*")
    )


_TEST_SUFFIX = re.compile(r"^(?:\.0\.(?P<test>[a-zA-Z0-9]+)|~(?P<pre>[a-zA-Z0-9]+))\.(?P<number>\d+)$")


@dataclass
class TestBuild:
    """A cached test or pre build of a package."""

    kind: str
    ident: str
    number: int
    built: float
    """When it was built, as a timestamp."""
    current: bool
    """Whether it is built from the current spec and dependencies."""


def test_builds(project: Project, name: str, hashes: dict[str, str]) -> list[TestBuild]:
    """Return the latest cached test or pre build of `name` for each ID, the newest first, current or not with the
    `hashes` of the package directories (see inputs()).
    """  # noqa: D205
    latest: dict[tuple[str, str], tuple[TestBuild, Path]] = {}
    for marker in cached_build(project, name, "").glob("[!.]*/autorev"):
        if not (m := _TEST_SUFFIX.match(json.loads(marker.read_text())[name])):
            continue
        kind = "test" if m["test"] else "pre"
        built = (marker.parent / "build.log").stat().st_mtime
        # the numbers start again with each release
        if (kind, m[kind]) not in latest or built > latest[kind, m[kind]][0].built:
            latest[kind, m[kind]] = TestBuild(kind, m[kind], int(m["number"]), built, current=False), marker
    for build, marker in latest.values():
        build.current = build_keys(project, json.loads(marker.read_text()), hashes)[name] == marker.parent.name
    return sorted((build for build, _ in latest.values()), key=lambda build: -build.built)


def builds_repo(project: Project, keys: dict[str, str], names: Iterable[str] | None = None) -> LocalRepo:
    """Return the repository the builds run against, with the cached builds matching `keys`, of `names` only, or of
    all the packages.
    """  # noqa: D205
    repo = LocalRepo(project, project.state / "repo")
    repo.reset()
    for name in keys if names is None else names:
        if cached_build(project, name, keys[name]).exists():
            repo.add(cached_build(project, name, keys[name]))
    return repo


def deps_repo(project: Project, name: str, keys: dict[str, str]) -> LocalRepo:
    """Return the repository the build of `name` runs against: the builds of its dependencies, with their `keys`,
    which its build root can have.
    """  # noqa: D205
    return builds_repo(project, keys, reachable(project.deps, project.deps[name]))


def build_path(project: Project, name: str, key: str) -> str:
    """Return the path of the tree of a build in the remote storage."""
    return f"builds/{project.arch}/{name}/{key}"


BUILD_FILES = ["RPMS", "SRPMS", "build.log"]
"""What a build is made of, in the cache and in the remote storage."""


def prepare_tree(project: Project, name: str) -> Path:
    """Return a fresh build tree of `name`, laid out as a packaging repository.

    The main spec is in SPECS/, everything else in SOURCES/, with the archives
    from the source cache, downloaded when missing (see fetch_sources).
    """
    pkg_dir = project.package_dirs[name]
    spec = main_spec(pkg_dir)
    tree = project.state / "build" / name
    shutil.rmtree(tree, ignore_errors=True)
    (tree / "SPECS").mkdir(parents=True)
    shutil.copy2(spec, tree / "SPECS")
    # `sources` and `origin` are xcraft's
    shutil.copytree(
        pkg_dir, tree / "SOURCES", ignore=lambda d, _: [spec.name, "sources", "origin"] if d == str(pkg_dir) else []
    )
    if missing := fetch_sources(project, name):
        msg = f"{', '.join(missing)} is not in the source cache, the remote storage, nor at a URL of the spec"
        raise BuildError(msg)
    if (pkg_dir / "sources").exists():
        for filename, digest in read_manifest(pkg_dir / "sources").items():
            archive = project.source_cache / digest / filename
            # read-only: the build tree has it hard-linked, and could change it
            archive.chmod(0o444)
            link(archive, tree / "SOURCES" / filename)
    return tree


def _run_build(  # noqa: PLR0913
    project: Project,
    name: str,
    repo: LocalRepo,
    rpmbuild_stage: str,
    *,
    replace: list[str],
    suffix: str = "",
    after: str = "",
    log: Path | None = None,
    on_line: Callable[[str], None] | None = None,
) -> None:
    """Run rpmbuild `rpmbuild_stage` (a: all, p: prep...) in the build tree of `name`, then the `after` commands.

    The upstream packages of the image named in `replace` are replaced first by
    ours. `suffix` is added to the release of a test build, as %autorev. The output goes to
    `log`, or to an interactive terminal without log.
    """
    parameters = {
        "spec": main_spec(project.package_dirs[name]).name,
        "stage": rpmbuild_stage,
        "repo": repo_id(project),
        "lock": DNF_LOCK,
        "autorev": suffix,
        "replace": " ".join(replace),
    }
    script = "".join(f"{p}={shlex.quote(value)}\n" for p, value in parameters.items()) + f". /xcraft/build.sh\n{after}"
    if size := project.config.ccache_size.get(name):
        # not in the recipe of the builds: it does not change them
        script = f"export CCACHE_MAXSIZE={shlex.quote(size)}\n{script}"
    cache = ccache.local_path(project, name)
    cache.mkdir(parents=True, exist_ok=True)
    tree = project.state / "build" / name
    volumes = [f"{tree}:/build", f"{cache}:/ccache", *repo.volumes()]
    run_container(project, script, container=f"xcraft-{project.id}-{name}", volumes=volumes, log=log, on_line=on_line)


@dataclass
class Result:
    """Outcome of a package in a build."""

    name: str
    status: str  # cached, fetched, built, failed, skipped, or again, to build again

    @property
    def failed(self) -> bool:
        """Whether it is not built: failed, skipped, or still to build again."""
        return self.status in ("failed", "skipped", "again")


def previous_duration(project: Project, name: str) -> float | None:
    """Return how long the last cached build of `name` took, if any."""
    builds = sorted(cached_build(project, name, "").glob("*/build.log"), key=lambda log: log.stat().st_mtime)
    for log in reversed(builds):
        # the build environment runs the build under `time`
        if times := re.findall(r"^real\s+(\d+)m([\d.]+)s$", log.read_text(errors="replace"), re.MULTILINE):
            minutes, seconds = times[-1]
            return int(minutes) * 60 + float(seconds)
    return None


def _estimate(project: Project, name: str) -> str:
    previous = previous_duration(project, name)
    return f" [dim](~{duration(previous)})[/]" if previous else ""


def _pid_file(project: Project, name: str) -> Path:
    return project.state / "build" / f"{name}.pid"


def _alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def building(project: Project, name: str) -> bool:
    """Return whether `name` is being built."""
    try:
        return _alive(_pid_file(project, name).read_text())
    except OSError:
        return False


def _queue_file(project: Project) -> Path:
    return project.state / "build" / ".queue"  # not a package name


def queued(project: Project) -> list[str]:
    """Return the packages a running build is still to build, after the current one, in order."""
    try:
        pid, *names = _queue_file(project).read_text().split()
    except (OSError, ValueError):
        return []
    return names if _alive(pid) else []


def _image_replacements(resolved: Resolved, name: str) -> list[str]:
    """Return the upstream packages of the image to replace by ours in the `resolved` build root of `name`."""
    return sorted(set(resolved.ours.get(name, [])) & set(resolved.image))


def _check_resolved(project: Project, name: str) -> Resolved:
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
    expected = {n: resolved.packages[n].evr for n in resolved.buildroots[name]}
    expected |= dict.fromkeys(resolved.ours[name], "ours")
    got = {n: "ours" if p.repo == "xcraft" else p.evr for n, p in installed.items()}
    if differences := sorted(
        [*(f"+{n} ({got[n]})" for n in got if got[n] != expected.get(n)),
         *(f"-{n} ({expected[n]})" for n in expected if expected[n] != got.get(n))]
    ):  # fmt: skip
        msg = f"its build root is not the one resolved for it: {', '.join(differences)}"
        raise BuildError(msg)


def _record_dynamic(project: Project, name: str, tree: Path) -> bool:
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


def _check_made(project: Project, name: str, tree: Path, *, test: bool) -> None:
    """Raise when the build of `name` did not make the binary packages parsed from its spec for the architecture.

    Their names, versions and architectures, and the provides and requirements their spec declares, those rpm
    generates apart, and the debug packages too, which rpm splits while building. A test build has its own releases.
    """

    def debug(n: str) -> bool:
        return n.endswith(("-debuginfo", "-debugsource"))

    made = {n: b for n, b in json.loads((tree / "made.json").read_text()).items() if not debug(n)}
    parsed = {b.name: b for b in project.parsed[name].binaries if not debug(b.name)}
    problems = [
        *(f"+{n}" for n in sorted(made.keys() - parsed.keys())),
        *(f"-{n}" for n in sorted(parsed.keys() - made.keys())),
    ]
    for n in sorted(made.keys() & parsed.keys()):
        b, m = parsed[n], made[n]
        if (m["arch"], m["evr"] if not test else "") != (b.arch, b.evr if not test else ""):
            problems.append(f"{n}-{m['evr']}.{m['arch']} rather than {b.evr}.{b.arch}")
        problems += [
            f"{n} without the {field} {', '.join(missing)}"
            for field in (("provides", "requires") if not test else ())
            if (missing := sorted(set(getattr(b, field)) - set(m[field])))
        ]
    if problems:
        msg = (
            f"its build did not make the packages parsed from its spec for {project.arch}: {'; '.join(problems)}: "
            "xcraft parses the spec otherwise than rpmbuild builds it"
        )
        raise BuildError(msg)


def _rpms(build: Path) -> list[Path]:
    """Return the binary packages of a build, but the debug ones."""
    return [p for p in (build / "RPMS").rglob("*.rpm") if not re.search(r"-debug(info|source)-", p.name)]


def record_built(project: Project, names: list[str], keys: dict[str, str]) -> bool:
    """Record in the lock what rpm generated for the binary packages of the cached builds of `names`, with `keys`,
    and return whether the lock changed.

    What a build generated is kept with it, for the same locked versions and requirements of our specs.
    """  # noqa: D205
    lock = load_lock(project)
    wanted = sorted({r for p in project.parsed.values() for r in p.requirements})
    context = hashlib.sha256(
        json.dumps([(BUILDENV / "resolve.py").read_text(), lock.sources, wanted]).encode()
    ).hexdigest()
    builds = {name: cached_build(project, name, keys[name]) for name in names}

    def found(build: Path) -> dict | None:
        recorded = json.loads(path.read_text()) if (path := build / "recorded.json").exists() else {}
        return recorded if recorded.get("context") == context else None

    if new := [name for name, build in builds.items() if not found(build)]:
        current = [
            rpm for n, k in keys.items() if (build := cached_build(project, n, k)).exists() for rpm in _rpms(build)
        ]
        binaries = {name: [rpm.name.rsplit("-", 2)[0] for rpm in _rpms(builds[name])] for name in new}
        parsed = {b.name for p in project.parsed.values() for b in p.binaries}
        result = record(project, sorted(b for bs in binaries.values() for b in bs if b in parsed), current)
        for name in new:
            # not kept: recorded again next time, once what they need is there
            if failed := sorted(set(binaries[name]) & result.errors.keys()):
                for binary in failed:
                    console.print(
                        f"[yellow]cannot record what rpm generated for {binary}[/]: {escape(result.errors[binary])}"
                    )
                continue
            recorded = {
                "context": context,
                "requires": {b: result.requires[b] for b in binaries[name] if b in result.requires},
                "provides": {b: result.provides[b] for b in binaries[name] if b in result.provides},
            }
            write_atomically(builds[name] / "recorded.json", json.dumps(recorded))
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


def _check_build(project: Project, name: str, tree: Path, *, test: bool) -> bool:
    """Record the dynamic BuildRequires of the build of `name` in `tree`, check what it installed and made, and return
    whether the lock changed.
    """  # noqa: D205
    changed = _record_dynamic(project, name, tree)
    installed = json.loads((tree / "buildroot.json").read_text())["installed"]
    try:
        _check_build_root(project, name, {n: Locked.model_validate(p) for n, p in installed.items()})
    except BuildError as e:
        if changed:
            msg = f"{e}, with the dynamic BuildRequires it asked for, now in the lock: built again after what they need"
            raise BuildRootChangedError(msg) from None
        raise
    _check_made(project, name, tree, test=test)
    return changed


def _build_one(  # noqa: PLR0913
    project: Project,
    name: str,
    keys: dict[str, str],
    hashes: dict[str, str],
    *,
    status: str | None,
    autorev: dict[str, str],
) -> None:
    """Build `name` and cache the result, showing `status` while it runs, or the build log without status.

    It only gets the builds of its dependencies. What its build found is
    recorded in the lock: its dynamic BuildRequires, even when it fails, and
    what rpm generated for its binary packages. It is not cached when its spec
    directory changed during the build, when its build root is not the one
    resolved for it (see _check_build_root), or when it did not make the
    packages parsed from its spec (see _check_made). Raise
    BuildRootChangedError when it found what changes its build root, to build
    it again, after the packages it needs. The `keys` are those of the
    `hashes` of the package directories at the start of the builds, computed
    again with them when the build changes the lock.
    """
    if error := project.packages[name].error:
        raise BuildError(error)
    resolved = _check_resolved(project, name)
    staged = hash_dir(project.package_dirs[name])
    replace = _image_replacements(resolved, name)
    repo = deps_repo(project, name, keys)
    tree = prepare_tree(project, name)
    log = project.log(name)
    pid_file = _pid_file(project, name)
    pid_file.write_text(str(os.getpid()))
    try:
        with activity(status, expected=previous_duration(project, name)) if status else nullcontext(None) as progress:
            # the errors and warnings above the progress, like with the whole log, after the status
            diagnose = diagnostics(print_log_line, header=Text.from_markup(status).plain) if status else print_log_line

            def on_line(line: str) -> None:
                if progress:
                    progress(line)
                diagnose(line)

            _run_build(
                project, name, repo, "a", replace=replace, suffix=autorev.get(name, ""), log=log, on_line=on_line
            )
    except ContainerError as e:
        # those it asked for before failing, maybe for one of ours not among its dependencies
        if (tree / "buildrequires.txt").exists() and _record_dynamic(project, name, tree):
            msg = f"{e}, asking for other dynamic BuildRequires, now in the lock: built again after what they need"
            raise BuildRootChangedError(msg) from None
        raise
    finally:
        pid_file.unlink()
    if _check_build(project, name, tree, test=name in autorev):
        keys = build_keys(project, autorev, hashes)  # covering what the build recorded
    if hash_dir(project.package_dirs[name]) != staged:
        msg = f"{project.package_dirs[name].relative_to(project.root)} changed during the build, build it again"
        raise BuildError(msg)
    # test builds are signed with the developer's key, if any
    if name in autorev and (signer := dev_signer(project)):
        for output in ("RPMS", "SRPMS"):
            signer.sign_rpms(project, tree / output)
    _cache(project, name, keys[name], tree, autorev)
    record_built(project, [name], keys)


def _cache(project: Project, name: str, key: str, tree: Path, autorev: dict[str, str]) -> Path:
    """Move the build of `name` in `tree` to the cache, with its `key` and the test build suffixes `autorev`, and
    return it there.
    """  # noqa: D205
    build_dir = cached_build(project, name, key)
    # assembled next to it, and renamed, not to leave a partial build in the cache
    new = build_dir.with_name(f".{build_dir.name}")
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    for output in ("RPMS", "SRPMS"):
        (tree / output).rename(new / output)
    if name in autorev:
        # the suffixes of all the test builds of the run, its own and those of the dependencies built with it
        (new / "autorev").write_text(json.dumps(autorev, sort_keys=True))
    shutil.copy2(project.log(name), new / "build.log")
    if build_dir.exists():  # an earlier build of the same inputs
        shutil.rmtree(new)
    else:
        new.rename(build_dir)
    shutil.rmtree(tree)
    return build_dir


def fetch_build(project: Project, name: str, key: str) -> bool:
    """Download the build of `name` with `key` from the remote storage into the cache, and return whether it has it."""
    if not (storage := project.config.remote):
        return False
    with activity(f"downloading the build of {name}") as on_line:
        return remote.download_tree(storage, build_path(project, name, key), cached_build(project, name, key), on_line)


def _show_plan(project: Project, plan: list[str], keys: dict[str, str], *, fetch: bool) -> None:
    """Show what is to build in `plan`, and how long it took last time."""
    to_build = [name for name in plan if not cached_build(project, name, keys[name]).exists()]
    if fetch and (storage := project.config.remote):
        try:
            fetched = {name for name in to_build if remote.tree_exists(storage, build_path(project, name, keys[name]))}
        except remote.RemoteError as e:
            console.print(f"[yellow]remote storage unavailable[/]: {escape(str(e))}")
            fetched = set()
        to_build = [name for name in to_build if name not in fetched]
    else:
        fetched = set()
    for name in plan:
        if name in to_build:
            console.print(f"[bold]build[/]    {name}{_estimate(project, name)}")
        else:
            console.print(f"[dim]{'fetch' if name in fetched else 'cached':<9}[/]{name}")
    durations = [d for name in to_build if (d := previous_duration(project, name))]
    summary = [f"{len(to_build)} to build"]
    if durations:
        summary.append(
            f"~{duration(sum(durations))}" + (" for the ones built before" if len(durations) < len(to_build) else "")
        )
    console.print(", ".join(summary))


def build(  # noqa: PLR0913
    project: Project,
    targets: list[str],
    *,
    with_deps: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
    autorev: dict[str, str] | None = None,
    fetch: bool = True,
    build_missing: bool = True,
) -> tuple[list[Result], dict[str, str]]:
    """Build `targets` and, unless `with_deps` is false, what they need, skipping what is cached, and return how
    it went, with the keys of all the packages, those of their builds.

    `autorev` has the release suffixes of the test builds, by package. Without
    `fetch`, what is not cached locally is built, even when the remote storage
    has it. Without `build_missing`, what is neither cached nor in the remote
    storage fails, rather than being built. When builds find what changes the
    build roots of others, the builds are planned again, once. A package whose
    directory, or one of its dependencies', changed since the start fails: the
    builds work on the packages as they were then.

    The lock must be prepared: see resolution.prepare().
    """  # noqa: D205
    autorev = autorev or {}
    if not dry_run:
        build_image(project)
    for attempt in range(2):
        keys, hashes = _record_cached(project, autorev)
        plan = build_order(project.deps, targets, with_deps=with_deps)
        if dry_run:
            _show_plan(project, plan, keys, fetch=fetch)
            return [], keys
        _queue_file(project).parent.mkdir(parents=True, exist_ok=True)
        try:
            results, keys = _build_all(
                project,
                plan,
                keys,
                hashes,
                verbose=verbose,
                autorev=autorev,
                fetch=fetch,
                build_missing=build_missing,
            )
        finally:
            _queue_file(project).unlink(missing_ok=True)
        if attempt or all(r.status != "again" for r in results):
            break
        console.print("the builds found what changes the build roots: planning them again")
    # those to build again still, after the second attempt
    return [Result(r.name, "failed") if r.status == "again" else r for r in results], keys


def _record_cached(project: Project, autorev: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Record what rpm generated for the cached builds of the current keys, and return the keys, with what they
    recorded, and the hashes of the package directories they have.
    """  # noqa: D205
    hashes = inputs(project)
    for _ in range(5):
        keys = build_keys(project, autorev, hashes)
        if not record_built(project, [n for n, k in keys.items() if cached_build(project, n, k).exists()], keys):
            return keys, hashes
        prepare(project)  # for what they recorded
    msg = "what the cached builds recorded keeps changing the keys"
    raise BuildError(msg)


def _build_all(  # noqa: PLR0913
    project: Project,
    plan: list[str],
    keys: dict[str, str],
    hashes: dict[str, str],
    *,
    verbose: bool,
    autorev: dict[str, str],
    fetch: bool,
    build_missing: bool,
) -> tuple[list[Result], dict[str, str]]:
    results: dict[str, Result] = {}
    use_remote = fetch and project.config.remote is not None
    recorded = _lock_state(project)
    for i, name in enumerate(plan, 1):
        _queue_file(project).write_text("\n".join([str(os.getpid()), *plan[i:]]) + "\n")
        keys, recorded = _current(project, keys, recorded, autorev, hashes)
        if done := _done(project, name, keys, hashes, verbose=verbose):
            results[name] = done
            continue
        if use_remote and name not in autorev:
            try:
                fetched = fetch_build(project, name, keys[name])
            except remote.RemoteError as e:
                console.print(f"[yellow]remote storage unavailable, building locally[/]: {escape(str(e))}")
                use_remote = fetched = False
            if fetched:
                _add_fetched(project, name, keys)
                results[name] = Result(name, "fetched")
                continue
        if failed := _failed(project, name, results):
            results[name] = Result(name, "skipped")
            console.print(f"[yellow]-[/] {name} [yellow]skipped[/], needs {', '.join(failed)}")
            continue
        if not build_missing:
            results[name] = Result(name, "failed")
            _show_failure(project, name, BuildError(f"not in the remote storage, for {project.arch}"), verbose=verbose)
            continue
        status = None if verbose else f"[{i}/{len(plan)}] building {name}{_estimate(project, name)}"
        if use_remote:  # without a compiler cache, the one of the remote storage
            ccache.fetch_missing(project, name)
        results[name] = _build_shown(project, name, keys, hashes, status=status, autorev=autorev, verbose=verbose)
    return list(results.values()), _current(project, keys, recorded, autorev, hashes)[0]


def _current(
    project: Project, keys: dict[str, str], recorded: bytes, autorev: dict[str, str], hashes: dict[str, str]
) -> tuple[dict[str, str], bytes]:
    """Return the `keys` of the lock `recorded`, and the lock, computed again when the builds changed it: not with
    the packages, as they were at the start.
    """  # noqa: D205
    if (state := _lock_state(project)) == recorded:
        return keys, recorded
    prepare(project)
    return build_keys(project, autorev, hashes), state


def _done(project: Project, name: str, keys: dict[str, str], hashes: dict[str, str], *, verbose: bool) -> Result | None:
    """Return the result of `name` when it is not to build: failed, as it or one of its dependencies changed since
    they had the `hashes` of the `keys`, or cached.
    """  # noqa: D205
    if edited := [n for n in sorted(reachable(project.deps, [name])) if hash_dir(project.package_dirs[n]) != hashes[n]]:
        error = BuildError(f"{', '.join(edited)} changed since the builds started: build again")
        _show_failure(project, name, error, verbose=verbose)
        return Result(name, "failed")
    if cached_build(project, name, keys[name]).exists():
        console.print(f"[dim]·[/] {name} [dim]cached[/]")
        return Result(name, "cached")
    return None


def _lock_state(project: Project) -> bytes:
    """Return the content of the lock, which the keys depend on."""
    return path.read_bytes() if (path := lock_path(project)).exists() else b""


def _build_shown(  # noqa: PLR0913
    project: Project,
    name: str,
    keys: dict[str, str],
    hashes: dict[str, str],
    *,
    status: str | None,
    autorev: dict[str, str],
    verbose: bool,
) -> Result:
    """Build `name` with _build_one(), and show how it went."""
    start = time.monotonic()
    try:
        _build_one(project, name, keys, hashes, status=status, autorev=autorev)
    except BuildRootChangedError as e:
        console.print(f"[yellow]↻[/] {name}: {escape(str(e))}")
        return Result(name, "again")
    except (OSError, RuntimeError) as e:
        _show_failure(project, name, e, verbose=verbose)
        return Result(name, "failed")
    console.print(f"[bold green]✓[/] {name} [green]built[/] in {duration(time.monotonic() - start)}")
    return Result(name, "built")


def _failed(project: Project, name: str, results: dict[str, Result]) -> list[str]:
    """Return the dependencies of `name` that failed or were skipped."""
    return sorted(dep for dep in project.deps[name] if dep in results and results[dep].status in ("failed", "skipped"))


def _add_fetched(project: Project, name: str, keys: dict[str, str]) -> None:
    """Record in the lock what rpm generated for the fetched build of `name`."""
    record_built(project, [name], keys)
    console.print(f"[green]↓[/] {name} [green]fetched[/]")


def _show_failure(project: Project, name: str, error: Exception, *, verbose: bool) -> None:
    """Show why the build of `name` failed, with the end of its log when it failed in the build environment."""
    console.print(f"[bold red]✗[/] {name}: {escape(str(error))}")
    if isinstance(error, ContainerError) and not verbose:
        for line in log_step(read_tail(project.log(name)))[-40:]:
            print_log_line(line)
        console.print(f"[dim]full log: xcraft log {name}[/]")


def shell(project: Project, name: str) -> None:
    """Open a shell in the build tree of `name`, with its build dependencies installed and sources prepared.

    rpmbuild works on the build tree there, like in the builds: rpmbuild -ba SPECS/<spec> builds it again, shown before
    the prompt. The build tree is the one of the host, in .xcraft/build/, where its files can be edited, and its
    packages are.
    """
    repo = deps_repo(project, name, build_keys(project))
    tree = prepare_tree(project, name)
    replace = _image_replacements(resolve(project), name)
    spec = main_spec(project.package_dirs[name]).name
    # the labels dim, the path bold, the command bold cyan: on a terminal, without NO_COLOR, like xcraft's console
    rows = [("build tree", f"{tree}, on the host", "bold"), ("to build", f"rpmbuild -ba SPECS/{spec}", "cyan")]
    after = (
        "echo '%_topdir /build' > ~/.rpmmacros"
        """ && echo 'alias rpmbuild="python3 /xcraft/offline.py rpmbuild"' >> ~/.bashrc && cd /build"""
        ' && dim= bold= cyan= off= && if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then'
        " dim=$'\\e[2m' bold=$'\\e[0;1m' cyan=$'\\e[0;1;36m' off=$'\\e[0m'; fi"
        + "".join(
            f' && printf "%s%-12s%s%s%s\\n" "$dim" {shlex.quote(label)} "${style}" {shlex.quote(value)} "$off"'
            for label, value, style in rows
        )
        + " && exec bash -l"
    )
    _run_build(project, name, repo, "p", replace=replace, after=after)


def make_repo(
    project: Project, names: list[str], dest: Path, keys: dict[str, str], *, autorev: dict[str, str] | None = None
) -> None:
    """Make `dest` a repository with the builds of `names`, with their `keys`, and the extra upstream RPMs.

    Check that everything they require is available, from this repository or from the
    build environment's ones. With the test builds of `autorev`, the repository only has
    them, to install over a host's packages, and they are checked with our other builds.
    """
    if missing := [name for name in names if not cached_build(project, name, keys[name]).exists()]:
        msg = f"not built: {', '.join(missing)}"
        raise BuildError(msg)
    repo = LocalRepo(project, dest)
    repo.reset(extra_upstream=not autorev)
    for name in names:
        repo.add(cached_build(project, name, keys[name]))
    repo.index()
    checked, check = repo, f"--check {repo_id(project)}"
    if autorev:
        checked = builds_repo(project, keys)
        check += "".join(f" --pkg {binary.name}" for name in names for binary in project.packages[name].binaries)
    # without best=False, repoclosure only considers the newest version of each package, even when the
    # priority of our repository makes dnf install an older one
    try:
        run(
            project,
            "repoclosure",
            f"{DNF_LOCK} dnf -q --refresh repoclosure --setopt=best=False {check}",
            volumes=checked.volumes(),
            show="checking dependencies",
        )
    except ContainerError:
        for line in (project.state / "logs/_repoclosure.log").read_text().splitlines():
            if line.startswith(("package:", "  unresolved deps", "    ")):
                print_log_line(line)
        msg = "some dependencies cannot be satisfied"
        raise BuildError(msg) from None
