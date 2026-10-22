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
import shutil
import time
import urllib.request
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.text import Text

from xcraft import remote
from xcraft.container import BUILDENV, DNF_LOCK, ContainerError, build_image, image_tag, run, run_container
from xcraft.display import (
    activity,
    console,
    diagnostics,
    download_progress,
    duration,
    log_step,
    print_log_line,
    read_tail,
)
from xcraft.graph import build_order, components
from xcraft.localrepo import REPO_FILE, LocalRepo, link, repo_id
from xcraft.lock import Locked, load_lock, save_lock
from xcraft.project import write_atomically
from xcraft.signing import dev_signer
from xcraft.sources import archive_name, read_manifest, sha256_of
from xcraft.specs import hash_dir, main_spec

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xcraft.project import Project

# Run in the build tree, mounted on /build: installs the build dependencies, including the
# dynamic ones, then runs `rpmbuild`, with stdin from /dev/null like koji and mock, so that
# interactive steps of a build don't wait forever.
_BUILD_SCRIPT = """\
set -euo pipefail
export CCACHE_DIR=/ccache PATH=/usr/lib64/ccache:$PATH
spec=/build/SPECS/{spec}
time {{
# dnf may not see that the local repository changed when it is regenerated within a second
{lock} dnf -q makecache --repo {repo} --refresh
{replace}{lock} dnf -y builddep -D "_topdir /build" {defines} "$spec"
if grep -q '^%generate_buildrequires' "$spec"; then
    # rpmbuild -br exits with 11 while some dynamic BuildRequires are missing
    while rc=0; rpmbuild -br -D "_topdir /build" {defines} "$spec" </dev/null || rc=$?; [ $rc -eq 11 ]; do
        {lock} dnf -y builddep /build/SRPMS/*.buildreqs.nosrc.rpm
        rm /build/SRPMS/*.buildreqs.nosrc.rpm
    done
    [ $rc -eq 0 ]
    rm -f /build/SRPMS/*.buildreqs.nosrc.rpm
fi
{lock} python3 /xcraft/record.py /build/buildroot.json
# like mock's login shell, for the environment of build dependencies, like opam's, which are not written for set -eu
set +eu +o pipefail
for profile in /etc/profile.d/*.sh; do . "$profile"; done
set -eu -o pipefail
rpmbuild -b{stage} -D "_topdir /build" {defines} "$spec" </dev/null
}}
{then}
"""

_RECIPE = hashlib.sha256((REPO_FILE + _BUILD_SCRIPT + (BUILDENV / "record.py").read_text()).encode()).hexdigest()
"""What makes the builds, besides the image and the packages: a change to it rebuilds them all."""


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


def build_keys(project: Project, autorev: dict[str, str] | None = None) -> dict[str, str]:
    """Return the cache key of each package: a SHA-256 of all that makes its build.

    That is its package directory, the build environment image and recipe,
    the locked versions of the upstream packages its last build installed,
    besides the image's ones, which the image id covers, the `autorev` of the
    test builds, and the keys of its dependencies. Packages depending on each
    other get the key of them all.
    """
    lock, deps = load_lock(project), project.deps
    base = json.dumps([image_tag(project), project.platform, project.config.extra_upstream, _RECIPE])

    def inputs(name: str) -> str:
        buildroot = [f"{upstream}={lock.packages[upstream].evr}" for upstream in lock.buildroots.get(name, [])]
        return json.dumps([name, hash_dir(project.package_dirs[name]), (autorev or {}).get(name), buildroot])

    keys: dict[str, str] = {}
    for component in components(deps):  # the dependencies first
        h = hashlib.sha256(base.encode())
        for name in sorted(component):
            h.update(inputs(name).encode())
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


def test_builds(project: Project, name: str) -> list[TestBuild]:
    """Return the latest cached test or pre build of `name` for each ID, the newest first."""
    latest: dict[tuple[str, str], TestBuild] = {}
    for marker in cached_build(project, name, "").glob("[!.]*/autorev"):
        autorev = json.loads(marker.read_text())
        if not (m := _TEST_SUFFIX.match(autorev[name])):
            continue
        kind = "test" if m["test"] else "pre"
        build = TestBuild(
            kind=kind,
            ident=m[kind],
            number=int(m["number"]),
            built=(marker.parent / "build.log").stat().st_mtime,
            current=build_keys(project, autorev)[name] == marker.parent.name,
        )
        if (kind, build.ident) not in latest or build.number > latest[kind, build.ident].number:
            latest[kind, build.ident] = build
    return sorted(latest.values(), key=lambda build: -build.built)


def builds_repo(project: Project, keys: dict[str, str]) -> LocalRepo:
    """Return the repository the builds run against, with the cached builds matching `keys`."""
    repo = LocalRepo(project, project.state / "repo")
    repo.reset()
    for name, key in keys.items():
        if cached_build(project, name, key).exists():
            repo.add(cached_build(project, name, key))
    return repo


def source_path(filename: str, digest: str) -> str:
    """Return the path of a source archive in the remote storage."""
    return f"sources/{digest}/{filename}"


def build_path(project: Project, name: str, key: str) -> str:
    """Return the path of the tree of a build in the remote storage."""
    return f"builds/{project.arch}/{name}/{key}"


BUILD_FILES = ["RPMS", "SRPMS", "build.log"]
"""What a build is made of, in the cache and in the remote storage."""


def _fetch_source(project: Project, name: str, filename: str, digest: str) -> bool:
    """Download an archive of `name` into the source cache, and return whether it was found.

    It comes from the remote storage, or else from the Source0 of `name`, when that is its URL.
    """
    archive = project.source_cache / digest / filename
    part = archive.with_name(f"{filename}.part")
    part.parent.mkdir(parents=True, exist_ok=True)
    source0, origin, error = project.packages[name].source0, "", None
    with activity(f"downloading {filename}") as on_line:
        try:
            if (storage := project.config.remote) and remote.download(
                storage, source_path(filename, digest), part, on_line
            ):
                origin = "the remote storage"
        except remote.RemoteError as e:
            error = e
        if not origin and "://" in source0 and archive_name(source0) == filename:
            try:
                urllib.request.urlretrieve(source0, part, reporthook=download_progress(on_line))  # noqa: S310
                origin = source0
            except OSError as e:
                error = e
    if not origin:
        part.unlink(missing_ok=True)
        if error:
            msg = f"{filename} is not in the source cache, and cannot be downloaded: {error}"
            raise BuildError(msg) from error
        return False
    if sha256_of(part) != digest:
        part.unlink()
        msg = f"{filename} from {origin} does not match the sha256 of the manifest"
        raise BuildError(msg)
    part.rename(archive)
    return True


def fetch_sources(project: Project, name: str) -> list[str]:
    """Put the archives of `name` in the source cache, downloaded when missing, and return those found nowhere."""
    manifest = project.package_dirs[name] / "sources"
    return [
        filename
        for filename, digest in (read_manifest(manifest).items() if manifest.exists() else [])
        if not (project.source_cache / digest / filename).exists()
        and not _fetch_source(project, name, filename, digest)
    ]


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
        msg = f"{', '.join(missing)} is not in the source cache, run: xcraft import {name}"
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
    script = _BUILD_SCRIPT.format(
        spec=main_spec(project.package_dirs[name]).name,
        stage=rpmbuild_stage,
        then=after,
        lock=DNF_LOCK,
        repo=repo_id(project),
        defines=f'-D "autorev {suffix}"' if suffix else "",
        # ours have their own versions, older ones for kernel-headers, and are not in the versionlock
        replace=f"{DNF_LOCK} dnf -y --disableplugin=versionlock --repo {repo_id(project)} distro-sync"
        f" {' '.join(replace)}\n"
        if replace
        else "",
    )
    ccache = project.state / "ccache" / name
    ccache.mkdir(parents=True, exist_ok=True)
    tree = project.state / "build" / name
    volumes = [f"{tree}:/build", f"{ccache}:/ccache", *repo.volumes()]
    run_container(project, script, container=f"xcraft-{project.id}-{name}", volumes=volumes, log=log, on_line=on_line)


@dataclass
class Result:
    """Outcome of a package in a build."""

    name: str
    status: str  # cached, fetched, built, failed or skipped


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


def _image_replacements(project: Project, name: str) -> list[str]:
    """Return the upstream packages of the image to replace by ours in the build root of `name`."""
    image = set(load_lock(project).image)
    return sorted(b.name for dep in project.deps[name] for b in project.packages[dep].binaries if b.name in image)


def _check_build_root(project: Project, name: str, recorded: dict[str, Locked], image: list[str]) -> None:
    """Raise when dnf installed, in the build root of `name`, packages that its dependencies do not explain.

    Those are binaries of ours from packages not among its dependencies, which
    the cache key and the build order miss, and upstream binaries with the name
    of one of ours, installed instead of ours, except those of the `image` in
    the builds of the packages ours need. Both happen with requirements xcraft
    cannot see in the specs: files, automatic provides, versions.
    """
    deps = project.deps[name]
    owners = {binary.name: package.name for package in project.packages.values() for binary in package.binaries}
    # those of no package of ours are the extra upstream ones, in the same repository
    unknown = sorted(
        f"{n} ({owners[n]})"
        for n, p in recorded.items()
        if p.repo == "xcraft" and n in owners and owners[n] not in {name, *deps}
    )
    shadowed = sorted(
        f"{n} (ours from {owners[n]})"
        for n, p in recorded.items()
        if p.repo != "xcraft" and n in owners and (n not in image or owners[n] in deps)
    )
    if unknown or shadowed:
        msg = "".join(
            [
                f"installed our {', '.join(unknown)}, missing from its dependencies; " if unknown else "",
                f"installed the upstream {', '.join(shadowed)}; " if shadowed else "",
                "add a BuildRequires on their name to its spec",
            ]
        )
        raise BuildError(msg)


def _record_build_root(project: Project, name: str, tree: Path, *, test: bool) -> None:
    """Add to the lock the upstream packages of the build root of `name`, and the build root unless a `test` build."""
    recorded = {n: Locked.model_validate(data) for n, data in json.loads((tree / "buildroot.json").read_text()).items()}
    lock = load_lock(project)
    _check_build_root(project, name, recorded, lock.image)
    installed = {n: p for n, p in recorded.items() if p.repo != "xcraft" and n not in lock.image}
    lock.merge(installed)
    if not test:
        lock.buildroots[name] = sorted(installed)
    save_lock(project, lock)


def _build_one(project: Project, name: str, repo: LocalRepo, *, status: str | None, autorev: dict[str, str]) -> None:
    """Build `name` and cache the result, showing `status` while it runs, or the build log without status.

    The upstream packages its build installed are added to the lock, and its
    build root too, unless it is a test build (in `autorev`).
    It is not cached when its spec directory changed during the build, or when
    its build root does not match its dependencies (see _check_build_root).
    """
    if error := project.packages[name].error:
        raise BuildError(error)
    staged = hash_dir(project.package_dirs[name])
    replace = _image_replacements(project, name)
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
    finally:
        pid_file.unlink()
    _record_build_root(project, name, tree, test=name in autorev)
    if hash_dir(project.package_dirs[name]) != staged:
        msg = f"{project.package_dirs[name].relative_to(project.root)} changed during the build, build it again"
        raise BuildError(msg)
    # test builds are signed with the developer's key, if any
    if name in autorev and (signer := dev_signer(project)):
        for output in ("RPMS", "SRPMS"):
            signer.sign_rpms(project, tree / output)
    # its key now covers the build root it got
    build_dir = cached_build(project, name, build_keys(project, autorev)[name])
    # assembled next to it, and renamed, not to leave a partial build in the cache
    new = build_dir.with_name(f".{build_dir.name}")
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    for output in ("RPMS", "SRPMS"):
        (tree / output).rename(new / output)
    if name in autorev:
        # the suffixes of all the test builds of the run, its own and those of the dependencies built with it
        (new / "autorev").write_text(json.dumps(autorev, sort_keys=True))
    shutil.copy2(log, new / "build.log")
    if build_dir.exists():  # an earlier build of the same inputs
        shutil.rmtree(new)
    else:
        new.rename(build_dir)
    shutil.rmtree(tree)
    repo.add(build_dir)


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
) -> list[Result]:
    """Build `targets` and, unless `with_deps` is false, what they need, skipping what is cached.

    `autorev` has the release suffixes of the test builds, by package. Without
    `fetch`, what is not cached locally is built, even when the remote storage
    has it. Without `build_missing`, what is neither cached nor in the remote
    storage fails, rather than being built.
    """
    autorev = autorev or {}
    plan = build_order(project.deps, targets, with_deps=with_deps)
    if not dry_run:
        build_image(project)  # which may change its tag, when it is new
    keys = build_keys(project, autorev)
    if dry_run:
        _show_plan(project, plan, keys, fetch=fetch)
        return []
    repo = builds_repo(project, keys)
    _queue_file(project).parent.mkdir(parents=True, exist_ok=True)
    try:
        return _build_all(
            project, plan, repo, verbose=verbose, autorev=autorev, fetch=fetch, build_missing=build_missing
        )
    finally:
        _queue_file(project).unlink(missing_ok=True)


def _build_all(  # noqa: PLR0913
    project: Project,
    plan: list[str],
    repo: LocalRepo,
    *,
    verbose: bool,
    autorev: dict[str, str],
    fetch: bool,
    build_missing: bool,
) -> list[Result]:
    results: dict[str, Result] = {}
    use_remote = fetch and project.config.remote is not None
    for i, name in enumerate(plan, 1):
        _queue_file(project).write_text("\n".join([str(os.getpid()), *plan[i:]]) + "\n")
        # the keys change with each build, which records its build root
        keys = build_keys(project, autorev)
        if cached_build(project, name, keys[name]).exists():
            results[name] = Result(name, "cached")
            console.print(f"[dim]·[/] {name} [dim]cached[/]")
            continue
        if use_remote and name not in autorev:
            try:
                fetched = fetch_build(project, name, keys[name])
            except remote.RemoteError as e:
                console.print(f"[yellow]remote storage unavailable, building locally[/]: {escape(str(e))}")
                use_remote = fetched = False
            if fetched:
                repo.add(cached_build(project, name, keys[name]))
                results[name] = Result(name, "fetched")
                console.print(f"[green]↓[/] {name} [green]fetched[/]")
                continue
        if failed := sorted(
            dep for dep in project.deps[name] if results.get(dep, Result(dep, "")).status in ("failed", "skipped")
        ):
            results[name] = Result(name, "skipped")
            console.print(f"[yellow]-[/] {name} [yellow]skipped[/], needs {', '.join(failed)}")
            continue
        start = time.monotonic()
        try:
            if not build_missing:
                msg = f"not in the remote storage, for {project.arch}"
                raise BuildError(msg)
            status = None if verbose else f"[{i}/{len(plan)}] building {name}{_estimate(project, name)}"
            _build_one(project, name, repo, status=status, autorev=autorev)
        except (OSError, RuntimeError) as e:
            results[name] = Result(name, "failed")
            _show_failure(project, name, e, verbose=verbose)
            continue
        results[name] = Result(name, "built")
        console.print(f"[bold green]✓[/] {name} [green]built[/] in {duration(time.monotonic() - start)}")
    return list(results.values())


def _show_failure(project: Project, name: str, error: Exception, *, verbose: bool) -> None:
    """Show why the build of `name` failed, with the end of its log when it failed in the build environment."""
    console.print(f"[bold red]✗[/] {name}: {escape(str(error))}")
    if isinstance(error, ContainerError) and not verbose:
        for line in log_step(read_tail(project.log(name)))[-40:]:
            print_log_line(line)
        console.print(f"[dim]full log: xcraft log {name}[/]")


def shell(project: Project, name: str) -> None:
    """Open a shell in the build tree of `name`, with its build dependencies installed and sources prepared.

    rpmbuild works on the build tree there, like in the builds: rpmbuild -ba SPECS/*.spec builds it again.
    """
    repo = builds_repo(project, build_keys(project))
    prepare_tree(project, name)
    replace = _image_replacements(project, name)
    after = (
        "echo '%_topdir /build' > ~/.rpmmacros && cd /build"
        " && echo 'the build tree of the package, prepared: rpmbuild -ba SPECS/*.spec builds it' && exec bash -l"
    )
    _run_build(project, name, repo, "p", replace=replace, after=after)


def make_repo(project: Project, names: list[str], dest: Path, *, autorev: dict[str, str] | None = None) -> None:
    """Make `dest` a repository with the builds of `names` and the extra upstream RPMs.

    Check that everything they require is available, from this repository or from the
    build environment's ones. With the test builds of `autorev`, the repository only has
    them, to install over a host's packages, and they are checked with our other builds.
    """
    keys = build_keys(project, autorev)
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
