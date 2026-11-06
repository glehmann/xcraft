"""Control a persistent local VM without the launching terminal."""

from __future__ import annotations

import codecs
import json
import time
from pathlib import Path
from typing import Annotated

import pyte
import typer

from xcraft import display, vm
from xcraft.commands import Arch
from xcraft.project import Project

app = typer.Typer(help="Inspect and control a local installer VM.", no_args_is_help=True)


@app.callback()
def options(
    ctx: typer.Context,
    arch: Arch = None,
    vm_dir: Annotated[Path | None, typer.Option(help="VM session directory, even outside a project.")] = None,
) -> None:
    """Select the session before the subcommand: its directory, or the architecture of the project's, found then, not
    to need one for --help.
    """
    ctx.obj = vm_dir.absolute() if vm_dir else arch


def directory(ctx: typer.Context) -> Path:
    """Get the selected session, validating that a launch has been recorded."""
    if not isinstance(ctx.obj, Path):
        project = Project.find(ctx.obj)
        ctx.obj = project.state / "vm" / project.arch
    if not (ctx.obj / "session.json").is_file():
        msg = f"no VM session in {ctx.obj}: run xcraft boot first"
        raise RuntimeError(msg)
    vm.Session.model_validate_json((ctx.obj / "session.json").read_text())
    return ctx.obj


def check_port(port: int | None) -> None:
    """Raise when the SSH `port` given is in use on localhost."""
    if port and not vm.port_free(port):
        msg = f"port {port} is in use on localhost: give another one, or none for the first free one from 2222"
        raise typer.BadParameter(msg, param_hint="--ssh-port")


def show(root: Path, session: vm.Session) -> None:
    """Show how to control the VM launched in `root`, and to log in."""
    display.console.print(f"virtual disk: {session.disk}\nVM session: {root}", markup=False)
    display.console.print(f"Control: xcraft vm --vm-dir {root} status", markup=False)
    if session.ssh_port:
        display.console.print(f"SSH when enabled in the guest: ssh -p {session.ssh_port} root@127.0.0.1", markup=False)


@app.command()
def start(
    ctx: typer.Context,
    memory: Annotated[int | None, typer.Option(min=512, help="VM memory in MiB, the session's by default.")] = None,
    cpus: Annotated[int | None, typer.Option(min=1, help="Number of virtual CPUs, the session's by default.")] = None,
    ssh_port: Annotated[
        int | None,
        typer.Option(
            min=0,
            max=65535,
            help="Localhost port forwarded to SSH in the VM: the session's by default, if free, or else the first free "
            "one from 2222. 0 disables it.",
            show_default=False,
        ),
    ] = None,
    headless: Annotated[
        bool, typer.Option("--headless", help="Disable the display window. Use xcraft vm for console access.")
    ] = False,
    background: Annotated[
        bool, typer.Option("--background", help="Launch without a window and return when ready.")
    ] = False,
) -> None:
    """Start the VM again from its disk, without the ISO, like an installed host, with its firmware variables and the
    options of xcraft boot, but those given.
    """
    check_port(ssh_port)
    root = directory(ctx)
    session = vm.Session.model_validate_json((root / "session.json").read_text())
    if not session.launch:
        msg = f"the VM of {root} was launched by an older xcraft, without its options: run xcraft boot again"
        raise RuntimeError(msg)
    if not session.disk.is_file():
        msg = f"no virtual disk at {session.disk}"
        raise RuntimeError(msg)
    vm.check_stopped(root)
    vm.check_tools(session.launch.accel, new_disk=False)
    launch = session.launch.model_copy(
        update={"memory": memory or session.launch.memory, "cpus": cpus or session.launch.cpus}
    )
    if ssh_port is None:
        ssh_port = session.ssh_port if not session.ssh_port or vm.port_free(session.ssh_port) else vm.free_port()
    cmd = vm.qemu_command(root, launch, disk=session.disk, iso=None, ssh_port=ssh_port, headless=headless or background)
    session = session.model_copy(update={"iso": None, "ssh_port": ssh_port, "command": cmd, "launch": launch})
    proc, _ = vm.launch(root, session)
    show(root, session)
    if not background:
        vm.wait(proc, root)


@app.command()
def status(ctx: typer.Context) -> None:
    """Print launch details and current QEMU status as JSON."""
    root = directory(ctx)
    session = vm.Session.model_validate_json((root / "session.json").read_text())
    state: dict[str, object] = session.model_dump(mode="json")
    state["directory"] = str(root)
    state["status"] = "stopped"
    if vm.running(root):
        with vm.Monitor(root) as monitor:
            state["qemu"] = monitor.execute("query-status")
        state["status"] = "running"
    typer.echo(json.dumps(state, indent=2))


@app.command()
def stop(ctx: typer.Context) -> None:
    """Stop QEMU immediately, retaining its disk, firmware variables, and logs."""
    root = directory(ctx)
    if vm.running(root):
        with vm.Monitor(root) as monitor:
            monitor.execute("quit")
    typer.echo("stopped")


@app.command()
def screenshot(
    ctx: typer.Context,
    output: Annotated[
        Path | None, typer.Argument(help="PNG destination. Defaults to screen.png in the VM directory.")
    ] = None,
) -> None:
    """Save the VM's graphical display as a PNG."""
    root = directory(ctx)
    output = (output or root / "screen.png").absolute()
    with vm.Monitor(root) as monitor:
        monitor.execute("screendump", {"filename": str(output), "format": "png"})
    typer.echo(output)


@app.command()
def text(
    ctx: typer.Context,
    value: Annotated[str, typer.Argument(help="Text to send.")],
    enter: Annotated[bool, typer.Option("--enter", help="Press Enter after the text.")] = False,
    escape: Annotated[
        bool, typer.Option("--escape", help=r"Decode backslash escapes such as \t, \r, and \x1b.")
    ] = False,
    physical: Annotated[bool, typer.Option("--physical", help="Type through the US-layout physical keyboard.")] = False,
) -> None:
    """Send serial input, or type into a graphical diagnostic terminal with --physical."""
    root = directory(ctx)
    if escape:
        value = codecs.escape_decode(value.encode())[0].decode()
    if physical:
        with vm.Monitor(root) as monitor:
            vm.type_text(monitor, value + ("\n" if enter else ""))
    else:
        vm.send(root, (value + ("\r" if enter else "")).encode())


@app.command()
def key(
    ctx: typer.Context, keys: Annotated[list[str], typer.Argument(help="QEMU key codes, e.g. ctrl alt f2.")]
) -> None:
    """Send a physical keyboard chord through QMP."""
    with vm.Monitor(directory(ctx)) as monitor:
        vm.press(monitor, keys)


@app.command()
def console(
    ctx: typer.Context,
    wait: Annotated[str | None, typer.Option(help="Wait for this text on the rendered screen.")] = None,
    timeout: Annotated[float, typer.Option(min=0.1, help="Maximum wait in seconds.")] = 30,
    stable: Annotated[float, typer.Option(min=0, help="Required seconds without serial output.")] = 0.2,
) -> None:
    """Render the current 80x24 serial screen; optionally wait for text and output stability."""
    screen = pyte.Screen(80, 24)
    stream = pyte.ByteStream(screen)
    deadline = time.monotonic() + timeout
    changed = time.monotonic()
    root = directory(ctx)
    with (root / "serial.log").open("rb") as log:
        while True:
            if data := log.read():
                stream.feed(data)
                changed = time.monotonic()
            content = "\n".join(screen.display)
            if (wait is None or wait in content) and time.monotonic() - changed >= stable:
                typer.echo(content)
                return
            if time.monotonic() >= deadline:
                typer.echo(content)
                output = root / f"timeout-{time.time_ns()}"
                vm.collect(root, output)
                msg = f"console did not stabilize with {wait!r}; diagnostics: {output}"
                raise RuntimeError(msg)
            time.sleep(0.05)


@app.command()
def logs(
    ctx: typer.Context, output: Annotated[Path, typer.Argument(help="New directory for the diagnostics bundle.")]
) -> None:
    """Collect serial/QEMU logs, launch details, and a screenshot. Guest files require guest access."""
    vm.collect(directory(ctx), output.absolute())
    typer.echo(output.absolute())
