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
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from rich.markup import escape

from xcraft import buildid, buildkey, ccache, container, develop, lint, pinned, remote, resolution
from xcraft.builds import load_builds
from xcraft.cache import (
    NOCHECK,
    BuildError,
    build_keys,
    build_path,
    build_signers,
    builds_repo,
    cached_build,
    deps_repo,
    previous_duration,
    release_suffix,
    signed_build_dir,
    untested,
)
from xcraft.checks import BuildRootChangedError, record_build, record_dynamic, record_rpm_deps, resolved_root
from xcraft.container import ContainerError, built_image, run_dnf
from xcraft.devbuilds import dev_keys, keep_built
from xcraft.display import (
    Board,
    activity,
    board,
    console,
    diagnostics,
    duration,
    in_ci,
    log_step,
    print_log_line,
    print_log_lines,
    read_tail,
    tagged,
)
from xcraft.graph import build_order, reachable
from xcraft.localrepo import REPO_FILE, LocalRepo, repo_id
from xcraft.lock import lock_path
from xcraft.packages import is_debug
from xcraft.process import thread_pool
from xcraft.rpmbuild import build_container, image_replacements, prepare_tree, remove_tree, run_build
from xcraft.signing import KEY_FILE, run_signer

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
def _shown(project: Project, name: str, tag: int, *, whole: bool = False) -> Generator[Callable[[str], None]]:
    """Show the build log of `name`, given line by line to the function yielded: whole with `whole`, or only its errors
    and warnings, while it runs, on a line of the board of the builds on a terminal, and every minute in CI. The lines
    shown follow the name of the package, padded to `tag`, like those of the other builds.
    """
    if whole:
        yield tagged(name, tag, console)
        return
    with activity(
        name, expected=previous_duration(project, name), still=f"building {name}", announced=False
    ) as progress:
        diagnose = diagnostics(tagged(name, tag))

        def on_line(line: str) -> None:
            progress(line)
            diagnose(line)

        yield on_line


def fetch_build(project: Project, name: str, key: str) -> bool:
    """Download the build of `name` with `key` from the remote storage into the cache, signed by the first key of
    build_signers() it has, and return whether it has one.
    """
    with activity(f"downloading the build of {name}") as on_line:
        return any(
            remote.fetch_tree(
                project, build_path(project, name, key, f), signed_build_dir(project, f, name, key), on_line, f
            )
            for f in build_signers(project)
        )


def _stored_build(project: Project, name: str, key: str) -> bool:
    """Return whether the remote storage has the build of `name` with `key`, signed by a key of build_signers()."""
    return any(remote.tree_available(project, build_path(project, name, key, f), f) for f in build_signers(project))


def _split_plan(
    project: Project, plan: list[str], keys: dict[str, str], nocheck: set[str], *, fetch: Fetch
) -> tuple[list[str], set[str], set[str]]:
    """Return the packages of `plan` to build, those only in the remote storage, and those of them to fetch: those
    cached without the tests of their specs are built again, but those of `nocheck`.
    """
    to_build = [
        n
        for n in plan
        if not cached_build(project, n, keys[n]).exists() or (n not in nocheck and untested(project, n, keys[n]))
    ]
    stored = set()
    if fetch != "none" and project.config.remote:
        try:
            stored = {name for name in to_build if _stored_build(project, name, keys[name])}
        except remote.RemoteError as e:
            console.print(f"[yellow]remote storage unavailable[/]: {escape(str(e))}")
        to_build = [name for name in to_build if name not in stored]
    needed = reachable(project.deps, {d for n in to_build for d in project.deps[n]})
    return to_build, stored, stored if fetch == "all" else stored & needed


def _show_plan(project: Project, plan: list[str], keys: dict[str, str], nocheck: set[str], *, fetch: Fetch) -> None:
    """Show what is to build or fetch in `plan`, and how long it took last time, with how many are cached, or only
    in the remote storage, see _split_plan().
    """
    to_build, stored, fetched = _split_plan(project, plan, keys, nocheck, fetch=fetch)
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
        console.print("build IDs: " + ", ".join(f"{name} {ids[name].build}" for name in changed if name in ids))
    return changed


def _record_cached(project: Project, dev: str | None) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return the keys, the hashes of the package directories they have, and the suffixes of the development builds
    `dev`, with what rpm generated for the cached builds of these keys recorded first: restored from what they kept,
    without resolving it again but for those without it, which the builds do after them, see _recorded_again().
    """
    hashes = buildkey.directories(project)
    for _ in range(5):
        keys, autorev = dev_keys(project, dev, hashes)
        if not record_rpm_deps(
            project,
            [n for n, k in keys.items() if cached_build(project, n, k).exists()],
            keys,
            quiet=True,
            loose=True,
        ):
            return keys, hashes, autorev
        resolution.prepare(project)  # for what they recorded
    msg = "what the cached builds recorded keeps changing the keys"
    raise BuildError(msg)


def _stored(project: Project, names: list[str], keys: dict[str, str]) -> dict[str, bool]:
    """Return whether the remote storage has the builds of `names` with `keys`, by `<name>/<key>`, asked
    concurrently: nothing when it cannot be asked, as each one is asked again then.
    """
    try:
        with thread_pool(16) as pool:
            found = pool.map(lambda name: _stored_build(project, name, keys[name]), names)
            return {f"{name}/{keys[name]}": stored for name, stored in zip(names, found, strict=True)}
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
        print_log_lines(log_step(lines)[-40:])
        console.print(f"[dim]full log: xcraft log {name}[/]")


@dataclass
class _Started:
    """A build running in a thread of the run: what it needs to be checked and cached once done, on the main thread."""

    name: str
    tree: Path
    repo: LocalRepo
    replace: list[str]
    suffix: str
    start: float
    on_line: Callable[[str], None]
    shown: ExitStack
    """Its display, closed as soon as it is done."""
    turn: ExitStack = field(default_factory=ExitStack)
    """Its turn, see _turn(), closed once recorded."""
    wall_start: float = field(default_factory=time.time)
    """When it started, for ccache.note_build()."""


def _priorities(project: Project, plan: list[str]) -> dict[str, float]:
    """Return the priority of each package of `plan` to start: the expected duration of the longest chain of builds
    it starts, itself and the packages built against it, from the durations of their last builds.
    """
    durations = {n: previous_duration(project, n) or 60 for n in plan}
    built_against: dict[str, set[str]] = {n: set() for n in plan}
    for name in plan:
        for dep in project.deps[name]:
            if dep in built_against:
                built_against[dep].add(name)
    priorities: dict[str, float] = {}
    for name in reversed(plan):
        priorities[name] = durations[name] + max((priorities.get(d, 0) for d in built_against[name]), default=0)
    return priorities


def default_jobs() -> int:
    """Return how many packages build at once by default: a third of the processors, rounded, as a build alone keeps
    about 4 of them busy on average, its configure script, links, tests and packaging mostly on one, measured with 8
    processors; and one by 4 GiB of memory at most, as each one runs its compilers on all the processors, and links
    the large packages with LTO.
    """
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**30
    except (ValueError, OSError):  # not known
        memory = 2**10
    return max(1, min(round((os.cpu_count() or 1) / 3), memory // 4))


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
    lint_dev: bool = False
    jobs: int = 1
    """How many packages build at once, see build()."""
    results: dict[str, Result] = field(default_factory=dict)
    stored: dict[str, bool] = field(default_factory=dict)
    """Whether the remote storage has the builds, by `<name>/<key>`."""
    use_remote: bool = field(default=False, init=False)
    recorded: bytes = field(default=b"", init=False)
    """The state of the lock of the `keys`, see _lock_state()."""
    unrecorded: list[str] = field(default_factory=list, init=False)
    """The packages fetched, whose builds are recorded together, see _record_fetched()."""
    step: int = field(default=0, init=False)
    """The packages built so far, for their step numbers."""
    steps: int = field(default=0, init=False)
    """The packages of the plan to build, as known so far, for their step numbers, see _to_build()."""
    linted: dict[str, bool] = field(default_factory=dict, init=False)
    """Whether rpmlint found errors in the specs of the packages to build, see _lint_specs()."""
    board: Board | None = field(default=None, init=False)
    """The board of the builds, on a terminal, see display.board()."""
    tag: int = field(default=0, init=False)
    """The width of the names of the packages, which their lines follow, see _shown()."""
    expected: dict[str, float] = field(default_factory=dict, init=False)
    """The expected durations of the packages to build, as known at the start, which the board shows the progress of."""
    pending: list[str] = field(default_factory=list, init=False)
    """The packages of the plan not started yet, the one starting the longest chain of builds first."""
    running: dict[Future[None], _Started] = field(default_factory=dict, init=False)
    """The builds running, by their futures."""
    needs: dict[str, set[str]] = field(default_factory=dict, init=False)
    """The packages of the plan each one is built against, at the start, for the time left, see _left()."""

    def build(self, plan: list[str]) -> list[Result]:
        """Build the packages of `plan`, `jobs` at once, and return how it went, see xcraft.build.build().

        A package starts once the packages of `plan` it needs are done, the one starting the longest chain of builds
        first. The builds only run their containers in threads: the main thread does the rest, one package at a time,
        like the recordings in the lock, and the checks and caching of the builds.
        """
        project = self.project
        self.use_remote = self.fetch != "none" and bool(project.config.remote)
        reused = {name for name, i in buildid.read(project).items() if i.accepts}
        if self.use_remote and self.fetch == "needed":  # asked at once, with the keys of the start
            missing = [name for name in plan if not cached_build(project, name, self.keys[name]).exists()]
            self.stored = _stored(project, missing, self.keys)
        self.recorded = _lock_state(project)
        self.step = 0
        self.steps = sum(self._to_build(name) for name in plan)
        # those surely built, before their builds; the others at their turn, unless fetched
        self._lint_specs([n for n in plan if self._to_build(n) and (n in self.autorev or not self.use_remote)])
        self.tag = max(map(len, plan), default=0)
        priorities = _priorities(project, plan)
        pending = self.pending = sorted(plan, key=lambda n: -priorities[n])
        running = self.running
        self.needs = {n: project.deps[n] & set(plan) for n in plan}
        self.expected = {n: previous_duration(project, n) or 0 for n in plan if self._to_build(n)}
        with (
            ThreadPoolExecutor(self.jobs) as pool,
            board("Building", sum(self.expected.values()), self.jobs, left=self._left) as self.board,
        ):
            try:
                while pending or running:
                    while len(running) < self.jobs and (name := self._startable(pending, set(plan), running)):
                        pending.remove(name)
                        _queue_file(project).write_text("\n".join([str(os.getpid()), *pending]) + "\n")
                        started = self._start(name, reused=name in reused)
                        if isinstance(started, Result):
                            self._done_with(started)
                        else:
                            running[pool.submit(self._run_build, started)] = started
                        self._summarize()
                    if running:
                        for future in wait(running, return_when=FIRST_COMPLETED)[0]:
                            self._done_with(self._finish(running.pop(future), future))
                            self._summarize()
            except BaseException:
                for started in running.values():
                    container.kill(build_container(project, started.name))
                wait(running)
                for started in running.values():
                    started.shown.close()
                    started.turn.close()
                raise
        self._refresh()
        return self._recorded_again()

    def _startable(self, pending: list[str], plan: set[str], running: dict[Future[None], _Started]) -> str | None:
        """Return the first package of `pending` whose packages of the `plan` it needs are done, if any, or the first
        one when nothing is `running`, as before the builds ran at once.
        """
        if ready := next((n for n in pending if all(d in self.results for d in self.project.deps[n] & plan)), None):
            return ready
        return pending[0] if pending and not running else None

    def _left(self) -> float:
        """Return the expected time left: that of the builds running, and of those pending started as they would be,
        `jobs` at once, once what they are built against is done, the longest chains first, from the durations of their
        last builds. Called by the board, in its thread: on copies of what the main thread changes.
        """
        now, clock, done = time.monotonic(), 0.0, set(self.results)
        running = {s.name: max(self.expected.get(s.name, 0) - (now - s.start), 0) for s in list(self.running.values())}
        pending = list(self.pending)
        while pending or running:
            for name in [n for n in pending if self.needs[n] <= done][: self.jobs - len(running)]:
                pending.remove(name)
                running[name] = self.expected.get(name, 0)
            if not running:  # what is pending waits for what nothing runs, as the builds would not
                break
            step = min(running.values())
            clock += step
            for name, left in list(running.items()):
                if left <= step:
                    del running[name]
                    done.add(name)
                else:
                    running[name] = left - step
        return clock

    def _done_with(self, result: Result) -> None:
        """Take the `result` of a package, counting its expected duration as done on the board when it was to build and
        is built, or out of its total when it is not, like fetched from the remote storage, or skipped.
        """
        self.results[result.name] = result
        if self.board and (expected := self.expected.pop(result.name, 0)):
            if result.status == "built":
                self.board.done += expected
            else:
                self.board.total -= expected

    def _summarize(self) -> None:
        """Show the counts of the builds on the board: those finished out of those to build, like their steps, without
        the packages cached, nor those fetched instead, and those failed, if any: the running ones have their lines.
        """
        if self.board:
            counts = Counter(r.status for r in self.results.values())
            failed = counts["failed"] + counts["skipped"]
            self.board.summary = f"{self.step - len(self.running)}/{self.steps}" + (
                f", [red]{failed} failed[/]" if failed else ""
            )

    def _start(self, name: str, *, reused: bool) -> Result | _Started:
        """Return the result of `name`, unless it is to build: then start its build, and return it, see _finish(). It
        is not built when it is done, in the remote storage, or not to build, like when `reused`, the build of the
        main branch, made by CI. It counts in the steps the board shows, the packages to build: those counted to build
        but not built, like those fetched or skipped, are counted out.
        """
        project = self.project
        turn = ExitStack()
        turn.enter_context(_turn(project, name))
        try:
            self._refresh()
            counted = self._to_build(name)
            if result := self._done(name) or self._from_storage(name) or self._not_built(name, reused=reused):
                self.steps -= counted
                return result
            if result := self._lint_failed(name):
                self.steps -= counted
                return result
            self.step += 1
            self.steps = max(self.steps, self.step)  # or more than counted, when the keys changed
            if self.use_remote:
                self._fetch_for(name)
            self._record_fetched()  # for its build root, with what the builds of its dependencies generated
            if isinstance(launched := self._launch(name), _Started):
                launched.turn = turn.pop_all()
            return launched
        finally:
            turn.close()

    def _launch(self, name: str) -> Result | _Started:
        """Start the build of `name`, see _run_build(), and return it, or its failure."""
        project = self.project
        built_image(project)  # only when a package is to build, rather than cached, fetched or stored: failing all
        start = time.monotonic()
        try:
            pinned.repository(project)  # here, for the build: checking it runs a container of its own, once
            if error := project.packages[name].error:
                raise BuildError(error)
            replace = image_replacements(resolved_root(project, name), name)
            tree = prepare_tree(project, name, self.hashes[name])
            repo = deps_repo(project, name, self.keys)
            repo.index()  # here: indexing runs a container of its own
        except (OSError, RuntimeError, ValueError) as e:
            _show_failure(project, name, e, verbose=self.verbose)
            return Result(name, "failed")
        shown = ExitStack()
        on_line = shown.enter_context(_shown(project, name, self.tag, whole=self.verbose))
        return _Started(name, tree, repo, replace, release_suffix(self.keys[name]), start, on_line, shown)

    def _run_build(self, started: _Started) -> None:
        """Run the build of `started`, in a thread: against the builds of its dependencies, in its repository."""
        project, name = self.project, started.name
        run_build(
            project,
            name,
            started.repo,
            "a",
            replace=started.replace,
            suffix=started.suffix,
            nocheck=self._nocheck(name),
            fast=name in self.autorev,
            log=project.log(name),
            on_line=started.on_line,
        )

    def _lint_specs(self, names: list[str]) -> None:
        """Lint the specs of `names`, but those that cannot be parsed, which fail to build, and show what rpmlint
        found.
        """
        project = self.project
        if names := [n for n in names if n not in self.linted and not project.packages[n].error]:
            self.linted |= lint.findings(project, {n: [lint.spec(project, n)] for n in names}, show="linting the specs")

    def _lint_failed(self, name: str) -> Result | None:
        """Return the result of `name` when rpmlint found errors in its spec, failed for a final build: a development
        build only shows them.
        """
        self._lint_specs([name])
        if name in self.autorev or not self.linted.get(name):
            return None
        _show_failure(self.project, name, BuildError("rpmlint found errors in its spec"), verbose=self.verbose)
        return Result(name, "failed")

    def _lint_binaries(self, name: str, tree: Path) -> None:
        """Lint the binary packages of the build of `name` in `tree`, those of the final builds, or of all with
        `lint_dev`, and show what rpmlint found: raise for the errors of a final build, not to cache it.
        """
        if name in self.autorev and not self.lint_dev:
            return
        files = {name: lint.binaries(tree / "RPMS")}
        failed = lint.findings(self.project, files, show=f"linting the packages of {name}")[name]
        if name not in self.autorev and failed:
            msg = "rpmlint found errors in its packages"
            raise BuildError(msg)

    def _nocheck(self, name: str) -> bool:
        """Return whether `name` is built without the tests of its spec: a development build with `nocheck`, while
        the final builds, which may be pushed and published, always run them.
        """
        return self.nocheck and name in self.autorev

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
            n for n in sorted(reachable(project.deps, [name])) if buildkey.directory(project, n) != self.hashes[n]
        ]:
            error = BuildError(f"{', '.join(edited)} changed since the builds started: build again")
            _show_failure(project, name, error, verbose=self.verbose)
            return Result(name, "failed")
        return Result(name, "cached") if self._cached(name) else None  # nothing to show: only the counts

    def _to_build(self, name: str) -> bool:
        """Return whether `name` is to build, as known now: not cached, nor known in the remote storage."""
        return not self._cached(name) and not self.stored.get(f"{name}/{self.keys[name]}", False)

    def _cached(self, name: str) -> bool:
        """Return whether `name` has a cached build to use: with the tests its spec asks for, unless --nocheck."""
        if not cached_build(self.project, name, self.keys[name]).exists():
            return False
        return self._nocheck(name) or not untested(self.project, name, self.keys[name])

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
            if (build := f"{name}/{self.keys[name]}") not in self.stored:
                self.stored[build] = _stored_build(self.project, name, self.keys[name])
            return Result(name, "stored") if self.stored[build] else None
        except remote.RemoteError as e:
            console.print(f"[yellow]remote storage unavailable, building locally[/]: {escape(str(e))}")
            self.use_remote = False
            return None

    def _not_built(self, name: str, *, reused: bool) -> Result | None:
        """Return the result of `name` when it is not to build: skipped, as dependencies failed or are to build
        again, or failed without `build_missing`, or when `reused`.
        """
        if failed := sorted(dep for dep in self.project.deps[name] if dep in self.results and self.results[dep].failed):
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
        """Show the build of `name` just fetched, and return its result: recorded later, see _record_fetched()."""
        self.unrecorded.append(name)
        console.print(f"[green]↓[/] {name} [green]fetched[/]")
        return Result(name, "fetched")

    def _record_fetched(self) -> None:
        """Record what rpm generated for the builds fetched since the last time, together, before a build, which may
        need them, and else at the end, see _recorded_again(): once, rather than after each fetch, with only some of
        our builds there, which what they generated may resolve to. Their builds are the main branch's, which CI
        recorded, with all ours there too, like in a new checkout once all are fetched.
        """
        if self.unrecorded:
            record_rpm_deps(self.project, self.unrecorded, self.keys, quiet=True)
            self.unrecorded.clear()
            self._refresh()

    def _finish(self, started: _Started, future: Future[None]) -> Result:
        """Return the result of the build of `started`, done in `future`, once checked and cached, and show it.

        It only got the builds of its dependencies. What its build found is
        recorded in the lock: its dynamic BuildRequires, even when it fails, and
        what rpm generated for its binary packages. It is not cached when its spec
        directory changed during the build, when its build root is not the one
        resolved for it, or when it did not make the packages parsed from its
        spec (see xcraft.checks). It is to build again when it found what changes
        its build root, after the packages it needs, or its build number. The keys
        are those of the `hashes` of the package directories at the start of the
        builds, computed again with them when the build changes the lock.
        """
        project, name, tree = self.project, started.name, started.tree
        started.shown.close()
        try:
            try:
                future.result()
            except ContainerError as e:
                # those it asked for before failing, maybe for one of ours not among its dependencies
                if (tree / "buildrequires.txt").exists() and record_dynamic(project, name, tree):
                    msg = f"{e}, asking for other dynamic BuildRequires, now in the lock: built again after "
                    msg += "what they need"
                    raise BuildRootChangedError(msg) from None
                raise
            ccache.note_build(project, name, started.wall_start)
            keys = self._checked(name, tree, started.suffix)
            self._lint_binaries(name, tree)
            self._cache(name, keys[name], tree)
            record_rpm_deps(project, [name], keys, quiet=True)  # see _recorded_again()
        except BuildRootChangedError as e:
            console.print(f"[yellow]↻[/] {name}: {escape(str(e))}")
            return Result(name, "again")
        except (OSError, RuntimeError, ValueError) as e:  # ValueError: like invalid JSON the build wrote
            _show_failure(project, name, e, verbose=self.verbose)
            return Result(name, "failed")
        finally:
            started.turn.close()
        console.print(f"[bold green]✓[/] {name} [green]built[/] in {duration(time.monotonic() - started.start)}")
        keep_built(project, self.dev, self.autorev, [name], self.hashes)
        return Result(name, "built")

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
        if buildkey.directory(project, name) != self.hashes[name]:  # those of its key, staged
            msg = f"{project.package_dirs[name].relative_to(project.root)} changed during the build, build it again"
            raise BuildError(msg)
        return keys

    def _cache(self, name: str, key: str, tree: Path) -> None:
        """Move the build of `name` in `tree` to the cache, with its `key`, and the suffixes `autorev` when it is a
        development build, signed by the key of the command, see xcraft.signing.run_signer(), once rpmbuild is gone:
        the specs never get near the key.
        """
        project, signer = self.project, run_signer(self.project)
        signer.sign_rpms(project, tree / "RPMS", tree / "SRPMS")
        build_dir = signed_build_dir(project, signer.key, name, key)
        # assembled next to it, and renamed, not to leave a partial build in the cache
        new = build_dir.with_name(f".{build_dir.name}")
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir(parents=True)
        for output in ("RPMS", "SRPMS"):
            (tree / output).rename(new / output)
        (new / KEY_FILE).write_text(signer.public_key())
        if name in self.autorev:
            # the suffixes of all the development builds of the run, its own and those of the dependencies built with it
            (new / "autorev").write_text(json.dumps(self.autorev, sort_keys=True))
        if self._nocheck(name):
            (new / NOCHECK).touch()
        shutil.copy2(project.log(name), new / "build.log")
        if (build_dir / NOCHECK).exists() and not (new / NOCHECK).exists():
            shutil.rmtree(build_dir)  # replaced by the build with the tests of its spec
        if build_dir.exists():  # an earlier build of the same inputs
            shutil.rmtree(new)
        else:
            new.rename(build_dir)
        remove_tree(project, name)

    def _recorded_again(self) -> list[Result]:
        """Record what rpm generated for the cached builds again, and return the results: packages built or fetched now
        may provide what they could not resolve before, which changes the keys of those built against them, then built
        again, like a change of the lock during the builds, or fetched again from the remote storage.
        """
        self.unrecorded.clear()  # recorded with all the others
        cached = [n for n, k in self.keys.items() if cached_build(self.project, n, k).exists()]
        if record_rpm_deps(self.project, cached, self.keys):
            self._refresh()
        return [r if self._holds(r) else Result(r.name, "again") for r in self.results.values()]

    def _holds(self, result: Result) -> bool:
        """Return whether `result` holds with the current keys: its build still cached, or in the remote storage."""
        key = self.keys[result.name]
        if result.status == "stored":
            return bool(self.stored.get(f"{result.name}/{key}"))
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
    lint_dev: bool = False,
    jobs: int | None = None,
) -> tuple[list[Result], dict[str, str], dict[str, str]]:
    """Build `targets` and, unless `with_deps` is false, what they need, skipping what is cached, and return how
    it went, with the keys of all the packages, those of their builds, and the suffixes of the development builds.

    `dev` is the identifier of the development builds, see xcraft.devbuilds, none for final builds. With `nocheck`,
    the development builds skip the tests of their specs, see xcraft.cache.untested(): those cached without them are
    built again otherwise; the final builds, like those of the dependencies the remote storage misses, run them.

    What is not cached locally is fetched from the remote storage when it has it, but only the builds of the
    dependencies of the packages to build with `fetch` needed, or nothing with none: it is built then. Without
    `build_missing`, what is neither cached nor in the remote storage fails, rather than being built. When builds find
    what changes the build roots of others, the builds are planned again, once. A package whose directory, or one of
    its dependencies', changed since the start fails: the builds work on the packages as they were then.

    The build IDs are updated before the builds, and after, with what they found, or checked with --locked: all the
    builds are numbered by them. The lock must be prepared: see resolution.prepare().

    rpmlint lints the specs of the packages to build first, and the binary packages of the final builds, or of all
    with `lint_dev`, see xcraft.lint: its errors fail the final builds, and only show for the development builds.

    `jobs` packages build at once, default_jobs() by default, those independent of each other, see _Run.build().
    """
    if dev is None:
        develop.check_none(project, "final builds")
    if not dry_run:
        update_build_ids(project)
    for attempt in (0, 1):
        keys, hashes, autorev = _record_cached(project, dev)
        plan = build_order(project.deps, targets, with_deps=with_deps)
        if dry_run:
            _show_plan(project, plan, keys, set(autorev) if nocheck else set(), fetch=fetch)
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
            lint_dev=lint_dev,
            jobs=jobs or default_jobs(),
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


def make_repo(project: Project, names: list[str], dest: Path, keys: dict[str, str], *, alone: bool = False) -> None:
    """Make `dest` a repository with the builds of `names`, with their `keys`, and the extra upstream RPMs.

    Check that everything they require is available, from this repository or from the
    build environment's ones. With `alone`, the repository only has them, to install over a host's packages, and
    they are checked with our other builds.
    """
    if missing := [name for name in names if not cached_build(project, name, keys[name]).exists()]:
        msg = f"not built: {', '.join(missing)}"
        raise BuildError(msg)
    repo = LocalRepo(project, dest)
    repo.reset(extra_upstream=not alone)
    for name in names:
        repo.add(cached_build(project, name, keys[name]))
    repo.index()
    checked, check = repo, f"--check {repo_id(project)}"
    if alone:
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
