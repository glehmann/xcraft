"""xcraft command line."""

from __future__ import annotations

import socket
import subprocess
import sys
from typing import Annotated

import typer

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
app.command(rich_help_panel="Building")(build.build)
app.command(rich_help_panel="Building")(repo.repo)
app.command(rich_help_panel="Building")(shell.shell)
app.command(rich_help_panel="Building")(log.log)
app.command(rich_help_panel="Building")(graph.graph)
app.command(rich_help_panel="Building")(status.status)
app.command("st", hidden=True)(status.status)
app.command(rich_help_panel="Packages")(bump.bump)
app.command(rich_help_panel="Packages")(patchset.patchset)
app.command(rich_help_panel="Upstream packages")(lock.lock)
app.command(rich_help_panel="Upstream packages")(impacts.impacts)
app.command(rich_help_panel="Upstream packages")(ack.ack)
app.command(rich_help_panel="Building")(clean.clean)
app.command("import", rich_help_panel="Packages")(import_.import_)
app.command(rich_help_panel="Publishing")(publish.publish)
app.command(rich_help_panel="Publishing")(iso.iso)
app.command(rich_help_panel="Host")(doctor.doctor)
app.add_typer(push.app, name="push", rich_help_panel="Sharing")
app.add_typer(fetch.app, name="fetch", rich_help_panel="Sharing")
app.add_typer(key.app, name="key", rich_help_panel="Publishing")


@app.callback()
def options(trace: Annotated[bool, typer.Option("--trace", help="Show stack traces on errors.")] = False) -> None:
    """Build the XCP-ng distribution."""
    # --trace is handled in main(), it is declared here for --help


def main() -> None:
    """Run the command line, showing errors without a stack trace unless --trace is given."""
    socket.setdefaulttimeout(60)  # the downloads, from servers not answering anymore: boto3 has its own
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
