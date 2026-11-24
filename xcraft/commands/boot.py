"""xcraft boot."""

from __future__ import annotations

import contextlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from xcraft import process, vm
from xcraft.commands import Arch, Dev, Final, Key, NoCheck, dev_ident
from xcraft.commands.iso import made_iso
from xcraft.commands.vm import check_port, show
from xcraft.display import console
from xcraft.project import Project

if TYPE_CHECKING:
    import subprocess


def _check_host(arch: str, accel: str, *, new_disk: bool, bios: bool) -> None:
    if accel not in {"kvm", "tcg"}:
        msg = "--accel must be kvm or tcg"
        raise typer.BadParameter(msg)
    if arch not in {"x86_64", "x86_64_v2", "aarch64"}:
        msg = "boot currently supports x86_64, x86_64_v2 and aarch64 only"
        raise typer.BadParameter(msg)
    if bios and vm.qemu_arch(arch) != "x86_64":
        msg = f"--bios is for x86 only, not {arch}, which boots with UEFI"
        raise typer.BadParameter(msg)
    vm.check_tools(accel, new_disk=new_disk, arch=arch)


def answered(text: str, answers: list[str]) -> str:
    """Return the answer file `text` with the `answers`: NAME=VALUE sets the text of the element NAME of the
    installation, NAME.ATTR=VALUE an attribute of it, each added when missing.
    """
    root = ET.fromstring(text, ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))  # noqa: S314, the project's
    for answer in answers:
        if not (match := re.fullmatch(r"([A-Za-z][\w-]*)(?:\.([A-Za-z][\w-]*))?=(.*)", answer, re.DOTALL)):
            msg = f"{answer}: not NAME=VALUE nor NAME.ATTR=VALUE"
            raise typer.BadParameter(msg, param_hint="--answer")
        name, attribute, value = match.groups()
        element = root.find(name)
        if element is None:
            element = ET.SubElement(root, name)
        if attribute:
            element.set(attribute, value)
        else:
            element.text = value
    ET.indent(root)
    # what is before the element of the installation as it is, like the declaration and the comments
    prolog = text[: text.index(f"<{root.tag}")]
    return prolog + ET.tostring(root, encoding="unicode") + "\n"


def _check_edits(*, bios: bool, kernel_args: str | None, unattended: bool, answer: list[str] | None) -> None:
    if bios and (kernel_args or unattended):
        msg = "--kernel-args and --unattended edit the GRUB menu of the ISO: not with --bios"
        raise typer.BadParameter(msg)
    if answer and not unattended:
        msg = "--answer changes the answer file of --unattended"
        raise typer.BadParameter(msg)


def _unattended(project: Project, answers: list[str]) -> str:
    """Return the answer file of [iso] unattended of xcraft.toml with the `answers`, see answered()."""
    if not project.config.iso.unattended:
        msg = "no unattended answer file in [iso] of xcraft.toml"
        raise typer.BadParameter(msg, param_hint="--unattended")
    if not (path := project.root / project.config.iso.unattended).is_file():
        msg = f"no answer file {path}, the unattended one of [iso] of xcraft.toml"
        raise typer.BadParameter(msg, param_hint="--unattended")
    try:
        return answered(path.read_text(), answers)
    except ET.ParseError as e:
        msg = f"{path}: {e}"
        raise typer.BadParameter(msg, param_hint="--unattended") from e


_STOPS = {
    "guest-shutdown": "the guest powered off",
    "guest-panic": "the guest kernel panicked",
    "host-qmp-quit": "QEMU was told to quit, like by xcraft vm stop",
    "host-signal": "QEMU was stopped by a signal",
}
"""What stopped the VM, by the reason of the SHUTDOWN event of QEMU, but the installer asking to reboot."""


def _show_end(reason: str | None, vm_dir: Path) -> None:
    """Show how the VM ended, by the `reason` QEMU gave, and fail unless the installer asked to reboot, which it does
    once it has installed the system.
    """
    if reason == "guest-reset":
        console.print(
            "[bold green]✓[/] the installer finished and asked to reboot: xcraft vm start boots the installed system"
        )
        return
    why = _STOPS.get(reason or "", f"QEMU stopped: {reason}" if reason else "QEMU stopped without telling why")
    console.print(f"[bold red]✗[/] {why}, rather than the installer asking to reboot: see {vm_dir / 'serial.log'}")
    raise typer.Exit(1)


def _boot_with(proc: subprocess.Popen[bytes], vm_dir: Path, kernel_args: str) -> None:
    """Boot the default entry of the GRUB menu of the VM of `proc` with `kernel_args`, or stop it when that fails, not
    to leave it running, with its disk and its ssh port, in a state nobody asked for.
    """
    try:
        vm.edit_default_entry(vm_dir, kernel_args)
    except BaseException:
        proc.terminate()
        proc.wait(timeout=10)
        raise
    console.print(f"booted the default entry with {kernel_args}", markup=False)


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
    bios: Annotated[bool, typer.Option("--bios", help="Use legacy BIOS instead of UEFI, on x86 only.")] = False,
    firmware_code: Annotated[
        Path | None, typer.Option(help="UEFI firmware CODE image, OVMF or AAVMF; pair with --firmware-vars.")
    ] = None,
    firmware_vars: Annotated[
        Path | None, typer.Option(help="UEFI firmware VARS template, copied only for a new session.")
    ] = None,
    accel: Annotated[
        str | None,
        typer.Option(
            help="QEMU accelerator: kvm, or tcg for software emulation; kvm for the architecture of the host, tcg "
            "for the others, by default.",
            show_default=False,
        ),
    ] = None,
    qemu_arg: Annotated[
        list[str] | None, typer.Option("--qemu-arg", help="Extra QEMU argument, repeatable. Use --qemu-arg=-option.")
    ] = None,
    kernel_args: Annotated[
        str | None,
        typer.Option(
            help="Arguments added to the kernel command line of the ISO's default boot entry, in GRUB's editor, like "
            "rt_answerfile=<url> for an installation with another answer file."
        ),
    ] = None,
    unattended: Annotated[
        bool,
        typer.Option(
            "--unattended",
            help="Install unattended, with the answer file of unattended in [iso] of xcraft.toml, which the VM gets "
            "from QEMU.",
        ),
    ] = False,
    answer: Annotated[
        list[str] | None,
        typer.Option(
            "--answer",
            help="NAME=VALUE sets the text of the element NAME of the unattended answer file, NAME.ATTR=VALUE an "
            "attribute of it, each added when missing, like primary-disk.fs-type=ext4. Repeatable.",
            show_default=False,
        ),
    ] = None,
    locked: Annotated[bool, typer.Option("--locked", help="Fail when making the ISO would change the lock.")] = False,
    arch: Arch = None,
    key: Key = None,
    dev: Dev = None,
    final: Final = False,
    nocheck: NoCheck = False,
    local: Annotated[
        bool,
        typer.Option("--local", help="Make the ISO on this machine, rather than on the builder of the architecture."),
    ] = False,
) -> None:
    """Build the installation ISO, create a virtual disk if missing, and boot it with QEMU: with the development builds
    of the packages whose build ID is not the main branch's, like xcraft build, unless --final. The ISO is made on the
    builder of the architecture, if any, unless --local, see xcraft iso.

    The disk persists between runs. The installer VM exits when it requests a reboot, which tells it finished: xcraft
    vm start then boots the installed disk, without the ISO. Use xcraft vm to inspect or stop it. In the foreground,
    it fails when the VM stops otherwise, like powered off, or by a kernel panic with the guest's pvpanic driver.
    """
    _check_edits(bios=bios, kernel_args=kernel_args, unattended=unattended, answer=answer)
    check_port(ssh_port)
    ident = dev_ident(ctx, dev, final=final, nocheck=nocheck)
    project = Project.find(arch)
    answers = _unattended(project, answer or []) if unattended else None
    kernel_args = " ".join(filter(None, [f"rt_answerfile={vm.ANSWER_URL}" if unattended else "", kernel_args])) or None
    vm_dir = (vm_dir or project.state / "vm" / project.arch).absolute()
    disk = (disk or vm_dir / "disk.qcow2").absolute()
    if disk.exists() and not disk.is_file():
        msg = f"virtual disk is not a file: {disk}"
        raise typer.BadParameter(msg)
    # but its disk: xcraft vm destroy deletes the directory
    ours = vm_dir.is_relative_to(project.state.absolute()) or (vm_dir / "session.json").exists()
    if not ours and vm_dir.is_dir() and any(path != disk for path in vm_dir.iterdir()):
        msg = f"{vm_dir} has content xcraft did not make, which xcraft vm destroy would delete"
        raise typer.BadParameter(msg, param_hint="--vm-dir")
    accel = accel or vm.default_accel(project.arch)
    _check_host(project.arch, accel, new_disk=not disk.exists(), bios=bios)
    if bios and (firmware_code or firmware_vars):
        msg = "--bios cannot be used with --firmware-code or --firmware-vars"
        raise typer.BadParameter(msg)
    ovmf = None if bios else vm.firmware(firmware_code, firmware_vars, project.arch)
    vm.check_stopped(vm_dir)
    if not iso:
        project.frozen = locked
        iso = made_iso(project, ident, key, local=local, nocheck=nocheck)
        console.print(f"[bold green]✓[/] {iso}")
    with project.lock():
        vm.check_stopped(vm_dir)
        ssh_port = vm.free_port() if ssh_port is None else ssh_port
        vm_dir.mkdir(parents=True, exist_ok=True)
        if not disk.exists():
            disk.parent.mkdir(parents=True, exist_ok=True)
            process.run(["qemu-img", "create", "-f", "qcow2", str(disk), disk_size])
        launch = vm.Launch(
            name=project.id,
            memory=memory,
            cpus=cpus,
            accel=accel,
            ovmf=ovmf,
            qemu_args=qemu_arg or [],
            arch=project.arch,
        )
        cmd = vm.qemu_command(
            vm_dir,
            launch,
            disk=disk,
            iso=iso,
            ssh_port=ssh_port,
            headless=headless or background,
            events=not background,
            answer=vm.answer_response(vm_dir, answers) if answers else None,
        )
        session = vm.Session(
            pid=0, arch=project.arch, disk=disk, iso=iso.absolute(), ssh_port=ssh_port, command=cmd, launch=launch
        )
        proc, events = vm.launch(vm_dir, session)
    with contextlib.ExitStack() as stack:
        if events:
            stack.enter_context(events)
        show(vm_dir, session)
        if kernel_args:
            _boot_with(proc, vm_dir, kernel_args)
        if events:
            _show_end(vm.wait(proc, vm_dir, events), vm_dir)
