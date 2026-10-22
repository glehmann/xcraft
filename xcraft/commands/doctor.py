"""xcraft doctor."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Annotated

import typer

from xcraft import host, remote
from xcraft.display import console
from xcraft.project import Project

_MARKS = {"ok": "[green]✓[/]", "warning": "[yellow]![/]", "error": "[red]✗[/]"}


def _tool(name: str, needed: str, *, required: bool = False) -> tuple[str, str]:
    if path := shutil.which(name):
        return "ok", f"{name}: {path}"
    return "error" if required else "warning", f"{name}: not found, needed {needed}"


def _checks(project: Project) -> list[tuple[str, str]]:
    """Return the checks of the host, as their state, ok, warning or error, and what they found."""
    podman = subprocess.run(
        ["podman", "info", "--format", "{{.Version.Version}} {{.Host.Security.Rootless}}"],
        capture_output=True,
        text=True,
        check=False,
    ) if shutil.which("podman") else None  # fmt: skip
    if podman and podman.returncode == 0 and podman.stdout.split()[1:] == ["true"]:
        checks = [("ok", f"podman {podman.stdout.split()[0]}, rootless")]
    else:
        checks = [("error", "podman: not working rootless, and the builds run in its containers")]
    checks += [
        _tool("git", "by xcraft import, bump and patchset", required=True),
        _tool("git-lfs", "by xcraft import, for the archives of the packaging repositories"),
        _tool("gpg", "to sign the packages"),
        _tool("rsync", "by xcraft repo --host"),
        _tool("ssh", "by xcraft repo --host"),
    ]
    for arch in (p.arch for p in project.all_platforms() if not p.native):
        problem = host.emulation_problem(arch)
        checks.append(("warning", problem) if problem else ("ok", f"emulation of {arch}"))
    if storage := project.config.remote:
        try:
            remote.exists(storage, "index.html")
            checks.append(("ok", f"remote storage: {storage.url}"))
        except remote.RemoteError as e:
            checks.append(("warning", f"remote storage unavailable: {e}"))
        if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
            checks.append(("ok", "S3 credentials of the remote storage"))
        else:
            checks.append(
                ("warning", "no S3 credentials in AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, for xcraft push")
            )
    return checks


def doctor(
    fix: Annotated[
        bool, typer.Option("--fix", help="Fix what it can, with sudo: the emulation of the other architectures.")
    ] = False,
) -> None:
    """Check that the host has what xcraft needs, and tell how to get what it misses."""
    project = Project.find()
    emulated = [p.arch for p in project.all_platforms() if not p.native]
    if fix and (archs := [arch for arch in emulated if (entry := host.binfmt(arch)) and "C" not in entry[1]]):
        fixes = {arch: host.emulation_fix(arch) for arch in archs}
        for arch, (conf, flags, _) in fixes.items():
            console.print(
                f"to emulate {arch} with sudo in the containers: /etc/binfmt.d/{conf.name}, a copy of {conf} with the "
                f"flags FOC rather than {flags}",
                markup=False,
            )
        commands = host.fix_commands([(conf, text) for conf, _, text in fixes.values()])
        console.print("with:\n" + "".join(f"  {' '.join(cmd)}\n" for cmd in commands), end="", markup=False)
        typer.confirm("Run them?", abort=True)
        host.fix_emulation([(conf, text) for conf, _, text in fixes.values()])
    checks = _checks(project)
    for state, what in checks:
        console.print(f"{_MARKS[state]} {what}")
    if any(state == "error" for state, _ in checks):
        raise typer.Exit(1)
