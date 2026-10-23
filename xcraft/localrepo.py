"""Local repositories of RPMs, as the containers see them, and the directories xcraft makes."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xcraft import process
from xcraft.container import run
from xcraft.display import activity, download_progress

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project

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
    """Make `dest` a hard link to `src`, or a copy on another file system."""
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
    """Return the extra upstream RPMs, downloaded to their cache when missing."""
    extra = project.state / "cache/extra"
    extra.mkdir(parents=True, exist_ok=True)
    rpms = []
    for url in project.config.extra_upstream.values():
        rpm = extra / url.rsplit("/", 1)[-1]
        if not rpm.exists():
            tmp = rpm.with_suffix(".part")
            with activity(f"downloading {rpm.name}") as on_line:
                process.download(url, tmp, download_progress(on_line))
            tmp.rename(rpm)
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

    def index(self) -> None:
        """Generate the metadata of the repository, when it changed."""
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
