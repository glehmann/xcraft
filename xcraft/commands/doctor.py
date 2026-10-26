"""xcraft doctor."""

from __future__ import annotations

import getpass
import os
import shutil
from typing import Annotated

import typer

from xcraft import host, process, remote
from xcraft.display import console
from xcraft.project import Project

_MARKS = {"ok": "[green]✓[/]", "warning": "[yellow]![/]", "error": "[red]✗[/]"}
_TOOLS = {
    # what each tool is needed for, and whether xcraft cannot work at all without it
    "git": ("by xcraft import, bump and patchset", True),
    "git-lfs": ("by xcraft import, for the archives of the packaging repositories", False),
    "gpg": ("to sign the packages", False),
    "rsync": ("by xcraft repo --host", False),
    "ssh": ("by xcraft repo --host", False),
}


def _podman() -> tuple[list[tuple[str, str]], list[str], list[host.Fix]]:
    """Return the checks of podman, the tools it misses, and the other fixes it needs."""
    if os.geteuid() == 0:
        return [("error", "podman: run by root, while xcraft runs it rootless: run xcraft as a regular user")], [], []
    if not shutil.which("podman"):
        return [("error", "podman: not found, and the builds run in its containers")], ["podman"], []
    info = ["podman", "info", "--format", "{{.Version.Version}} {{.Host.Security.Rootless}}"]
    podman = process.run(info, check=False, capture=True)
    if podman.returncode == 0:
        return [("ok", f"podman {podman.stdout.split()[0]}, rootless")], [], []
    error = (podman.stderr.strip().splitlines() or ["failed"])[-1]
    checks = [("error", f"podman: not working rootless, and the builds run in its containers: {error}")]
    if shutil.which("newuidmap"):
        return checks, [], host.subids(getpass.getuser())
    checks.append(("error", "newuidmap: not found, needed by rootless podman"))
    return checks, ["newuidmap"], host.subids(getpass.getuser())


def _storage(project: Project) -> list[tuple[str, str]]:
    """Return the checks of the remote storage, if any."""
    if not (storage := project.config.remote):
        return []
    try:
        remote.exists(storage, "index.html")
        checks = [("ok", f"remote storage: {storage.url}")]
    except remote.RemoteError as e:
        checks = [("warning", f"remote storage unavailable: {e}")]
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return [*checks, ("ok", "S3 credentials of the remote storage")]
    return [*checks, ("warning", "no S3 credentials in AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, for xcraft push")]


def _checks(project: Project) -> tuple[list[tuple[str, str]], list[host.Fix]]:
    """Return the checks of the host, as their state, ok, warning or error, and what they found, and their fixes."""
    checks, missing, fixes = _podman()
    for tool, (needed, required) in _TOOLS.items():
        if path := shutil.which(tool):
            checks.append(("ok", f"{tool}: {path}"))
        else:
            checks.append(("error" if required else "warning", f"{tool}: not found, needed {needed}"))
            missing.append(tool)
    for arch in (p.arch for p in project.all_platforms() if not p.native):
        if problem := host.emulation_problem(arch):
            checks.append(("warning", problem))
            if not host.emulator(arch):
                missing.append(f"qemu-{arch}")
            fixes += host.emulation_fix(arch)
        else:
            checks.append(("ok", f"emulation of {arch}"))
    checks += _storage(project)
    install = host.install(missing)
    if missing and not install:
        checks.append(("warning", f"install {', '.join(missing)} with the package manager of the distribution"))
    # the installs first, and each command once, like the creation of /etc/binfmt.d/ for several architectures
    return checks, list({str(fix): fix for fix in [*install, *fixes]}.values())


def doctor(
    fix: Annotated[
        bool, typer.Option("--fix", help="Fix what it can: install what is missing, configure podman and emulation.")
    ] = False,
) -> None:
    """Check that the host has what xcraft needs, and tell how to get what it misses."""
    project = Project.find()
    checks, fixes = _checks(project)
    done: set[str] = set()
    # an install can make other fixes possible, like registering the emulator it installs
    while fix and fixes and not done >= {str(f) for f in fixes}:
        console.print("to fix the host:\n" + "".join(f"  {f}\n" for f in fixes), end="", markup=False)
        typer.confirm("Run these commands?", abort=True)
        for f in fixes:
            f.run()
        done |= {str(f) for f in fixes}
        checks, fixes = _checks(project)
    for state, what in checks:
        console.print(f"{_MARKS[state]} {what}")
    if fixes and not fix:
        console.print(
            "xcraft doctor --fix fixes it, with:\n" + "".join(f"  {f}\n" for f in fixes), end="", markup=False
        )
    if any(state == "error" for state, _ in checks):
        raise typer.Exit(1)
