"""xcraft history."""

from __future__ import annotations

import re

from xcraft import process
from xcraft.builds import builds_path, toml_key
from xcraft.commands import OnePackage
from xcraft.project import Project


def history(package: OnePackage) -> None:
    """Show the commits changing a package, with git log: those of its directory, and those of its lines in
    xcraft.builds, its build IDs and what its builds recorded.
    """
    project = Project.find()
    git = ["git", "-C", str(project.root), "log"]
    regex = "^" + re.sub(r"([.+*?^$()\[\]{}|\\])", r"\\\1", toml_key(package)) + r"\."
    paths = [f"{repo}/{package}" for repo in project.config.repositories]
    commits = {
        *process.run([*git, "--format=%H", "--", *paths], capture=True).stdout.split(),
        *process.run([*git, "--format=%H", "-G", regex, "--", builds_path(project).name], capture=True).stdout.split(),
    }
    # in the order of git log, children first
    if shown := [c for c in process.run([*git, "--format=%H"], capture=True).stdout.split() if c in commits]:
        process.run([*git, "--no-walk=unsorted", *shown])
