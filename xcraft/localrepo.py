"""Local repositories of RPMs, as the containers see them, and the directories xcraft makes."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xcraft import process
from xcraft.container import run
from xcraft.display import activity, download_progress
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project
    from xcraft.signing import Signer

# its own id in each project, {repo}, for its own metadata in the dnf cache the containers share: the builds do
# not depend on it, nor their keys
REPO_FILE = """\
[{repo}]
name=xcraft local builds
baseurl=file:///xcraft-repo
priority=0
gpgcheck=0
metadata_expire=0
"""


class RepoError(RuntimeError):
    """Raised when a repository cannot be made."""


def repo_id(project: Project) -> str:
    """Return the id of the repository of our builds in the containers: its own in each project, see REPO_FILE."""
    return f"xcraft-{project.id}"


def link(src: Path, dest: Path) -> None:
    """Make `dest` a hard link to `src`, or a copy on another file system, replacing the file it was: never writing
    into it, which may be hard linked to the cache, like a build of another key of the same file name.
    """
    dest.unlink(missing_ok=True)
    try:
        dest.hardlink_to(src)
    except OSError:
        shutil.copy2(src, dest)


OUTPUT_MARKER = ".xcraft-output"
"""Marks the directories xcraft made, which it can empty."""


def fresh_dir(project: Project, path: Path) -> None:
    """Make `path` an empty directory, refusing to empty one with content xcraft did not make.

    xcraft made the directories in .xcraft/, those with its marker, and the
    repositories made before it had one.
    """
    ours = (
        path.resolve().is_relative_to(project.state.resolve())
        or (path / OUTPUT_MARKER).exists()
        or (path / "repodata").is_dir()
    )
    if path.exists() and any(path.iterdir()) and not ours:
        msg = f"{path} has content xcraft did not make: not emptying it"
        raise RepoError(msg)
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    (path / OUTPUT_MARKER).touch()


def extra_rpms(project: Project) -> list[Path]:
    """Return the extra upstream RPMs, in their cache, by SHA-256, of the SHA-256 of their pins: downloaded when
    missing, or of other contents.
    """
    rpms = []
    for extra in project.config.extra_upstream.values():
        rpm = project.state / "cache/extra" / extra.sha256 / extra.url.rsplit("/", 1)[-1]
        if not rpm.exists() or sha256_of(rpm) != extra.sha256:
            rpm.parent.mkdir(parents=True, exist_ok=True)
            tmp = rpm.with_suffix(".part")
            with activity(f"downloading {rpm.name}") as on_line:
                process.download(extra.url, tmp, download_progress(on_line))
            if (digest := sha256_of(tmp)) != extra.sha256:
                tmp.unlink()
                msg = f"{extra.url} has other contents, {digest}, than pinned in xcraft.toml, {extra.sha256}"
                raise RepoError(msg)
            tmp.replace(rpm)
        rpms.append(rpm)
    return rpms


@dataclass
class LocalRepo:
    """A repository of locally built packages, as seen by the containers."""

    project: Project
    path: Path
    up_to_date: bool = False

    def reset(self, *, extra_upstream: bool = True) -> None:
        """Start the repository over, with only the extra upstream RPMs, or empty."""
        fresh_dir(self.project, self.path)
        self.up_to_date = False
        for rpm in extra_rpms(self.project) if extra_upstream else []:
            link(rpm, self.path / rpm.name)

    def add(self, build: Path) -> None:
        """Add the binary RPMs of a build."""
        for rpm in (build / "RPMS").rglob("*.rpm"):
            link(rpm, self.path / rpm.name)
        self.up_to_date = False

    def volumes(self) -> list[str]:
        """Return the volumes making the repository available to a container, as the xcraft repository."""
        self.index()
        repo_file = self.project.state / "xcraft.repo"
        repo_file.write_text(REPO_FILE.format(repo=repo_id(self.project)))
        return [f"{self.path}:/xcraft-repo:ro", f"{repo_file}:/etc/yum.repos.d/xcraft.repo:ro"]

    def index(self, signer: Signer | None = None) -> None:
        """Generate the metadata of the repository, when it changed, signed by `signer`, if any."""
        if not self.up_to_date:
            run(
                self.project,
                "repo",
                "createrepo_c -q /repo",
                volumes=[f"{self.path}:/repo"],
                show="indexing the local repository",
                network=False,
            )
            self.up_to_date = True
        if signer:
            signer.sign_file(self.path / "repodata/repomd.xml")
