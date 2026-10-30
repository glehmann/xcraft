"""Control a persistent local VM without the launching terminal."""

from __future__ import annotations

import codecs
import json
import time
from pathlib import Path
from typing import Annotated

import pyte
import typer

from xcraft import vm
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
