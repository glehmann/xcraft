"""xcraft repo: the repository of the builds, and the test hosts it is copied to."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import typer

from tests import xcraft
from xcraft.commands.repo import _connected, _Host, _host, _kept


def test_packages_kept_by_test_hosts() -> None:
    rpms = ["xen-4.17-1+b3~2.dev.x86_64.rpm", "xen-libs-4.17-1+b3~2.dev.x86_64.rpm", "qemu-9-1+b5~1.dev.x86_64.rpm"]
    installed = [
        "xen-4.17-1+b3~2.dev.x86_64",  # upgraded
        "xen-libs-4.17-1+b3~4.qcow2.x86_64",  # newer, from another identifier
        "package qemu is not installed",
    ]
    assert _kept(installed, rpms) == [("xen-libs-4.17-1+b3~4.qcow2.x86_64", "xen-libs-4.17-1+b3~2.dev.x86_64")]


def test_test_hosts_with_their_ports() -> None:
    assert _host("root@h") == _Host("root@h", "root@h")
    assert _host("ssh://root@127.0.0.1:2222") == _Host("ssh://root@127.0.0.1:2222", "root@127.0.0.1", ["-p", "2222"])
    assert _host("ssh://h") == _Host("ssh://h", "h")
    for bad in ("-oProxyCommand=x", "ssh://-oProxyCommand=x", "ssh://-u@h", "ssh://h:port", "ssh://h/path", "ssh://"):
        with pytest.raises(typer.BadParameter, match="not a host"):
            _host(bad)


@pytest.mark.skipif(not shutil.which("sshd", path="/usr/sbin:/usr/bin"), reason="needs sshd")
def test_test_host_connected_once(tmp_path: Path) -> None:
    # a real sshd on localhost, as this user, which the commands of a copy reach through one connection
    for key in ("host", "user"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", tmp_path / key], check=True)
    shutil.copy(tmp_path / "user.pub", tmp_path / "authorized_keys")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (tmp_path / "sshd_config").write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {tmp_path / 'host'}\nAuthorizedKeysFile "
        f"{tmp_path / 'authorized_keys'}\nPasswordAuthentication no\nUsePAM no\nStrictModes no\n"
    )
    sshd = shutil.which("sshd", path="/usr/sbin:/usr/bin")
    assert sshd
    log = tmp_path / "sshd.log"
    with subprocess.Popen([sshd, "-D", "-f", tmp_path / "sshd_config", "-E", log]) as server:
        try:
            options = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]
            options += ["-i", str(tmp_path / "user"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
            host = _Host("test", "127.0.0.1", ["-p", str(port), *options])
            for _ in range(100):  # until it listens
                if subprocess.run([*host.ssh("true")], capture_output=True, check=False).returncode == 0:
                    break
                time.sleep(0.1)
            before = log.read_text().count("Accepted publickey")
            with _connected(host) as shared:
                for _ in range(3):
                    subprocess.run(shared.ssh("true"), check=True)
                control = Path(shared.options[shared.options.index("-o", len(host.options)) + 1].split("=", 1)[1])
            assert log.read_text().count("Accepted publickey") == before + 1
            assert not control.parent.exists()
        finally:
            server.terminate()


def test_repo_host_options(root: Path) -> None:
    for args, error in [
        (["--host", "h", "--final"], "--host, --vm and --upgrade are for the development builds of the given packages"),
        (["--upgrade"], "--upgrade needs --host or --vm"),
        (["--host", "h", "--baseurl", "http://x/"], "with --host or --vm, the hosts find the repository in"),
        (["--host", "ssh://root@h:port"], "not a host: ssh://root@h:port, as ssh://[user@]host[:port]"),
        (["--vm"], "no VM running in"),
    ]:
        code, output = xcraft("repo", "trivial", *args)
        assert code == 2
        assert error in " ".join(output.replace("│", "").split())
