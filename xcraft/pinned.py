"""The pinned upstream binary packages of an architecture, in the repository the build environment installs from.

Their RPMs are in a store of the machine, a podman volume the projects share, by their SHA-256: each one downloaded
once, and kept only with the SHA-256 of its pin, see buildenv/pin.py. The repository of the pins of an architecture is
made once, in the store too, of those RPMs, checked against the pins before it is used, and mounted read-only on
/pinned in the containers installing from it:
the build of the image, see container.build_image(), and the builds, see buildenv/build.sh, which install nothing
else upstream. The packages of the base image, of the same versions, are not installed again: they are those of its
digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from xcraft import process
from xcraft.lock import load_lock

if TYPE_CHECKING:
    from xcraft.lock import Pin
    from xcraft.project import Project

VOLUME = "xcraft-pinned"
"""The podman volume of the store of the pinned RPMs, and of their repositories."""
REPO_ID = "xcraft-pinned"
REPO_FILE = f"[{REPO_ID}]\nname={REPO_ID}\nbaseurl=file:///pinned\nenabled=0\ngpgcheck=0\nmetadata_expire=0\n"
"""The definition of the repository in the image, enabled by the commands installing from it only: its metadata is
read again each time, as /pinned is the repository of other pins then."""


class PinnedError(RuntimeError):
    """Raised when the pinned packages are not in the lock, or cannot be downloaded."""


def pins(project: Project) -> dict[str, Pin]:
    """Return the pinned binary packages of the architecture of `project`, of its lock."""
    if not (pinned := load_lock(project).binaries.get(project.arch)):
        msg = f"no binary packages pinned for {project.arch} in the lock: see xcraft lock"
        raise PinnedError(msg)
    return pinned


_checked: set[Path] = set()
"""The repositories whose RPMs this process checked."""


def repository(project: Project) -> Path:
    """Return the repository of the pinned binary packages of the architecture of `project`, made if needed, with
    RPMs of the SHA-256 of their pins: those of other contents in the store are downloaded again.
    """
    pinned = pins(project)
    ident = hashlib.sha256(json.dumps({n: p.text() for n, p in sorted(pinned.items())}).encode()).hexdigest()
    store = _store()
    repo = store / "repos" / ident
    if repo in _checked:
        return repo
    for _ in range(2):
        if not (repo / "repodata/repomd.xml").exists():
            _make(project, pinned, store, ident)
        if not (damaged := {n: p for n, p in pinned.items() if _sha256(repo / file_name(n, p)) != p.sha256}):
            _checked.add(repo)
            return repo
        for name, pin in damaged.items():  # in the store, which the repository has them hard-linked from
            (store / "rpms" / pin.sha256 / file_name(name, pin)).unlink(missing_ok=True)
        shutil.rmtree(repo)
    msg = f"the pinned packages of {project.arch} keep other contents than pinned in the store {store}"
    raise PinnedError(msg)


def file_name(name: str, pin: Pin) -> str:
    """Return the file name of the RPM of the pinned package `name`, in the store and the repositories, as pin.py."""
    return f"{name}-{pin.evr.split(':')[-1]}.{pin.arch}.rpm"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _store() -> Path:
    """Return the store, made if needed."""
    if process.run(["podman", "volume", "exists", VOLUME], check=False).returncode:
        process.run(["podman", "volume", "create", VOLUME], capture=True)
    inspect = ["podman", "volume", "inspect", "--format", "{{.Mountpoint}}", VOLUME]
    return Path(process.run(inspect, capture=True).stdout.strip())


def _make(project: Project, pinned: dict[str, Pin], store: Path, ident: str) -> None:
    """Make the repository `ident` of the `pinned` packages, of their RPMs downloaded and checked, see pin.py."""
    from xcraft.container import run_natively  # noqa: PLC0415, which builds the image of the pins

    part = f"repos/.{ident}.{os.getpid()}"
    with project.work("pin") as work:
        data = {"arch": project.arch, "pins": {name: pin.model_dump() for name, pin in pinned.items()}}
        (work / "pins.json").write_text(json.dumps(data))
        run_natively(
            project,
            "pin",
            ["python3", "/xcraft/pin.py", "/work/pins.json", part],
            volumes=[f"{work}:/work:ro", f"{VOLUME}:/store"],
            show=f"getting the pinned packages of {project.arch}",
        )
    # with the tool of the host, in this container only
    run_natively(
        project.on_host(),
        "pinned-repo",
        ["bash", "-c", f"dnf -q -y install createrepo_c && createrepo_c -q /store/{part}"],
        volumes=[f"{VOLUME}:/store"],
        show=f"making the repository of the pinned packages of {project.arch}",
    )
    try:
        (store / part).rename(store / "repos" / ident)
    except OSError:
        if not (store / "repos" / ident / "repodata/repomd.xml").exists():
            raise
        shutil.rmtree(store / part)  # made meanwhile, by another command
