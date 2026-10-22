"""The compiler caches of the packages, shared through the remote storage.

Each package has its own ccache directory, which the builds of CI fill. It is shared as a tarball, in
``ccache/<arch>/<package>/``, rather than read from the storage by ccache itself, as the builds run without the
network: a build without a local compiler cache gets it first. The tarball is not compressed again: ccache
compresses its entries.
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from xcraft import remote
from xcraft.display import activity, console

if TYPE_CHECKING:
    from xcraft.project import Project, Remote

TARBALL = "ccache.tar"
_LOCAL = {"ccache.conf", "tmp"}
"""What is not shared: the configuration of a cache, and its temporary files."""


def local_path(project: Project, name: str) -> Path:
    """Return the compiler cache of `name`, which its builds use."""
    return project.state / "ccache" / name


def remote_path(project: Project, name: str) -> str:
    """Return the tree of the compiler cache of `name` in the remote storage."""
    return f"ccache/{project.arch}/{name}"


def push(project: Project, storage: Remote, name: str) -> bool:
    """Upload the compiler cache of `name`, replacing the one of the storage, and return whether it has one."""
    if not (cache := local_path(project, name)).is_dir():
        return False
    with tempfile.TemporaryDirectory(prefix="xcraft-ccache-") as tmp, activity(f"uploading the ccache of {name}"):
        with tarfile.open(Path(tmp) / TARBALL, "w") as tar:
            for path in sorted(cache.iterdir()):
                if path.name not in _LOCAL:
                    tar.add(path, path.name)
        remote.upload_tree(storage, remote_path(project, name), Path(tmp), [TARBALL])
    return True


def fetch(project: Project, storage: Remote, name: str) -> bool:
    """Download the compiler cache of `name`, replacing the local one, and return whether the storage has one."""
    cache = local_path(project, name)
    cache.parent.mkdir(parents=True, exist_ok=True)
    # next to it, to move it there
    with tempfile.TemporaryDirectory(dir=cache.parent, prefix=f".{name}-") as tmp:
        with activity(f"downloading the ccache of {name}") as on_line:
            if not remote.download_tree(storage, remote_path(project, name), Path(tmp) / "tree", on_line):
                return False
        with tarfile.open(Path(tmp) / "tree" / TARBALL) as tar:
            tar.extractall(Path(tmp) / "cache", filter="data")
        if cache.exists():
            shutil.rmtree(cache)
        (Path(tmp) / "cache").rename(cache)
    return True


def fetch_missing(project: Project, name: str) -> None:
    """Get the compiler cache of `name` from the remote storage, when it has none, and the storage has one."""
    if not (storage := project.config.remote) or local_path(project, name).exists():
        return
    try:
        fetch(project, storage, name)
    except (remote.RemoteError, tarfile.TarError) as e:
        console.print(f"[yellow]no compiler cache from the remote storage[/]: {escape(str(e))}")
