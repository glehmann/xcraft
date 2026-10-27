"""xcraft boot."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Annotated

import typer

from xcraft import process, vm
from xcraft.commands import Arch, Key, choose_signer
from xcraft.display import console
from xcraft.iso import build_iso
from xcraft.project import Project


def _check_host(arch: str, accel: str, *, new_disk: bool) -> None:
    if accel not in {"kvm", "tcg"}:
        msg = "--accel must be kvm or tcg"
        raise typer.BadParameter(msg)
    if arch not in {"x86_64", "x86_64_v2"}:
        msg = "boot currently supports x86_64 and x86_64_v2 only"
        raise typer.BadParameter(msg)
    for tool in ["qemu-system-x86_64", *(["qemu-img"] if new_disk else [])]:
        if not shutil.which(tool):
            msg = f"{tool} is not installed: install QEMU to run the VM"
            raise RuntimeError(msg)
    if accel == "kvm" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
        msg = "KVM is not accessible: enable it and grant access to /dev/kvm, or use --accel tcg"
        raise RuntimeError(msg)


def boot(  # noqa: PLR0913, PLR0917
    iso: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Boot an existing ISO instead of generating one.")
    ] = None,
    installed: Annotated[
        bool, typer.Option("--installed", help="Boot the installed disk, without making an ISO.")
    ] = False,
    disk: Annotated[Path | None, typer.Option(help="Virtual disk. .xcraft/vm/<arch>/disk.qcow2 by default.")] = None,
    disk_size: Annotated[str, typer.Option(help="Size of a new disk. Existing disks are not resized.")] = "80G",
    memory: Annotated[int, typer.Option(min=512, help="VM memory in MiB.")] = 8192,
    cpus: Annotated[int, typer.Option(min=1, help="Number of virtual CPUs.")] = 4,
    ssh_port: Annotated[
        int, typer.Option(min=0, max=65535, help="Localhost port forwarded to SSH in the VM. 0 disables forwarding.")
    ] = 2222,
    headless: Annotated[
        bool, typer.Option("--headless", help="Disable the display window. Use xcraft vm for console access.")
    ] = False,
    background: Annotated[
        bool, typer.Option("--background", help="Launch without a window and return when ready.")
    ] = False,
    vm_dir: Annotated[Path | None, typer.Option(help="Session directory. .xcraft/vm/<arch> by default.")] = None,
    bios: Annotated[bool, typer.Option("--bios", help="Use legacy BIOS instead of UEFI.")] = False,
    firmware_code: Annotated[Path | None, typer.Option(help="OVMF CODE image; pair with --firmware-vars.")] = None,
    firmware_vars: Annotated[
        Path | None, typer.Option(help="OVMF VARS template, copied only for a new session.")
    ] = None,
    accel: Annotated[str, typer.Option(help="QEMU accelerator: kvm, or tcg for software emulation.")] = "kvm",
    qemu_arg: Annotated[
        list[str] | None, typer.Option("--qemu-arg", help="Extra QEMU argument, repeatable. Use --qemu-arg=-option.")
    ] = None,
    locked: Annotated[bool, typer.Option("--locked", help="Fail when making the ISO would change the lock.")] = False,
    unsigned: Annotated[bool, typer.Option("--unsigned", help="Do not sign the ISO's packages or repository.")] = False,
    arch: Arch = None,
    key: Key = None,
) -> None:
    """Build the installation ISO, create a virtual disk if missing, and boot it with QEMU.

    The disk persists between runs. After installation, use --installed to boot it without the ISO.
    The installer VM exits when it requests a reboot. Use xcraft vm to inspect or stop it.
    """
    if installed and iso:
        msg = "--installed and --iso cannot be used together"
        raise typer.BadParameter(msg)
    project = Project.find(arch)
    vm_dir = (vm_dir or project.state / "vm" / project.arch).absolute()
    disk = (disk or vm_dir / "disk.qcow2").absolute()
    if disk.exists() and not disk.is_file():
        msg = f"virtual disk is not a file: {disk}"
        raise typer.BadParameter(msg)
    if installed and not disk.is_file():
        msg = f"no installed disk at {disk}: run xcraft boot first"
        raise typer.BadParameter(msg)
    _check_host(project.arch, accel, new_disk=not disk.exists())
    if bios and (firmware_code or firmware_vars):
        msg = "--bios cannot be used with --firmware-code or --firmware-vars"
        raise typer.BadParameter(msg)
    ovmf = None if bios else vm.firmware(firmware_code, firmware_vars)
    vm.check_stopped(vm_dir)
    if not (iso or installed):
        project.frozen = locked
        signer = choose_signer(project, local=True, unsigned=unsigned, dev_key=False, key=key)
        iso = build_iso(project, signer=signer)
        console.print(f"[bold green]✓[/] {iso}")
    with project.lock():
        vm.check_stopped(vm_dir)
        vm_dir.mkdir(parents=True, exist_ok=True)
        if not disk.exists():
            disk.parent.mkdir(parents=True, exist_ok=True)
            process.run(["qemu-img", "create", "-f", "qcow2", str(disk), disk_size])
        cmd = vm.command(
            vm_dir,
            name=project.id,
            disk=disk,
            iso=iso,
            memory=memory,
            cpus=cpus,
            ssh_port=ssh_port,
            accel=accel,
            headless=headless or background,
            ovmf=ovmf,
        )
        proc = vm.launch(
            vm_dir,
            vm.Session(
                pid=0,
                arch=project.arch,
                disk=disk,
                iso=iso.absolute() if iso else None,
                ssh_port=ssh_port,
                command=[*cmd, *(qemu_arg or [])],
            ),
        )
    console.print(f"virtual disk: {disk}\nVM session: {vm_dir}", markup=False)
    console.print(f"Control: xcraft vm --vm-dir {vm_dir} status", markup=False)
    if ssh_port:
        console.print(f"SSH when enabled in the guest: ssh -p {ssh_port} root@127.0.0.1", markup=False)
    if not background:
        vm.wait(proc, vm_dir)
