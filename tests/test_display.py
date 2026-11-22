"""The display of the commands: their activities, diagnostics and progress, and xcraft log of their logs."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

from tests import TOML, config, xcraft
from xcraft import build
from xcraft.display import TreeProgress, activity, console, diagnostics, print_message_line, read_tail
from xcraft.project import Project

if TYPE_CHECKING:
    from pathlib import Path


def should_throttle_the_progress_lines_of_logged_activities(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = 0
    monkeypatch.setattr("xcraft.display.time.monotonic", lambda: now)
    with activity("publishing", log=True) as update:
        now = 31
        update("first.rpm")
        update("second.rpm")  # less than 30s after the previous line
        now = 62
    output = capsys.readouterr().err
    assert "publishing\n" in output
    assert "publishing: first.rpm" in output
    assert "second.rpm" not in output
    assert "publishing finished in" in output


def should_show_an_activity_still_running_with_its_last_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("xcraft.display._STILL_INTERVAL", 0.1)
    monkeypatch.setenv("CI", "true")
    output = ""
    with activity("[1/1] building foo", still="building foo") as update:
        update("\x1b[1mgcc -c foo.c\x1b[0m")
        for _ in range(100):
            output += capsys.readouterr().err
            if "still" in output:
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"no line telling the activity is still running in 2s: {output!r}")
    # first, then after the interval, with the last line, plain
    assert output.startswith("[1/1] building foo\n  still building foo (0s): gcc -c foo.c\n")


LOG = """\
Installing build dependencies
Executing(%prep): /bin/sh -e /var/tmp/rpm-tmp.1
+ tar xf foo.tar.gz
Executing(%build): /bin/sh -e /var/tmp/rpm-tmp.2
+ make
gcc -c foo.c
foo.c:3: error: oops
make: *** [all] Error 1
"""


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "xcraft.toml").write_text(TOML)
    (tmp_path / ".xcraft/logs").mkdir(parents=True)
    (tmp_path / ".xcraft/build").mkdir()
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    path = tmp_path / ".xcraft/logs/foo.log"
    path.write_text(LOG)
    return path


@pytest.mark.usefixtures("log")
def should_filter_build_logs_by_step_pattern_and_count() -> None:
    assert xcraft("log", "foo") == (0, LOG)
    assert xcraft("log", "foo", "--step", "prep") == (
        0,
        "Executing(%prep): /bin/sh -e /var/tmp/rpm-tmp.1\n+ tar xf foo.tar.gz\n",
    )
    assert xcraft("log", "foo", "--step", "build", "--grep", r"error|Error", "--last", "1") == (
        0,
        "make: *** [all] Error 1\n",
    )
    assert xcraft("log", "foo", "-n", "2") == xcraft("log", "foo", "--last", "2")


@pytest.mark.usefixtures("log")
def should_tell_the_steps_a_build_log_has() -> None:
    assert xcraft("log", "foo", "--step", "install") == (1, "no install step in the log of foo, it has: prep, build")


@pytest.mark.usefixtures("log")
def should_refuse_the_log_of_a_package_never_built() -> None:
    assert xcraft("log", "bar") == (1, "bar has no build log")


def should_follow_a_build_log_until_the_build_ends(log: Path) -> None:
    pid_file = log.parent.parent / "build/foo.pid"
    pid_file.write_text(str(os.getpid()))

    def build() -> None:
        time.sleep(0.5)
        with log.open("a") as f:
            f.write("Wrote: foo-1.0-1.x86_64.rpm\nnot a match\n")
        time.sleep(0.5)
        pid_file.unlink()

    thread = threading.Thread(target=build)
    thread.start()
    # returns once the build is over, with the lines it added
    assert xcraft("log", "foo", "--follow", "--grep", "Wrote|Error") == (
        0,
        "make: *** [all] Error 1\nWrote: foo-1.0-1.x86_64.rpm\n",
    )
    thread.join()


def should_read_the_last_complete_lines_of_a_file(tmp_path: Path) -> None:
    path = tmp_path / "log"
    path.write_text("first line\nsecond\nthird\n")
    assert read_tail(path) == ["first line", "second", "third"]
    assert read_tail(path, size=10) == ["third"]  # without the partial line


def should_show_only_the_errors_and_warnings_of_build_output() -> None:
    shown: list[str] = []
    on_line = diagnostics(shown.append)
    for line in [
        "Executing(%build): /bin/sh -e /var/tmp/rpm-tmp.WXoNig",
        "error: bad date in %changelog: Fri 02 Oct 2026 Yann Dirson <yann.dirson@vates.tech> - 26.4.0-1.3",
        "gcc -O2 -c foo.c",
        "foo.c:3:5: \x1b[01;35mwarning: \x1b[munused variable",
        "foo.c:4:1: error: expected ';'",
        "warning: Macro expanded in comment on line 40: %{_sbindir}/sendmail",
        "Failed to preset unit: Unit mpcount.service does not exist",
        "FAILED: test_foo",
        "make: *** [Makefile:60: test] Error 1",
        "RPM build warnings:",
        "",
        "RPM build errors:",
        "    source_date_epoch_from_changelog set but %changelog is missing",
        "    Bad exit status from /var/tmp/rpm-tmp.WXoNig (%check)",
        "",
        "real\t1m51.217s",
    ]:
        on_line(line)
    assert shown == [
        "error: bad date in %changelog: Fri 02 Oct 2026 Yann Dirson <yann.dirson@vates.tech> - 26.4.0-1.3",
        "foo.c:4:1: error: expected ';'",
        "warning: Macro expanded in comment on line 40: %{_sbindir}/sendmail",
        "FAILED: test_foo",
        "RPM build warnings:",
        "RPM build errors:",
        "    source_date_epoch_from_changelog set but %changelog is missing",
        "    Bad exit status from /var/tmp/rpm-tmp.WXoNig (%check)",
    ]


def should_give_only_the_diagnostics() -> None:
    shown: list[str] = []
    on_line = diagnostics(shown.append)
    for line in ["gcc -O2 -c foo.c", "error: one", "error: two"]:
        on_line(line)
    assert shown == ["error: one", "error: two"]


def should_leave_the_last_terminal_column_free() -> None:
    # of a terminal of 80x24, and once resized
    code = (
        "from xcraft.display import messages\nprint(*messages.size)\nmessages.size = (40, 30)\nprint(*messages.size)\n"
    )
    env = {**os.environ, "COLUMNS": "80", "LINES": "24", "TERM": "xterm"}
    output = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout
    assert output == "79 24\n39 30\n"


def should_print_diagnostics_as_messages_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    on_line = diagnostics(print_message_line)
    on_line("gcc -O2 -c foo.c")
    on_line("warning: File listed twice: /boot/grub2")
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "warning: File listed twice: /boot/grub2\n"


def should_show_colours_without_progress_frames_in_ci() -> None:
    # coloured, but without the frames of the progress: the logs of CI are not terminals
    code = (
        "from xcraft.display import activity, console\n"
        "with activity('building') as on_line:\n"
        "    on_line('a line')\n"
        "console.print('[green]built[/]')\n"
    )
    env = {**os.environ, "FORCE_COLOR": "1"}
    output = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout
    assert output == "\x1b[32mbuilt\x1b[0m\n"


def should_highlight_the_xcraft_commands_in_messages() -> None:
    # those of the command line of xcraft.__main__, imported
    def highlighted(message: str) -> list[str]:
        return [message[span.start : span.end] for span in console.render_str(message).spans]

    assert highlighted("no key: see xcraft key create") == ["xcraft key create"]
    assert highlighted("see xcraft boot and xcraft vm start") == ["xcraft boot", "xcraft vm start"]
    assert highlighted("1 to update: xcraft lock --update updates them") == ["xcraft lock --update"]
    # not the word xcraft alone, nor in other names
    assert highlighted("another xcraft command is building or changing this project") == []
    assert highlighted("the xcraft-build image") == []


def should_show_the_progress_of_all_the_files_of_a_download() -> None:
    lines: list[str] = []
    progress = TreeProgress(lines.append, 3, 7 * 2**20)
    first, second = progress.report("a"), progress.report("b")
    first(1, 2**20, 4 * 2**20)
    second(1, 2**20, 2 * 2**20)
    first(4, 2**20, 4 * 2**20)
    progress.done("a")
    # the files downloading at once shown together, out of the size of all, not each one in turn
    assert lines[-2:] == ["0/3 files, 5.0/7.0 MiB", "1/3 files, 5.0/7.0 MiB"]
    # without the sizes of the files, in a manifest without them
    lines.clear()
    progress = TreeProgress(lines.append, 2)
    progress.report("a")(3, 2**20, 4 * 2**20)
    assert lines == ["0/2 files, 3.0 MiB"]


def should_show_the_diagnostics_of_a_build_after_its_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # of a package without a previous duration, in CI too, padded to the longest name of the builds
    (tmp_path / "xcraft.toml").write_text(TOML)
    for ci in ("", "true"):
        monkeypatch.setenv("CI", ci)
        with build._shown(Project(tmp_path, config()), "trivial", 9) as on_line:
            on_line("gcc -O2 -c foo.c")
            on_line("error: boom")
        assert capsys.readouterr().err == "trivial   | error: boom\n"
