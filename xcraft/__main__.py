"""xcraft command line."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
from typing import Annotated

import typer
from rich.logging import RichHandler

from xcraft import process
from xcraft.commands import (
    ack,
    build,
    bump,
    clean,
    doctor,
    fetch,
    graph,
    impacts,
    import_,
    iso,
    key,
    lock,
    log,
    patchset,
    publish,
    push,
    repo,
    shell,
    status,
)
from xcraft.display import console

app = typer.Typer(
    help="Build the XCP-ng distribution.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
for panel, commands in {
    "Building": [build.build, repo.repo, shell.shell, log.log, graph.graph, status.status, clean.clean],
    "Packages": [bump.bump, patchset.patchset],
    "Upstream packages": [lock.lock, impacts.impacts, ack.ack],
    "Publishing": [publish.publish, iso.iso],
    "Host": [doctor.doctor],
}.items():
    for command in commands:
        app.command(rich_help_panel=panel)(command)
app.command("st", hidden=True)(status.status)
app.command("import", rich_help_panel="Packages")(import_.import_)
app.add_typer(push.app, name="push", rich_help_panel="Sharing")
app.add_typer(fetch.app, name="fetch", rich_help_panel="Sharing")
app.add_typer(key.app, name="key", rich_help_panel="Publishing")


@app.callback()
def options(
    trace: Annotated[bool, typer.Option("--trace", help="Show stack traces on errors.")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Show the commands xcraft runs.")] = False,
) -> None:
    """Build the XCP-ng distribution."""
    # --trace and --debug are handled in main(), they are declared here for --help


def main() -> None:
    """Run the command line, showing errors without a stack trace unless --trace is given."""
    socket.setdefaulttimeout(60)  # the downloads, from servers not answering anymore: boto3 has its own
    if "--debug" in sys.argv:
        # above the progress of the activities, like the other messages
        handler = RichHandler(console=console, show_time=False, show_level=False, show_path=False, markup=False)
        process.log.addHandler(handler)
        process.log.setLevel(logging.DEBUG)
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        if "--trace" in sys.argv:
            raise
        console.print(f"error: {exc}", style="red", markup=False)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            console.print(exc.stderr.strip(), markup=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
