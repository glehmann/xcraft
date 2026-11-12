"""Build IDs: the numbers of the builds of our packages, with the build keys they are built from.

A package's build ID is in the lock of the builds, see xcraft.builds: its number and build key. The build key is the
same on all the architectures: it covers what goes into the package on any of them, from the package directory, the
recipe and the locks, and the build IDs of the packages of ours it is built against, but not its own number, which it
decides.
A package keeps the build ID of the main branch, where the current branch forks from it, as long as its key is the
same, and gets the next number otherwise. On a maintenance branch, or a branch of one, the next number is a
maintenance one, +b<n>.<m>, after the release's +b<n>, see docs/embargo-design.md.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from rpm_version import evr_compare  # ty: ignore[unresolved-import]  # pyright: ignore[reportAttributeAccessIssue]

from xcraft import buildkey, process
from xcraft.builds import BuildId, builds_path, load_builds, parse_builds, save_builds
from xcraft.display import console
from xcraft.graph import components
from xcraft.lock import load_lock, lock_path, parse_lock

if TYPE_CHECKING:
    from collections.abc import Callable

    from xcraft.builds import Builds
    from xcraft.project import Project


class BuildIdError(RuntimeError):
    """Raised when the build IDs would change, which --locked forbids, or give a number to two different builds."""


def read(project: Project) -> dict[str, BuildId]:
    """Return the build IDs of the working tree, but those of a merge conflict, which update() gives again."""
    return load_builds(project).ids


def _git(project: Project, *args: str) -> str | None:
    result = process.run(["git", "-C", str(project.root), *args], check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _jj(project: Project, revset: str, template: str) -> str | None:
    """Return what jj prints of `revset` with `template`, when the checkout is one of jj, colocated with git: None
    otherwise. jj snapshots its working copy first.
    """
    if not (top := _git(project, "rev-parse", "--show-toplevel")) or not (Path(top) / ".jj").is_dir():
        return None
    if not shutil.which("jj"):
        return None
    found = process.run(["jj", "-R", top, "log", "-r", revset, "--no-graph", "-T", template], check=False, capture=True)
    return found.stdout if found.returncode == 0 else None


def commit(project: Project) -> str | None:
    """Return the commit of the checkout, None out of a git repository: with jj, the commit of its working copy, which
    it snapshots, or its parent's while it is empty, as git sees it; with git, HEAD, as <commit>-dirty with changes.
    """
    if found := _jj(project, "@", 'commit_id ++ " " ++ empty ++ " " ++ parents.map(|c| c.commit_id()).join(" ")'):
        working, empty, *parents = found.split()
        return parents[0] if empty == "true" and len(parents) == 1 else working
    head = _git(project, "rev-parse", "-q", "--verify", "HEAD")
    return head and (f"{head}-dirty" if _git(project, "status", "--porcelain") else head)


def _builds_at(project: Project, commit: str) -> Builds:
    return parse_builds(_git(project, "show", f"{commit}:./{builds_path(project).name}") or "")


_commits: dict[tuple[str, str], dict[str, BuildId]] = {}


def _at(project: Project, commit: str) -> dict[str, BuildId]:
    """Return the build IDs of `commit`, a commit id, read once."""
    if (str(project.root), commit) not in _commits:
        _commits[str(project.root), commit] = _builds_at(project, commit).ids
    return _commits[str(project.root), commit]


def _main(project: Project) -> str | None:
    """Return the commit of the main branch: the remote's, as last fetched, or else the local one, if any."""
    branch = project.config.main_branch
    return next(
        filter(None, (_git(project, "rev-parse", "-q", "--verify", ref) for ref in (f"origin/{branch}", branch))), None
    )


_RELEASE_TAGS = "refs/tags/release/"
"""The tags of the published releases, which keep the commits of the maintenance branches removed since."""


def _branches(project: Project, patterns: list[str], *refs: str) -> dict[str, str]:
    """Return the commits of the branches of `patterns`, and of the `refs`, by name: the remote's, as last fetched, or
    else the local ones.
    """
    if not (patterns or refs):
        return {}
    named = [*(f"refs/{p}/{pattern}" for pattern in patterns for p in ("heads", "remotes/origin")), *refs]
    found, listed = {}, _git(project, "for-each-ref", "--format=%(refname) %(*objectname) %(objectname)", *named)
    for line in (listed or "").splitlines():
        ref, commit = line.split()[:2]  # the commit of an annotated tag, else of the ref
        found[ref.removeprefix("refs/heads/").removeprefix("refs/remotes/origin/")] = commit
    return found


def maintenance(project: Project) -> dict[str, str]:
    """Return the commits of the maintenance branches, and of the release tags, by name, see _branches()."""
    return _branches(project, project.config.maintenance_branches, _RELEASE_TAGS)


_chains: dict[tuple[str, str], frozenset[str]] = {}


def _chain(project: Project, commit: str) -> frozenset[str]:
    """Return the commits of the first-parent chain of `commit`, a commit id, read once."""
    if (str(project.root), commit) not in _chains:
        chain = _git(project, "rev-list", "--first-parent", commit) or ""
        _chains[str(project.root), commit] = frozenset(chain.split())
    return _chains[str(project.root), commit]


def _owner(project: Project, commit: str) -> str | None:
    """Return the official branch whose first-parent chain brought `commit`: the main branch, for its chain, else a
    maintenance branch, or a release tag, for the commits of its chain the main branch's does not have; None for the
    commits of no official branch.
    """
    if (main := _main(project)) and commit in _chain(project, main):
        return project.config.main_branch
    return next((name for name, c in maintenance(project).items() if commit in _chain(project, c)), None)


def _ancestor(project: Project, commit: str, of: str) -> bool:
    return _git(project, "merge-base", "--is-ancestor", commit, of) is not None


class _Fork(NamedTuple):
    commit: str
    maintenance: bool
    """Whether it is a commit of a maintenance branch, rather than of the main branch."""
    others: tuple[str, ...] = ()
    """The fork points with the maintenance branches the current branch merges, whose build IDs it may keep."""
    embargoed: bool = False
    """Whether it is a commit of an embargo branch, see embargo_branches of xcraft.toml."""
    branch: str = ""
    """The branch it is a commit of: the main one, or a maintenance one, or a release tag."""


def _current(project: Project) -> list[str]:
    """Return the branches the checkout is on: with jj, the nearest bookmarks of the main and maintenance branches, on
    its working copy, or else on its closest ancestors, like `jj new maint/9.0` makes; or else git's, whose HEAD jj may
    leave on a branch, after jj git init.
    """
    names = [f"exact:{json.dumps(project.config.main_branch)}"]
    names += [f"glob:{json.dumps(pattern)}" for pattern in project.config.maintenance_branches]
    revset = f"heads(::@ & ({' | '.join(f'bookmarks({n})' for n in names)}))"
    if (found := _jj(project, revset, 'local_bookmarks.map(|b| b.name()).join(" ") ++ " "')) is not None:
        return found.split()
    return list(filter(None, [_git(project, "symbolic-ref", "-q", "--short", "HEAD")]))


target: str | None = None
"""The branch the builds are numbered against, the main one or a maintenance one, given by xcraft --maintenance, like
the target of a pull request in CI, rather than told by the fork points: see _fork()."""


def _fork(project: Project) -> _Fork | None:
    """Return the fork point of the current branch, if any: where it forks from the main branch, or from a
    maintenance branch when that is the most recent of its fork points, which descends from the others; with a merge in
    progress, where the merge forks from them. The tip of a maintenance branch without commits of its own yet, like
    just made, at the release, is one of its commits, but while the main branch is there too.

    With a `target`, or $XCRAFT_MAINTENANCE, or else on a maintenance branch, see _current(), the fork point is that of
    the branch it names.
    """
    if not (main := _main(project)):
        return None
    heads = ["HEAD", *filter(None, [_git(project, "rev-parse", "-q", "--verify", "MERGE_HEAD")])]
    if not (fork := _git(project, "merge-base", main, *heads)):
        return None
    branches = maintenance(project)
    name = (
        target
        or os.environ.get("XCRAFT_MAINTENANCE")
        or next((b for b in _current(project) if b in branches and not b.startswith(_RELEASE_TAGS)), None)
    )
    embargoes = _branches(project, project.config.embargo_branches).keys()
    if name and name != project.config.main_branch:
        if name not in branches or name.startswith(_RELEASE_TAGS):
            msg = f"no maintenance branch {name}, of {', '.join(project.config.maintenance_branches)}, fetched"
            raise BuildIdError(msg)
        forked = _git(project, "merge-base", branches[name], *heads) or fork
        return _Fork(forked, maintenance=True, embargoed=name in embargoes, branch=name)
    maintained: dict[str, set[str]] = {}
    for ref, c in branches.items():
        if (f := _git(project, "merge-base", c, *heads)) and (
            _owner(project, f) not in (None, project.config.main_branch)
            or (f == c and c != main and not ref.startswith(_RELEASE_TAGS))
        ):
            maintained.setdefault(f, set()).add(ref)
    if not name and (after := [f for f in maintained if _ancestor(project, fork, f)]):
        forked = next(f for f in sorted(after) if all(_ancestor(project, g, f) for g in after))
        refs = sorted(maintained[forked], key=lambda ref: (ref.startswith(_RELEASE_TAGS), ref))  # branches first
        return _Fork(forked, maintenance=True, embargoed=bool(maintained[forked] & embargoes), branch=refs[0])
    return _Fork(
        fork,
        maintenance=False,
        others=tuple(sorted(f for f in maintained if not _ancestor(project, f, fork))),
        branch=project.config.main_branch,
    )


def embargoed(project: Project) -> bool:
    """Return whether the current branch is an embargo branch, or one of its branches, see _fork(): its builds go to
    the private remote storage only.
    """
    return bool((fork := _fork(project)) and fork.embargoed)


class Base(NamedTuple):
    """The build IDs the current ones are numbered against, see compute()."""

    ids: dict[str, BuildId]
    """Those of the fork point, see _fork()."""
    maintenance: bool = False
    """Whether they are of a maintenance branch, numbered +b<n>.<m>."""
    others: tuple[dict[str, BuildId], ...] = ()
    """Those of the maintenance branches merged, kept for the same keys."""


def fork_branch(project: Project) -> str | None:
    """Return the official branch the current one forks from, see _fork(): None out of a git repository, or without
    the main branch.
    """
    return fork.branch if (fork := _fork(project)) else None


def base(project: Project) -> Base | None:
    """Return the build IDs of the official branch where the current one forks from it, see _fork(), or None out of a
    git repository, or without the main branch.
    """
    if not (fork := _fork(project)):
        return None
    return Base(_at(project, fork.commit), fork.maintenance, tuple(_at(project, c) for c in fork.others))


def _official(project: Project) -> bool:
    """Return whether the current commit is one of an official branch, whose build IDs are those of final builds: of
    the main branch, or of a maintenance one.
    """
    return bool(head := _git(project, "rev-parse", "-q", "--verify", "HEAD")) and _owner(project, head) is not None


def _evr(project: Project, name: str) -> str | None:
    """Return the epoch-version-release of the package `name`, without %dist, if its spec is parsed."""
    package = project.parsed.get(name)
    return package.binaries[0].evr.removesuffix(package.dist) if package and package.binaries else None


def _newer(evr: str | None, than: str | None) -> bool:
    """Return whether the epoch-version-release `evr` is greater than `than`, both known: its builds start at 1."""
    return bool(evr and than) and evr_compare(evr, than) > 0


def check_first_parent(project: Project, ids: dict[str, BuildId]) -> None:
    """On an official branch, check that the packages whose build key changed since the first parent of the current
    commit have greater numbers, or greater epoch-version-releases: one number is never given to two different builds
    of an EVR, even by a merge conflict resolved by keeping one side. On the main branch, check that the maintenance
    numbers come from the maintenance branches it merges, the other parents, with the same keys, rather than from a
    fast-forward or a squash of one: the maintenance branches, or their release tags, must be fetched.
    """
    if not (head := _git(project, "rev-parse", "-q", "--verify", "HEAD")) or not (owner := _owner(project, head)):
        return
    if not (parents := (_git(project, "rev-parse", "HEAD^@") or "").split()):
        console.print("[yellow]warning[/]: the build IDs are not checked against the previous commit, not fetched")
        return
    main = project.config.main_branch
    previous = _at(project, parents[0])
    merged = [_at(project, p) for p in parents[1:] if _owner(project, p) not in (None, main)]
    if reused := sorted(
        name
        for name, i in ids.items()
        if (p := previous.get(name)) and p.key != i.key and i.order <= p.order and not _newer(i.evr, p.evr)
    ):
        msg = f"build numbers given again to other builds, since the previous commit: {', '.join(reused)}"
        raise BuildIdError(msg)
    if owner == main and (
        unmerged := sorted(
            name
            for name, i in ids.items()
            if i.sub and not i.same(previous.get(name)) and not any(i.same(m.get(name)) for m in merged)
        )
    ):
        msg = (
            f"maintenance build numbers on {owner} not from the maintenance branches it merges: {', '.join(unmerged)}; "
            "merge them with a merge commit, rather than a fast-forward or a squash, and fetch them"
        )
        raise BuildIdError(msg)


def update(project: Project, reuse: Callable[[str, dict[str, BuildId]], bool] | None = None) -> list[str]:
    """Write the build IDs of the packages, and return those changed, those `reuse` takes reusing their builds, see
    compute(). When the project is frozen, fail instead if they would change, or, on the main branch, if they reuse the
    numbers of the previous commit for other builds. Fail when a package changed gets a lower epoch-version-release
    than the main branch's, whose numbers may have been given before.
    """
    fork, current = base(project), read(project)
    ids = compute(project, fork, reuse=reuse)
    changed = sorted(name for name in ids.keys() | current.keys() if ids.get(name) != current.get(name))
    forked = fork.ids if fork else {}
    if lowered := sorted(n for n in changed if n in ids and (f := forked.get(n)) and _newer(f.evr, ids[n].evr)):
        evrs = ", ".join(f"{n} {forked[n].evr} to {ids[n].evr}" for n in lowered)
        msg = (
            f"the epoch-version-release goes down, from that of {project.config.main_branch}: {evrs}; its builds could "
            "get the numbers of earlier ones, and the hosts would not upgrade to them: raise the epoch, or the release"
        )
        raise BuildIdError(msg)
    if project.frozen:
        if changed:
            msg = f"the build IDs would change, which --locked forbids: {', '.join(changed)}"
            raise BuildIdError(msg)
        check_first_parent(project, ids)
    if changed:
        with project.lock():
            builds = load_builds(project)
            builds.ids = ids
            save_builds(project, builds)
    return changed


def reuse(project: Project, names: list[str], sources: list[str]) -> list[str]:
    """Reuse the builds of the main branch where the current one forks from it, despite changes, of the packages
    `names`, but those whose directories changed, and of those whose only changes are updates of the upstream source
    packages `sources`, and return those changed.

    The packages built against them keep their builds too, unless they changed otherwise.
    """
    if not (found := _fork(project)):
        msg = f"reusing builds needs the main branch, {project.config.main_branch}, in a git repository"
        raise BuildIdError(msg)
    fork = found.commit
    forked, forked_builds = _at(project, fork), _builds_at(project, fork)
    forked_lock = parse_lock(_git(project, "show", f"{fork}:./{lock_path(project).name}") or "")
    lock, builds, updated = load_lock(project), load_builds(project), set(sources)
    # the inputs of the keys with the locks of the fork, the rest as it is now
    forked_inputs = buildkey.Inputs(project, lock=forked_lock, builds=forked_builds)
    component_of = {name: component for component in components(forked_inputs.deps) for name in component}

    def built_alike(name: str) -> bool:
        """Return whether `name` has the key of the fork with its lock: only the lock changed since, not the recipe,
        nor the build IDs of the packages it is built against.
        """
        component = component_of[name]
        if name not in forked or any(d not in forked for n in component for d in forked_inputs.deps[n] - component):
            return False
        return forked[name].matches(buildkey.key(forked_inputs.of(component, forked), name))  # or reuses its build

    def changed(path: Path) -> bool:
        """Return whether the files of `path` changed since the fork, untracked ones included, ignored or not, with
        those its links point to, like those of another package: what its hash covers, see specs.hash_dir(), but its
        origin.
        """
        links = [p.resolve() for p in path.rglob("*") if p.is_symlink()]
        paths = [p.relative_to(project.root).as_posix() for p in [path, *links]]
        origin = (path / "origin").relative_to(project.root).as_posix()
        untracked = (_git(project, "ls-files", "--others", "--", *paths) or "").splitlines()
        return _git(project, "diff", "--quiet", fork, "--", *paths) is None or any(f != origin for f in untracked)

    def reusable(name: str, ids: dict[str, BuildId]) -> bool:
        if changed(project.package_dirs[name]):
            return False
        if name in names:
            return True
        return (
            bool(updated)
            and built_alike(name)
            # the packages it is built against reuse their builds too
            and all(ids[d].same(forked[d]) for d in forked_inputs.deps[name] if d in ids and d in forked)
            and buildkey.lock_inputs(forked_inputs.platforms, lock, builds, name, updated)
            == buildkey.lock_inputs(forked_inputs.platforms, forked_lock, forked_builds, name, updated)
        )  # fmt: skip

    return update(project, reusable)


def _next(known: BuildId | None, evr: str | None, *, maintenance: bool) -> tuple[int, int]:
    """Return the number after `known`, the build ID of the fork, for a build of the epoch-version-release `evr`:
    +b<n+1> on the main branch, +b<n>.<m+1> on a maintenance one, and 1, or 0.1, for a greater epoch-version-release.
    """
    if not known or _newer(evr, known.evr):
        return (0, 1) if maintenance else (1, 0)
    return (known.number, known.sub + 1) if maintenance else (known.number + 1, 0)


def compute(
    project: Project,
    fork: Base | None,
    hashes: dict[str, str] | None = None,
    reuse: Callable[[str, dict[str, BuildId]], bool] | None = None,
    shown: dict[str, dict[str, list]] | None = None,
    inputs: buildkey.Inputs | None = None,
) -> dict[str, BuildId]:
    """Return the build ID of each package, from those of the main branch where the current one forks from it, `fork`,
    see base(), from the `inputs` of the keys, read with the `hashes` of the package directories unless given (see
    xcraft.buildkey.Inputs).

    The build keys are those of xcraft.buildkey, see docs/build-keys.md: their inputs are put in `shown`, by package,
    if given.

    A package with the key of the fork keeps its build ID, and so does one reusing a build for the key, there or in the
    working tree, while it is the fork's, or one with the key of a maintenance branch merged, and a greater number
    than the fork's. Otherwise, a package `reuse` takes, with the build IDs of those before it, reuses the build of the
    fork, accepting the key; and the others get the next number of the fork's, see _next(), or 1, or 0.1, for a greater
    epoch-version-release than the fork's, which tells its builds apart already: a lower one, whose numbers may have
    been given before, fails update(). The numbers the main branch gave since the fork do not count: giving
    one again conflicts on merge, in the lines of the package.
    Without the main branch, the numbers are those of the working tree, 1 for new packages: there is nothing to number
    them against.
    """
    inputs, current = inputs or buildkey.Inputs(project, hashes), read(project)
    ids: dict[str, BuildId] = {}
    for component in components(inputs.deps):  # the dependencies first
        of = inputs.of(component, ids)
        for name in sorted(component):
            key = buildkey.key(of, name)
            if shown is not None:
                shown[name] = of
            known = (current if fork is None else fork.ids).get(name)
            # still the fork's build
            reused = [
                i for i in (current.get(name),) if i and i.accepts and (fork is None or i.same(fork.ids.get(name)))
            ]
            merged = [
                i for o in (fork.others if fork else ()) if (i := o.get(name)) and (not known or i.order > known.order)
            ]
            if found := next((i for i in [*filter(None, [known]), *reused, *merged] if i.matches(key)), None):
                # with its epoch-version-release, if given before it was recorded: that of its spec, of the same key
                ids[name] = found if found.evr else found.model_copy(update={"evr": _evr(project, name)})
            elif known and reuse and reuse(name, ids):
                ids[name] = known.model_copy(update={"accepts": key})
            elif fork is None:
                number, sub = (known.number, known.sub) if known else (1, 0)
                ids[name] = BuildId(number=number, sub=sub, key=key, evr=_evr(project, name))
            else:
                evr = _evr(project, name)
                number, sub = _next(known, evr, maintenance=fork.maintenance)
                ids[name] = BuildId(number=number, sub=sub, key=key, evr=evr)
    return ids


def proposed(fork: Base | None, ids: dict[str, BuildId]) -> set[str]:
    """Return the packages whose build ID of `ids` is not the one of the official branch where the current one forks
    from it, `fork`, see base(), nor of a maintenance branch it merges: those of development builds, none without the
    main branch. A reused build is the main branch's.
    """
    if fork is None:
        return set()
    return {name for name, i in ids.items() if not any(i.same(o.get(name)) for o in (fork.ids, *fork.others))}


def final_only(project: Project) -> bool:
    """Return whether the builds of the checkout are all final ones, without computing their IDs: out of a git
    repository, without the main branch, or on it without changes.
    """
    return _fork(project) is None or (_official(project) and not _git(project, "status", "--porcelain"))
