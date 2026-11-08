"""xcraft command line."""

from __future__ import annotations

import inspect
import logging
import socket
import subprocess
import sys
from typing import TYPE_CHECKING, Annotated

import typer
from typer import rich_utils

from xcraft import process, remote
from xcraft.commands import (
    boot,
    build,
    bump,
    clean,
    contents,
    doctor,
    fetch,
    graph,
    history,
    import_,
    iso,
    key,
    lock,
    log,
    patchset,
    publish,
    push,
    repo,
    reuse,
    shell,
    status,
    unpublish,
    vm,
)
from xcraft.display import console, known_commands

if TYPE_CHECKING:
    from collections.abc import Iterator

# Typer's Rich help does not use Click's max_content_width.
rich_utils.MAX_WIDTH = min(120, console.width, rich_utils.MAX_WIDTH or 120)

app = typer.Typer(
    help="Build the XCP-ng distribution.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
for panel, commands in {
    "Building": [
        build.build,
        repo.repo,
        shell.shell,
        log.log,
        graph.graph,
        contents.contents,
        status.status,
        clean.clean,
    ],
    "Packages": [bump.bump, patchset.patchset, history.history],
    "Upstream packages": [lock.lock, reuse.reuse],
    "Publishing": [iso.iso, boot.boot, unpublish.unpublish],
    "Host": [doctor.doctor],
}.items():
    for command in commands:
        app.command(rich_help_panel=panel)(command)
app.command("st", hidden=True)(status.status)
app.command("import", rich_help_panel="Packages")(import_.import_)
app.add_typer(push.app, name="push", rich_help_panel="Sharing")
app.add_typer(fetch.app, name="fetch", rich_help_panel="Sharing")
app.add_typer(publish.app, name="publish", rich_help_panel="Publishing")
app.add_typer(key.app, name="key", rich_help_panel="Publishing")
app.add_typer(vm.app, name="vm", rich_help_panel="Host")


@app.callback()
def options(
    trace: Annotated[bool, typer.Option("--trace", help="Show stack traces on errors.")] = False,  # noqa: ARG001
    debug: Annotated[bool, typer.Option("--debug", help="Show the commands xcraft runs.")] = False,  # noqa: ARG001
) -> None:
    """Build the XCP-ng distribution."""
    # --trace and --debug are handled in main(), they are declared here for --help
    remote.forget()  # what another command, in the same process like the tests, read


def _reflowed(doc: str | None) -> str | None:
    """Return the docstring `doc` with each paragraph on one line, for typer, which wraps them to the terminal, but
    keeps their line breaks otherwise.
    """
    return doc and "\n\n".join(" ".join(p.split("\n")) for p in inspect.cleandoc(doc).split("\n\n"))


def _reflow(typer_app: typer.Typer) -> None:
    """Reflow the help of the commands of `typer_app`, at all depths, see _reflowed()."""
    infos = [*typer_app.registered_commands, *filter(None, [typer_app.registered_callback])]
    for info in infos:
        info.help = info.help or _reflowed(getattr(info.callback, "__doc__", None))
    for group in typer_app.registered_groups:
        if group.typer_instance:
            _reflow(group.typer_instance)


_reflow(app)


class _Handler(logging.Handler):
    """Show the log messages on whole lines, above the progress of the activities, like the other messages."""

    def emit(self, record: logging.LogRecord) -> None:
        console.print(self.format(record), markup=False)


def _names(command: object) -> Iterator[str]:
    """Return the names of the subcommands of `command`, at all depths."""
    for name, sub in getattr(command, "commands", {}).items():
        yield name
        yield from _names(sub)


known_commands(_names(typer.main.get_command(app)))


def main() -> None:
    """Run the command line, showing errors without a stack trace unless --trace is given."""
    socket.setdefaulttimeout(60)  # the downloads, from servers not answering anymore: boto3 has its own
    args = sys.argv[1:]
    end = args.index("--") if "--" in args else len(args)
    if "--debug" in args[:end]:
        process.log.addHandler(_Handler())
        process.log.setLevel(logging.DEBUG)
    try:
        app(args=[arg for arg in args[:end] if arg != "--debug"] + args[end:])
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
