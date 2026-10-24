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
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.markup import escape
from rich.text import Text

from xcraft import ccache, remote
from xcraft.cache import (
    BuildError,
    build_keys,
    build_path,
    builds_repo,
    cached_build,
    deps_repo,
    inputs,
    previous_duration,
)
from xcraft.checks import BuildRootChangedError, check_build, check_resolved, record_built, record_dynamic
from xcraft.container import DNF_LOCK, ContainerError, build_image, run
from xcraft.display import activity, console, diagnostics, duration, log_step, print_log_line, read_tail
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

Status = Literal["cached", "fetched", "built", "failed", "skipped", "again"]
"""What became of a package in a build: again, to build again."""


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

            run_build(project, name, repo, "a", replace=replace, suffix=autorev.get(name, ""), log=log, on_line=on_line)
    except ContainerError as e:
        # those it asked for before failing, maybe for one of ours not among its dependencies
        if (tree / "buildrequires.txt").exists() and record_dynamic(project, name, tree):
            msg = f"{e}, asking for other dynamic BuildRequires, now in the lock: built again after what they need"
            raise BuildRootChangedError(msg) from None
        raise
    finally:
        pid_file.unlink()
    if check_build(project, name, tree, test=name in autorev):
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
                record_built(project, [name], keys)
                console.print(f"[green]↓[/] {name} [green]fetched[/]")
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


def _show_failure(project: Project, name: str, error: Exception, *, verbose: bool) -> None:
    """Show why the build of `name` failed, with the end of its log when it failed in the build environment."""
    console.print(f"[bold red]✗[/] {name}: {escape(str(error))}")
    if isinstance(error, ContainerError) and not verbose:
        for line in log_step(read_tail(project.log(name)))[-40:]:
            print_log_line(line)
        console.print(f"[dim]full log: xcraft log {name}[/]")


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
