"""xcraft boot."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from xcraft import process, vm
from xcraft.commands import Arch, Dev, Final, Key, NoCheck, choose_signer, dev_ident
from xcraft.commands.vm import check_port, show
from xcraft.display import console
from xcraft.iso import checkout_iso
from xcraft.project import Project


def _check_host(arch: str, accel: str, *, new_disk: bool) -> None:
    if accel not in {"kvm", "tcg"}:
        msg = "--accel must be kvm or tcg"
        raise typer.BadParameter(msg)
    if arch not in {"x86_64", "x86_64_v2"}:
        msg = "boot currently supports x86_64 and x86_64_v2 only"
        raise typer.BadParameter(msg)
    vm.check_tools(accel, new_disk=new_disk)


def _kernel_args(project: Project, kernel_args: str | None, *, unattended: bool) -> str | None:
    """Return the kernel arguments to add to the default boot entry of the ISO: the unattended ones of xcraft.toml,
    with --unattended, then `kernel_args`.
    """
    if unattended and not project.config.iso.unattended:
        msg = "no unattended kernel arguments in [iso] of xcraft.toml"
        raise typer.BadParameter(msg, param_hint="--unattended")
    return " ".join(filter(None, [project.config.iso.unattended if unattended else "", kernel_args])) or None


def boot(
    ctx: typer.Context,
    iso: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="Boot an existing ISO instead of generating one.")
    ] = None,
    disk: Annotated[Path | None, typer.Option(help="Virtual disk. .xcraft/vm/<arch>/disk.qcow2 by default.")] = None,
    disk_size: Annotated[str, typer.Option(help="Size of a new disk. Existing disks are not resized.")] = "80G",
    memory: Annotated[int, typer.Option(min=512, help="VM memory in MiB.")] = 8192,
    cpus: Annotated[int, typer.Option(min=1, help="Number of virtual CPUs.")] = 4,
    ssh_port: Annotated[
        int | None,
        typer.Option(
            min=0,
            max=65535,
            help="Localhost port forwarded to SSH in the VM, the first free one from 2222 by default. 0 disables it.",
            show_default=False,
        ),
    ] = None,
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
    kernel_args: Annotated[
        str | None,
        typer.Option(
            help="Arguments added to the kernel command line of the ISO's default boot entry, in GRUB's editor, like "
            "rt_answerfile=file:///opt/xensource/installer/answerfile-vm.xml for an unattended installation."
        ),
    ] = None,
    unattended: Annotated[
        bool,
        typer.Option(
            "--unattended",
            help="Install unattended, with the kernel arguments of unattended in [iso] of xcraft.toml, added like with "
            "--kernel-args.",
        ),
    ] = False,
    locked: Annotated[bool, typer.Option("--locked", help="Fail when making the ISO would change the lock.")] = False,
    unsigned: Annotated[bool, typer.Option("--unsigned", help="Do not sign the ISO's packages or repository.")] = False,
    arch: Arch = None,
    key: Key = None,
    dev: Dev = None,
    final: Final = False,
    nocheck: NoCheck = False,
) -> None:
    """Build the installation ISO, create a virtual disk if missing, and boot it with QEMU: with the development builds
    of the packages whose build ID is not the main branch's, like xcraft build, unless --final.

    The disk persists between runs. The installer VM exits when it requests a reboot: xcraft vm start then boots the
    installed disk, without the ISO. Use xcraft vm to inspect or stop it.
    """
    if bios and (kernel_args or unattended):
        msg = "--kernel-args and --unattended edit the GRUB menu of the ISO: not with --bios"
        raise typer.BadParameter(msg)
    check_port(ssh_port)
    ident = dev_ident(ctx, dev, final=final, nocheck=nocheck)
    project = Project.find(arch)
    kernel_args = _kernel_args(project, kernel_args, unattended=unattended)
    vm_dir = (vm_dir or project.state / "vm" / project.arch).absolute()
    disk = (disk or vm_dir / "disk.qcow2").absolute()
    if disk.exists() and not disk.is_file():
        msg = f"virtual disk is not a file: {disk}"
        raise typer.BadParameter(msg)
    _check_host(project.arch, accel, new_disk=not disk.exists())
    if bios and (firmware_code or firmware_vars):
        msg = "--bios cannot be used with --firmware-code or --firmware-vars"
        raise typer.BadParameter(msg)
    ovmf = None if bios else vm.firmware(firmware_code, firmware_vars)
    vm.check_stopped(vm_dir)
    if not iso:
        project.frozen = locked
        signer = choose_signer(project, local=True, unsigned=unsigned, dev_key=False, key=key)
        iso = checkout_iso(project, signer=signer, dev=ident, nocheck=nocheck)
        console.print(f"[bold green]✓[/] {iso}")
    with project.lock():
        vm.check_stopped(vm_dir)
        ssh_port = vm.free_port() if ssh_port is None else ssh_port
        vm_dir.mkdir(parents=True, exist_ok=True)
        if not disk.exists():
            disk.parent.mkdir(parents=True, exist_ok=True)
            process.run(["qemu-img", "create", "-f", "qcow2", str(disk), disk_size])
        launch = vm.Launch(name=project.id, memory=memory, cpus=cpus, accel=accel, ovmf=ovmf, qemu_args=qemu_arg or [])
        cmd = vm.qemu_command(vm_dir, launch, disk=disk, iso=iso, ssh_port=ssh_port, headless=headless or background)
        session = vm.Session(
            pid=0, arch=project.arch, disk=disk, iso=iso.absolute(), ssh_port=ssh_port, command=cmd, launch=launch
        )
        proc = vm.launch(vm_dir, session)
    show(vm_dir, session)
    if kernel_args:
        vm.edit_default_entry(vm_dir, kernel_args)
        console.print(f"booted the default entry with {kernel_args}", markup=False)
    if not background:
        vm.wait(proc, vm_dir)
