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
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from rich.markup import escape
from rich.text import Text

from xcraft import buildid, buildkey, ccache, remote, resolution
from xcraft.builds import load_builds
from xcraft.cache import (
    NOCHECK,
    BuildError,
    build_keys,
    build_path,
    builds_repo,
    cached_build,
    deps_repo,
    previous_duration,
    release_suffix,
    untested,
)
from xcraft.checks import BuildRootChangedError, record_build, record_dynamic, record_rpm_deps, resolved_root
from xcraft.container import ContainerError, built_image, run_dnf
from xcraft.devbuilds import dev_keys, keep_built
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
from xcraft.localrepo import REPO_FILE, LocalRepo, repo_id
from xcraft.lock import lock_path
from xcraft.packages import is_debug
from xcraft.rpmbuild import image_replacements, prepare_tree, run_build
from xcraft.signing import dev_signer
from xcraft.specs import hash_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
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


@contextmanager
def _turn(project: Project, name: str) -> Generator[None]:
    """Mark `name` as being built during its turn: from the resolutions before its build, to the recording after it."""
    (path := _pid_file(project, name)).write_text(str(os.getpid()))
    try:
        yield
    finally:
        path.unlink()


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


@contextmanager
def _shown(project: Project, name: str, status: str | None) -> Generator[Callable[[str], None]]:
    """Show the build log of `name`, given line by line to the function yielded: whole without `status`, or only its
    errors and warnings, with `status` shown while it runs.
    """
    if not status:
        yield print_log_line
        return
    with activity(status, expected=previous_duration(project, name), still=f"building {name}") as progress:
        # the errors and warnings above the progress, like with the whole log, after the status, which CI shows
        diagnose = diagnostics(print_log_line, header="" if in_ci() else Text.from_markup(status).plain)

        def on_line(line: str) -> None:
            progress(line)
            diagnose(line)

        yield on_line


def fetch_build(project: Project, name: str, key: str) -> bool:
    """Download the build of `name` with `key` from the remote storage into the cache, and return whether it has it."""
    with activity(f"downloading the build of {name}") as on_line:
        return remote.fetch_tree(project, build_path(project, name, key), cached_build(project, name, key), on_line)


def _split_plan(
    project: Project, plan: list[str], keys: dict[str, str], *, fetch: Fetch
) -> tuple[list[str], set[str], set[str]]:
    """Return the packages of `plan` to build, those only in the remote storage, and those of them to fetch."""
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
    needed = reachable(project.deps, {d for n in to_build for d in project.deps[n]})
    return to_build, stored, stored if fetch == "all" else stored & needed


def _show_plan(project: Project, plan: list[str], keys: dict[str, str], *, fetch: Fetch) -> None:
    """Show what is to build or fetch in `plan`, and how long it took last time, with how many are cached, or only
    in the remote storage.
    """
    to_build, stored, fetched = _split_plan(project, plan, keys, fetch=fetch)
    for name in plan:
        if name in to_build:
            console.print(f"[bold]build[/]    {name}{_estimate(project, name)}")
        elif name in fetched:
            console.print(f"[dim]fetch[/]    {name}")
    summary = [f"{len(to_build)} to build"]
    if durations := [d for name in to_build if (d := previous_duration(project, name))]:
        summary.append(
            f"~{duration(sum(durations))}" + (" for the ones built before" if len(durations) < len(to_build) else "")
        )
    counts = {
        "to fetch": len(fetched),
        "in the remote storage": len(stored - fetched),
        "cached": len(plan) - len(to_build) - len(stored),
    }
    console.print(", ".join(summary + [f"{n} {what}" for what, n in counts.items() if n]))


def update_build_ids(project: Project) -> list[str]:
    """Update the build IDs of the packages, showing the changed ones, and return them, or fail with --locked, see
    xcraft.buildid.
    """
    if changed := buildid.update(project):
        ids = buildid.read(project)
        console.print("build IDs: " + ", ".join(f"{name} {ids[name].number}" for name in changed if name in ids))
    return changed


def _record_cached(project: Project, dev: str | None) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Record what rpm generated for the cached builds of the current keys, and return the keys, with what they
    recorded, the hashes of the package directories they have, and the suffixes of the development builds `dev`.
    """
    hashes = buildkey.directories(project)
    for _ in range(5):
        keys, autorev = dev_keys(project, dev, hashes)
        if not record_rpm_deps(
            project, [n for n, k in keys.items() if cached_build(project, n, k).exists()], keys, quiet=True
        ):
            return keys, hashes, autorev
        resolution.prepare(project)  # for what they recorded
    msg = "what the cached builds recorded keeps changing the keys"
    raise BuildError(msg)


def _stored(project: Project, names: list[str], keys: dict[str, str]) -> dict[str, bool]:
    """Return whether the remote storage has the builds of `names` with `keys`, by their paths in it, asked
    concurrently: nothing when it cannot be asked, as each one is asked again then.
    """
    paths = [build_path(project, name, keys[name]) for name in names]
    try:
        with ThreadPoolExecutor(16) as pool:
            return dict(zip(paths, pool.map(lambda p: remote.tree_available(project, p), paths), strict=True))
    except remote.RemoteError:
        return {}


def _lock_state(project: Project) -> bytes:
    """Return the content of the lock, and what the builds recorded, which the keys depend on."""
    records = load_builds(project).model_dump_json(exclude={"ids"}).encode()
    return (path.read_bytes() if (path := lock_path(project)).exists() else b"") + records


def _show_failure(project: Project, name: str, error: Exception, *, verbose: bool) -> None:
    """Show why the build of `name` failed, with the end of its log when it failed in the build environment, after
    the whole log in CI, to debug it there: folded on GitHub Actions.
    """
    console.print(f"[bold red]✗[/] {name}: {escape(str(error))}")
    if isinstance(error, ContainerError) and not verbose:
        lines = read_tail(project.log(name), 16 * 2**20)
        if in_ci():
            fold = os.environ.get("GITHUB_ACTIONS") == "true"
            console.file.write(f"::group::full {name} build log\n" if fold else f"full {name} build log:\n")
            console.file.write("".join(f"{line}\n" for line in lines) + ("::endgroup::\n" if fold else ""))
        for line in log_step(lines)[-40:]:
            print_log_line(line)
        console.print(f"[dim]full log: xcraft log {name}[/]")


@dataclass
class _Run:
    """A run of builds, of the packages as they were at the start, with the `hashes` of their directories: the `keys`
    of all the packages, and the suffixes `autorev` of the development builds `dev`, follow the lock, which the builds
    change.
    """

    project: Project
    hashes: dict[str, str]
    keys: dict[str, str]
    autorev: dict[str, str]
    dev: str | None = None
    nocheck: bool = False
    verbose: bool = False
    fetch: Fetch = "all"
    build_missing: bool = True
    results: dict[str, Result] = field(default_factory=dict)
    stored: dict[str, bool] = field(default_factory=dict)
    """Whether the remote storage has the builds, by path."""
    use_remote: bool = field(default=False, init=False)
    recorded: bytes = field(default=b"", init=False)
    """The state of the lock of the `keys`, see _lock_state()."""

    def build(self, plan: list[str]) -> list[Result]:
        """Build the packages of `plan`, in order, and return how it went, see xcraft.build.build()."""
        project = self.project
        self.use_remote = self.fetch != "none" and bool(project.config.remote)
        reused = {name for name, i in buildid.read(project).items() if i.accepts}
        if self.use_remote and self.fetch == "needed":  # asked at once, with the keys of the start
            missing = [name for name in plan if not cached_build(project, name, self.keys[name]).exists()]
            self.stored = _stored(project, missing, self.keys)
        self.recorded = _lock_state(project)
        for i, name in enumerate(plan, 1):
            _queue_file(project).write_text("\n".join([str(os.getpid()), *plan[i:]]) + "\n")
            with _turn(project, name):
                self._refresh()
                self.results[name] = self._one(name, f"[{i}/{len(plan)}]", reused=name in reused)
        self._refresh()
        return self._recorded_again()

    def _one(self, name: str, step: str, *, reused: bool) -> Result:
        """Return the result of `name`, at `step` of the plan: built, unless it is done, in the remote storage, or not
        to build, like when `reused`, the build of the main branch, made by CI.
        """
        if result := self._done(name) or self._from_storage(name) or self._not_built(name, reused=reused):
            return result
        if self.use_remote:
            self._fetch_for(name)
            self._refresh()  # for what they recorded
        status = None if self.verbose else f"{step} building {name}{_estimate(self.project, name)}"
        if (result := self._build_shown(name, status)).status == "built":
            keep_built(self.project, self.dev, self.autorev, [name], self.hashes)
        return result

    def _refresh(self) -> None:
        """Compute the `keys` and the suffixes `autorev` again when the builds changed the lock: not with the packages,
        as they were at the start.
        """
        if (state := _lock_state(self.project)) != self.recorded:
            resolution.prepare(self.project)
            (self.keys, self.autorev), self.recorded = dev_keys(self.project, self.dev, self.hashes), state

    def _done(self, name: str) -> Result | None:
        """Return the result of `name` when it is not to build: failed, as it or one of its dependencies changed since
        they had their `hashes`, or cached.
        """
        project = self.project
        if edited := [
            n
            for n in sorted(reachable(project.deps, [name]))
            if hash_dir(project.package_dirs[n], project.root) != self.hashes[n]
        ]:
            error = BuildError(f"{', '.join(edited)} changed since the builds started: build again")
            _show_failure(project, name, error, verbose=self.verbose)
            return Result(name, "failed")
        build = cached_build(project, name, self.keys[name])
        if not self.nocheck and build.exists() and untested(project, name, self.keys[name]):
            shutil.rmtree(build)  # made again, with the tests of its spec
        if build.exists():  # nothing to show: only the counts
            return Result(name, "cached")
        return None

    def _from_storage(self, name: str) -> Result | None:
        """Return the result of `name` when the remote storage has its build: fetched, or only noted there with `fetch`
        needed, as `stored` says, or asked now.
        """
        if not self.use_remote or name in self.autorev:
            return None
        try:
            if self.fetch != "needed":
                return self._fetched(name) if fetch_build(self.project, name, self.keys[name]) else None
            # fetched by _fetch_for(), if a build needs it
            if (path := build_path(self.project, name, self.keys[name])) not in self.stored:
                self.stored[path] = remote.tree_available(self.project, path)
            return Result(name, "stored") if self.stored[path] else None
        except remote.RemoteError as e:
            console.print(f"[yellow]remote storage unavailable, building locally[/]: {escape(str(e))}")
            self.use_remote = False
            return None

    def _not_built(self, name: str, *, reused: bool) -> Result | None:
        """Return the result of `name` when it is not to build: skipped, as dependencies failed, or failed without
        `build_missing`, or when `reused`.
        """
        if failed := sorted(
            dep
            for dep in self.project.deps[name]
            if dep in self.results and self.results[dep].status in ("failed", "skipped")
        ):
            console.print(f"[yellow]-[/] {name} [yellow]skipped[/], needs {', '.join(failed)}")
            return Result(name, "skipped")
        if self.build_missing and not reused:
            return None
        why = "not in the remote storage" if self.use_remote else "not built, and the remote storage is not used"
        _show_failure(self.project, name, BuildError(f"{why}, for {self.project.arch}"), verbose=self.verbose)
        return Result(name, "failed")

    def _fetch_for(self, name: str) -> None:
        """Fetch from the remote storage what the build of `name` needs: the compiler cache of the storage, without
        one, and the builds of its dependencies only there, see Fetch.
        """
        project = self.project
        ccache.fetch_missing(project, name)
        try:
            for dep in sorted(reachable(project.deps, project.deps[name])):
                key = self.keys[dep]
                if not cached_build(project, dep, key).exists() and fetch_build(project, dep, key):
                    self.results[dep] = self._fetched(dep)
        except remote.RemoteError as e:
            console.print(f"[yellow]remote storage unavailable[/]: {escape(str(e))}")

    def _fetched(self, name: str) -> Result:
        """Record what rpm generated for the build of `name` just fetched, show it, and return its result."""
        record_rpm_deps(self.project, [name], self.keys, quiet=True)  # see _recorded_again()
        console.print(f"[green]↓[/] {name} [green]fetched[/]")
        return Result(name, "fetched")

    def _build_shown(self, name: str, status: str | None) -> Result:
        """Build `name` with _build_one(), and show how it went."""
        built_image(self.project)  # only when a package is to build, rather than cached, fetched or stored: failing all
        start = time.monotonic()
        try:
            self._build_one(name, status)
        except BuildRootChangedError as e:
            console.print(f"[yellow]↻[/] {name}: {escape(str(e))}")
            return Result(name, "again")
        except (OSError, RuntimeError) as e:
            _show_failure(self.project, name, e, verbose=self.verbose)
            return Result(name, "failed")
        console.print(f"[bold green]✓[/] {name} [green]built[/] in {duration(time.monotonic() - start)}")
        return Result(name, "built")

    def _build_one(self, name: str, status: str | None) -> None:
        """Build `name` and cache the result, showing `status` while it runs, or the build log without status.

        It only gets the builds of its dependencies. What its build found is
        recorded in the lock: its dynamic BuildRequires, even when it fails, and
        what rpm generated for its binary packages. It is not cached when its spec
        directory changed during the build, when its build root is not the one
        resolved for it, or when it did not make the packages parsed from its
        spec (see xcraft.checks). Raise
        BuildRootChangedError when it found what changes its build root, to build
        it again, after the packages it needs, or its build number. The keys are those of the
        `hashes` of the package directories at the start of the builds, computed
        again with them when the build changes the lock.
        """
        project = self.project
        if error := project.packages[name].error:
            raise BuildError(error)
        replace = image_replacements(resolved_root(project, name), name)
        repo = deps_repo(project, name, self.keys)
        tree = prepare_tree(project, name, self.hashes[name])
        suffix = release_suffix(self.keys[name])
        start = time.time()
        try:
            with _shown(project, name, status) as on_line:
                run_build(
                    project,
                    name,
                    repo,
                    "a",
                    replace=replace,
                    suffix=suffix,
                    nocheck=self.nocheck,
                    fast=name in self.autorev,
                    log=project.log(name),
                    on_line=on_line,
                )
        except ContainerError as e:
            # those it asked for before failing, maybe for one of ours not among its dependencies
            if (tree / "buildrequires.txt").exists() and record_dynamic(project, name, tree):
                msg = f"{e}, asking for other dynamic BuildRequires, now in the lock: built again after what they need"
                raise BuildRootChangedError(msg) from None
            raise
        ccache.note_build(project, name, start)
        keys = self._checked(name, tree, suffix)
        self._cache(name, keys[name], tree)
        record_rpm_deps(project, [name], keys, quiet=True)  # see _recorded_again()

    def _checked(self, name: str, tree: Path, suffix: str) -> dict[str, str]:
        """Check the build of `name` in `tree`, with the release `suffix`, see _build_one(), and return the keys, with
        what it recorded.
        """
        project, keys = self.project, self.keys
        if record_build(project, name, tree, suffix=suffix):
            keys = build_keys(project, self.autorev, self.hashes)  # covering what the build recorded
            if release_suffix(keys[name]) != suffix:
                msg = (
                    "its dynamic BuildRequires, now in the lock, change its build number: built again with the new one"
                )
                raise BuildRootChangedError(msg)
        if hash_dir(project.package_dirs[name], project.root) != self.hashes[name]:  # those of its key, staged
            msg = f"{project.package_dirs[name].relative_to(project.root)} changed during the build, build it again"
            raise BuildError(msg)
        return keys

    def _cache(self, name: str, key: str, tree: Path) -> None:
        """Move the build of `name` in `tree` to the cache, with its `key`, and the suffixes `autorev` when it is a
        development build, signed with the developer's key, if any.
        """
        project = self.project
        if name in self.autorev and (signer := dev_signer(project)):
            for output in ("RPMS", "SRPMS"):
                signer.sign_rpms(project, tree / output)
        build_dir = cached_build(project, name, key)
        # assembled next to it, and renamed, not to leave a partial build in the cache
        new = build_dir.with_name(f".{build_dir.name}")
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir(parents=True)
        for output in ("RPMS", "SRPMS"):
            (tree / output).rename(new / output)
        if name in self.autorev:
            # the suffixes of all the development builds of the run, its own and those of the dependencies built with it
            (new / "autorev").write_text(json.dumps(self.autorev, sort_keys=True))
        if self.nocheck:
            (new / NOCHECK).touch()
        shutil.copy2(project.log(name), new / "build.log")
        if build_dir.exists():  # an earlier build of the same inputs
            shutil.rmtree(new)
        else:
            new.rename(build_dir)
        shutil.rmtree(tree)

    def _recorded_again(self) -> list[Result]:
        """Record what rpm generated for the cached builds again, and return the results: packages built or fetched now
        may provide what they could not resolve before, which changes the keys of those built against them, then built
        again, like a change of the lock during the builds, or fetched again from the remote storage.
        """
        cached = [n for n, k in self.keys.items() if cached_build(self.project, n, k).exists()]
        if record_rpm_deps(self.project, cached, self.keys):
            self._refresh()
        return [r if self._holds(r) else Result(r.name, "again") for r in self.results.values()]

    def _holds(self, result: Result) -> bool:
        """Return whether `result` holds with the current keys: its build still cached, or in the remote storage."""
        key = self.keys[result.name]
        if result.status == "stored":
            return bool(self.stored.get(build_path(self.project, result.name, key)))
        return (
            result.status not in ("built", "fetched", "cached") or cached_build(self.project, result.name, key).exists()
        )


def build(
    project: Project,
    targets: list[str],
    *,
    with_deps: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
    dev: str | None = None,
    nocheck: bool = False,
    fetch: Fetch = "all",
    build_missing: bool = True,
) -> tuple[list[Result], dict[str, str], dict[str, str]]:
    """Build `targets` and, unless `with_deps` is false, what they need, skipping what is cached, and return how
    it went, with the keys of all the packages, those of their builds, and the suffixes of the development builds.

    `dev` is the identifier of the development builds, see xcraft.devbuilds, none for final builds. With `nocheck`,
    the packages built skip the tests of their specs, see xcraft.cache.untested(): those cached without them are
    built again otherwise. What is
    not cached locally is fetched from the remote storage when it has it, but
    only the builds of the dependencies of the packages to build with `fetch`
    needed, or nothing with none: it is built then. Without `build_missing`,
    what is neither cached nor in the remote
    storage fails, rather than being built. When builds find what changes the
    build roots of others, the builds are planned again, once. A package whose
    directory, or one of its dependencies', changed since the start fails: the
    builds work on the packages as they were then.

    The build IDs are updated before the builds, and after, with what they found, or checked with --locked: all the
    builds are numbered by them. The lock must be prepared: see resolution.prepare().
    """
    if not dry_run:
        update_build_ids(project)
    for attempt in (0, 1):
        keys, hashes, autorev = _record_cached(project, dev)
        plan = build_order(project.deps, targets, with_deps=with_deps)
        if dry_run:
            _show_plan(project, plan, keys, fetch=fetch)
            return [], keys, autorev
        _queue_file(project).parent.mkdir(parents=True, exist_ok=True)
        run = _Run(
            project,
            hashes,
            keys,
            autorev,
            dev=dev,
            nocheck=nocheck,
            verbose=verbose,
            fetch=fetch,
            build_missing=build_missing,
        )
        try:
            results = run.build(plan)
        finally:
            _queue_file(project).unlink(missing_ok=True)
        keys, autorev = run.keys, run.autorev
        if attempt or all(r.status != "again" for r in results):
            break
        console.print("the builds found what changes the build roots: planning them again")
    update_build_ids(project)  # with what the builds found
    # those to build again still, after the second attempt
    return [Result(r.name, "failed") if r.status == "again" else r for r in results], keys, autorev


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
            f" --pkg {n}"
            for name in names
            for n in project.packages[name].built_names(release_suffix(keys[name]))
            if not is_debug(n)
        )
    # without best=False, repoclosure only considers the newest version of each package, even when the
    # priority of our repository makes dnf install an older one
    checked.index()
    try:
        run_dnf(
            project,
            "repoclosure",
            f"--refresh repoclosure --setopt=best=False {check}",
            volumes=[f"{checked.path}:/xcraft-repo:ro"],
            show="checking dependencies",
            repos={"xcraft.repo": REPO_FILE.format(repo=repo_id(project))},
        )
    except ContainerError:
        for line in (project.state / "logs/_repoclosure.log").read_text().splitlines():
            if line.startswith(("package:", "  unresolved deps", "    ")):
                print_log_line(line)
        msg = "some dependencies cannot be satisfied"
        raise BuildError(msg) from None
