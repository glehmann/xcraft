"""Building packages in the build environment, with a cache of the results (see xcraft.cache).

The builds run against a local repository holding the cached results matching
the current keys, with a priority taking over the build environment's
repositories. Packages not built locally come from those repositories.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.markup import escape
from rich.text import Text

from xcraft import buildid, ccache, remote
from xcraft.cache import (
    BuildError,
    build_keys,
    build_path,
    builds_repo,
    cached_build,
    deps_repo,
    inputs,
    previous_duration,
    release_suffix,
)
from xcraft.checks import BuildRootChangedError, check_build, check_resolved, record_dynamic, record_rpm_deps
from xcraft.container import DNF_LOCK, ContainerError, build_image, run
from xcraft.display import (
    activity,
    console,
    diagnostics,
    duration,
    in_ci,
    log_step,
    print_log_line,
    read_tail,
)
from xcraft.graph import build_order, reachable
from xcraft.localrepo import LocalRepo, repo_id
from xcraft.lock import lock_path
from xcraft.resolution import prepare
from xcraft.rpmbuild import image_replacements, prepare_tree, run_build
from xcraft.signing import dev_signer
from xcraft.specs import hash_dir

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project

Status = Literal["cached", "stored", "fetched", "built", "failed", "skipped", "again"]
"""What became of a package in a build: stored, in the remote storage only, see Fetch; again, to build again."""
Fetch = Literal["all", "needed", "none"]
"""What the builds fetch from the remote storage rather than build: all they can, or the builds of the dependencies
of those to build only, or nothing."""


@dataclass
class Result:
    """Outcome of a package in a build."""

    name: str
    status: Status

    @property
    def failed(self) -> bool:
        """Whether it is not built: failed, skipped, or still to build again."""
        return self.status in ("failed", "skipped", "again")


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
    resolved for it, or when it did not make the packages parsed from its
    spec (see xcraft.checks). Raise
    BuildRootChangedError when it found what changes its build root, to build
    it again, after the packages it needs. The `keys` are those of the
    `hashes` of the package directories at the start of the builds, computed
    again with them when the build changes the lock.
    """
    if error := project.packages[name].error:
        raise BuildError(error)
    resolved = check_resolved(project, name)
    staged = hash_dir(project.package_dirs[name])
    replace = image_replacements(resolved, name)
    repo = deps_repo(project, name, keys)
    tree = prepare_tree(project, name)
    log = project.log(name)
    suffix = release_suffix(keys[name])
    pid_file = _pid_file(project, name)
    pid_file.write_text(str(os.getpid()))
    start = time.time()
    try:
        with (
            activity(status, expected=previous_duration(project, name), still=f"building {name}")
            if status
            else nullcontext(None) as progress
        ):
            # the errors and warnings above the progress, like with the whole log, after the status, which CI shows
            header = Text.from_markup(status).plain if status and not in_ci() else ""
            diagnose = diagnostics(print_log_line, header=header) if status else print_log_line

            def on_line(line: str) -> None:
                if progress:
                    progress(line)
                diagnose(line)

            run_build(project, name, repo, "a", replace=replace, suffix=suffix, log=log, on_line=on_line)
    except ContainerError as e:
        # those it asked for before failing, maybe for one of ours not among its dependencies
        if (tree / "buildrequires.txt").exists() and record_dynamic(project, name, tree):
            msg = f"{e}, asking for other dynamic BuildRequires, now in the lock: built again after what they need"
            raise BuildRootChangedError(msg) from None
        raise
    finally:
        pid_file.unlink()
    ccache.note_build(project, name, start)
    if check_build(project, name, tree, suffix=suffix):
        keys = build_keys(project, autorev, hashes)  # covering what the build recorded
    if hash_dir(project.package_dirs[name]) != staged:
        msg = f"{project.package_dirs[name].relative_to(project.root)} changed during the build, build it again"
        raise BuildError(msg)
    # development builds are signed with the developer's key, if any
    if name in autorev and (signer := dev_signer(project)):
        for output in ("RPMS", "SRPMS"):
            signer.sign_rpms(project, tree / output)
    _cache(project, name, keys[name], tree, autorev)
    record_rpm_deps(project, [name], keys, quiet=True)  # see _build_all()


def _cache(project: Project, name: str, key: str, tree: Path, autorev: dict[str, str]) -> Path:
    """Move the build of `name` in `tree` to the cache, with its `key` and the development build suffixes `autorev`, and
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
        # the suffixes of all the development builds of the run, its own and those of the dependencies built with it
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
    with activity(f"downloading the build of {name}") as on_line:
        return remote.fetch_tree(project, build_path(project, name, key), cached_build(project, name, key), on_line)


def _show_plan(project: Project, plan: list[str], keys: dict[str, str], *, fetch: Fetch) -> None:
    """Show what is to build or fetch in `plan`, and how long it took last time, with how many are cached, or only
    in the remote storage.
    """  # noqa: D205
    to_build = [name for name in plan if not cached_build(project, name, keys[name]).exists()]
    stored = set()
    if fetch != "none" and project.config.remote:
        try:
            stored = {
                name for name in to_build if remote.tree_available(project, build_path(project, name, keys[name]))
            }
        except remote.RemoteError as e:
            console.print(f"[yellow]remote storage unavailable[/]: {escape(str(e))}")
        to_build = [name for name in to_build if name not in stored]
    fetched = (
        stored if fetch == "all" else stored & reachable(project.deps, {d for n in to_build for d in project.deps[n]})
    )
    for name in plan:
        if name in to_build:
            console.print(f"[bold]build[/]    {name}{_estimate(project, name)}")
        elif name in fetched:
            console.print(f"[dim]fetch[/]    {name}")
    durations = [d for name in to_build if (d := previous_duration(project, name))]
    summary = [f"{len(to_build)} to build"]
    if durations:
        summary.append(
            f"~{duration(sum(durations))}" + (" for the ones built before" if len(durations) < len(to_build) else "")
        )
    if fetched:
        summary.append(f"{len(fetched)} to fetch")
    if stored - fetched:
        summary.append(f"{len(stored - fetched)} in the remote storage")
    if cached := len(plan) - len(to_build) - len(stored):
        summary.append(f"{cached} cached")
    console.print(", ".join(summary))


def build(  # noqa: PLR0913
    project: Project,
    targets: list[str],
    *,
    with_deps: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
    autorev: dict[str, str] | None = None,
    fetch: Fetch = "all",
    build_missing: bool = True,
) -> tuple[list[Result], dict[str, str]]:
    """Build `targets` and, unless `with_deps` is false, what they need, skipping what is cached, and return how
    it went, with the keys of all the packages, those of their builds.

    `autorev` has the release suffixes of the development builds, by package. What is
    not cached locally is fetched from the remote storage when it has it, but
    only the builds of the dependencies of the packages to build with `fetch`
    needed, or nothing with none: it is built then. Without `build_missing`,
    what is neither cached nor in the remote
    storage fails, rather than being built. When builds find what changes the
    build roots of others, the builds are planned again, once. A package whose
    directory, or one of its dependencies', changed since the start fails: the
    builds work on the packages as they were then.

    The lock must be prepared: see resolution.prepare().
    """  # noqa: D205
    autorev = autorev or {}
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
        if not record_rpm_deps(
            project, [n for n, k in keys.items() if cached_build(project, n, k).exists()], keys, quiet=True
        ):
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
    fetch: Fetch,
    build_missing: bool,
) -> tuple[list[Result], dict[str, str]]:
    results: dict[str, Result] = {}
    use_remote = fetch != "none" and bool(project.config.remote)
    reused = {name for name, i in buildid.read(project).items() if i.accepts}
    # asked at once, with the keys of the start
    missing = [name for name in plan if not cached_build(project, name, keys[name]).exists()]
    stored = _stored(project, missing, keys) if use_remote and fetch == "needed" else {}
    recorded = _lock_state(project)
    for i, name in enumerate(plan, 1):
        _queue_file(project).write_text("\n".join([str(os.getpid()), *plan[i:]]) + "\n")
        keys, recorded = _current(project, keys, recorded, autorev, hashes)
        if done := _done(project, name, keys, hashes, verbose=verbose):
            results[name] = done
            continue
        if use_remote and name not in autorev:
            try:
                if _from_storage(project, name, keys, results, stored, fetch=fetch):
                    continue
            except remote.RemoteError as e:
                console.print(f"[yellow]remote storage unavailable, building locally[/]: {escape(str(e))}")
                use_remote = False
        if failed := _failed(project, name, results):
            results[name] = Result(name, "skipped")
            console.print(f"[yellow]-[/] {name} [yellow]skipped[/], needs {', '.join(failed)}")
            continue
        if not build_missing or name in reused:  # the build of the main branch, made by CI
            results[name] = Result(name, "failed")
            _show_failure(project, name, BuildError(f"not in the remote storage, for {project.arch}"), verbose=verbose)
            continue
        status = None if verbose else f"[{i}/{len(plan)}] building {name}{_estimate(project, name)}"
        if use_remote:
            _fetch_for(project, name, keys, results)
        results[name] = _build_shown(project, name, keys, hashes, status=status, autorev=autorev, verbose=verbose)
    keys = _current(project, keys, recorded, autorev, hashes)[0]
    # Retry cached builds too: packages built or fetched now may provide what they could not resolve before.
    record_rpm_deps(project, [n for n, k in keys.items() if cached_build(project, n, k).exists()], keys)
    return list(results.values()), keys


def _stored(project: Project, names: list[str], keys: dict[str, str]) -> dict[str, bool]:
    """Return whether the remote storage has the builds of `names` with `keys`, by their paths in it, asked
    concurrently: nothing when it cannot be asked, as each one is asked again then.
    """  # noqa: D205
    if not project.config.remote:
        return {}
    paths = [build_path(project, name, keys[name]) for name in names]
    try:
        with ThreadPoolExecutor(16) as pool:
            return dict(zip(paths, pool.map(lambda p: remote.tree_available(project, p), paths), strict=True))
    except remote.RemoteError:
        return {}


def _from_storage(  # noqa: PLR0913
    project: Project,
    name: str,
    keys: dict[str, str],
    results: dict[str, Result],
    stored: dict[str, bool],
    *,
    fetch: Fetch,
) -> bool:
    """Fetch the build of `name` from the remote storage, or only note it is there with `fetch` needed, in `results`,
    and return whether the storage has it: as `stored` says, by path, or asked now.
    """  # noqa: D205
    if fetch == "needed":  # fetched by _fetch_for(), if a build needs it
        path = build_path(project, name, keys[name])
        if path not in stored and project.config.remote:
            stored[path] = remote.tree_available(project, path)
        if not stored.get(path):
            return False
        results[name] = Result(name, "stored")
        return True
    if not fetch_build(project, name, keys[name]):
        return False
    _fetched(project, name, keys, results)
    return True


def _fetch_for(project: Project, name: str, keys: dict[str, str], results: dict[str, Result]) -> None:
    """Fetch from the remote storage what the build of `name` needs: the compiler cache of the storage, without
    one, and the builds of its dependencies only there, see Fetch.
    """  # noqa: D205
    ccache.fetch_missing(project, name)
    try:
        for dep in sorted(reachable(project.deps, project.deps[name])):
            if not cached_build(project, dep, keys[dep]).exists() and fetch_build(project, dep, keys[dep]):
                _fetched(project, dep, keys, results)
    except remote.RemoteError as e:
        console.print(f"[yellow]remote storage unavailable[/]: {escape(str(e))}")


def _fetched(project: Project, name: str, keys: dict[str, str], results: dict[str, Result]) -> None:
    """Record what rpm generated for the build of `name` just fetched, and show it."""
    record_rpm_deps(project, [name], keys, quiet=True)  # see _build_all()
    console.print(f"[green]↓[/] {name} [green]fetched[/]")
    results[name] = Result(name, "fetched")


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
    if cached_build(project, name, keys[name]).exists():  # nothing to show: only the counts
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
    build_image(project)  # only when a package is to build, rather than cached, fetched or stored: failing them all
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


def _show_failure(project: Project, name: str, error: Exception, *, verbose: bool) -> None:
    """Show why the build of `name` failed, with the end of its log when it failed in the build environment, after
    the whole log in CI, to debug it there: folded on GitHub Actions.
    """  # noqa: D205
    console.print(f"[bold red]✗[/] {name}: {escape(str(error))}")
    if isinstance(error, ContainerError) and not verbose:
        lines = read_tail(project.log(name), 16 * 2**20)
        if in_ci():
            fold = os.environ.get("GITHUB_ACTIONS") == "true"
            console.file.write(f"::group::the log of {name}\n" if fold else f"the log of {name}:\n")
            console.file.write("".join(f"{line}\n" for line in lines) + ("::endgroup::\n" if fold else ""))
        for line in log_step(lines)[-40:]:
            print_log_line(line)
        console.print(f"[dim]full log: xcraft log {name}[/]")


def make_repo(
    project: Project, names: list[str], dest: Path, keys: dict[str, str], *, autorev: dict[str, str] | None = None
) -> None:
    """Make `dest` a repository with the builds of `names`, with their `keys`, and the extra upstream RPMs.

    Check that everything they require is available, from this repository or from the
    build environment's ones. With the development builds of `autorev`, the repository only has
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
        check += "".join(
            f" --pkg {n}" for name in names for n in project.packages[name].built_names(release_suffix(keys[name]))
        )
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
