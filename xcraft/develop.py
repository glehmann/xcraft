"""Packages built from the trees of their projects, as they are, for the development builds: see xcraft develop, and
Developing from a project's tree in docs/builds.md.

A package developed from a tree keeps its spec, but its build tree: its main archive is the tree, its patches are those
of the tree, and its %prep keeps the directory of its sources, the one of its %setup, from one build to the next,
synchronized with the tree, rather than unpacked again: the builds of a tree are incremental, like a build in it. Its
version and release are those of the spec: its development builds upgrade the hosts, as those of any change.

The trees are a setting of the checkout, in .xcraft/, never in git: the final builds, publish and push refuse them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from pydantic import TypeAdapter

from xcraft import process
from xcraft.container import BUILDENV
from xcraft.display import messages
from xcraft.project import write_atomically

if TYPE_CHECKING:
    from xcraft.project import Project

ARCHIVE = "xcraft-develop.tar"
"""The main archive of a package developed from a tree, in its SOURCES/: the tree, as git archive makes it."""
SYNC = "develop.py"
"""The script of BUILDENV synchronizing the directory of the sources with the archive, in %prep."""
FORMAT = 2
"""The version of how a tree is staged, in the keys of its builds: changed with it, to build them again."""
_TREES = TypeAdapter(dict[str, str])
_SECTION = re.compile(
    r"^%(build|install|check|clean|files|changelog|package|description|pre|post|preun|postun|pretrans|posttrans"
    r"|trigger\w*|filetrigger\w*|transfiletrigger\w*|verifyscript|generate_buildrequires|conf|sourcelist|patchlist)\b"
)


class DevelopError(RuntimeError):
    """Raised when a tree cannot be developed from."""


def _path(project: Project) -> Path:
    return project.state / "develop.json"


def trees(project: Project) -> dict[str, Path]:
    """Return the trees the packages are developed from, by package."""
    path = _path(project)
    return {name: Path(tree) for name, tree in _TREES.validate_json(path.read_bytes()).items()} if path.exists() else {}


def save(project: Project, developed: dict[str, Path]) -> None:
    """Make `developed` the trees the packages are developed from, by package."""
    if not developed:
        _path(project).unlink(missing_ok=True)
        return
    project.state.mkdir(exist_ok=True)
    text = _TREES.dump_json({name: str(tree) for name, tree in sorted(developed.items())}, indent=1).decode()
    write_atomically(_path(project), f"{text}\n")


def show(project: Project) -> None:
    """Show the trees the packages are developed from, if any, on stderr: a setting nobody should forget."""
    if developed := trees(project):
        messages.print(f"[yellow]developing[/]: {', '.join(f'{n} from {t}' for n, t in developed.items())}")


def check_none(project: Project, what: str) -> None:
    """Raise unless no package is developed from a tree: `what` takes the packages as they are in git only."""
    if developed := trees(project):
        msg = f"{what} with packages developed from trees, {', '.join(developed)}: xcraft develop --stop first"
        raise DevelopError(msg)


def _git(tree: Path, *args: str, env: dict[str, str] | None = None) -> str:
    try:
        return process.run(["git", "-C", str(tree), *args], capture=True, env=env).stdout.strip()
    except subprocess.CalledProcessError as e:
        msg = f"{tree}: git {' '.join(args)}: {e.stderr.strip()}"
        raise DevelopError(msg) from None
    except OSError as e:
        msg = f"{tree}: git {' '.join(args)}: {e}"
        raise DevelopError(msg) from None


class Tree(NamedTuple):
    """The state of a tree, as its builds get it."""

    path: Path
    commit: str
    """The commit git archive makes the archive of: HEAD, or a commit of the files of the tree on top of it."""
    describe: str
    """What git describe says of HEAD, for the messages."""


def tree(project: Project, name: str) -> Tree | None:
    """Return the state of the tree `name` is developed from, if any: its files, untracked ones too, but those git
    ignores, as a commit, the same for the same files and HEAD, which git archive makes the same archive of.
    """
    if not (path := trees(project).get(name)):
        return None
    head = _git(path, "rev-parse", "--verify", "HEAD^{commit}")
    with tempfile.TemporaryDirectory(prefix="xcraft-develop-") as tmp:
        index = Path(tmp) / "index"
        if (current := path / _git(path, "rev-parse", "--git-path", "index")).exists():
            shutil.copy2(current, index)  # with its stat data: git hashes only the files changed since
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        _git(path, "add", "--all", env=env)
        files = _git(path, "write-tree", env=env)
    if files == _git(path, "rev-parse", "HEAD^{tree}"):
        commit = head
    else:  # of the date of HEAD, by xcraft: the same commit for the same files
        date = _git(path, "log", "-1", "--format=%cI", "HEAD")
        identity = {f"GIT_{who}_{what}": value for who in ("AUTHOR", "COMMITTER") for what, value in
                    (("NAME", "xcraft develop"), ("EMAIL", "xcraft@localhost"), ("DATE", date))}  # fmt: skip
        commit = _git(path, "commit-tree", files, "-p", head, "-m", "xcraft develop", env={**os.environ, **identity})
    try:
        describe = _git(path, "describe", "--tags", "--always", "--dirty", "--abbrev=12")
    except DevelopError:
        describe = head[:12]
    return Tree(path, commit, describe)


def directory_hash(plain: str, developed: Tree | None) -> str:
    """Return the hash of the directory of a package, `plain`, with the tree it is developed from, if any: what its
    build trees get, see rpmbuild.prepare_tree().
    """
    if not developed:
        return plain
    sync = hashlib.sha256((BUILDENV / SYNC).read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps([plain, developed.commit, FORMAT, sync]).encode()).hexdigest()


def spec_text(text: str) -> str:
    """Return the spec `text` of a package developed from a tree: its main archive the tree's, ARCHIVE, without its
    patches, and its first %setup or %autosetup of %prep keeping the directory of the sources, synchronized with the
    archive, see SYNC. The directory is the one of the %setup, of the builds of the package, which compile in it: those
    of the tree compile in the same, for the cache of the compilers, which tells the directories apart.
    """
    lines, prep, replaced, source = [], False, False, False
    for line in text.splitlines():
        if line.startswith("%prep"):
            prep = True
        elif _SECTION.match(line):
            prep = False
        if re.match(r"Patch\d*\s*:", line, re.IGNORECASE) or (prep and re.match(r"%(auto)?patch\b", line)):
            continue
        if not source and re.match(r"Source0?\s*:", line, re.IGNORECASE):
            lines.append(f"Source0: {ARCHIVE}")
            source = True
        elif prep and not replaced and re.match(r"%(auto)?setup\b", line):
            directory = m[1] if (m := re.search(r"\s-[a-zA-Z]*n\s*(\S+)", line)) else "%{name}-%{version}"
            lines += [
                f"mkdir -p %{{_builddir}}/{directory}",
                f"%setup -q -T -D -n {directory}",
                f"python3 /xcraft/{SYNC} %{{SOURCE0}}",
            ]
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        msg = "no %setup nor %autosetup in its %prep, to replace with the tree"
        raise DevelopError(msg)
    if not source:
        lines.insert(0, f"Source0: {ARCHIVE}")
    return "\n".join(lines) + "\n"


def stage(developed: Tree, spec: Path, sources: Path) -> None:
    """Stage the tree `developed` in a build tree: its `spec` rewritten, see spec_text(), and its archive in
    `sources`.
    """
    spec.write_text(spec_text(spec.read_text()))
    _git(developed.path, "archive", "--format=tar", "-o", str(sources / ARCHIVE), developed.commit)
