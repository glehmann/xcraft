"""Run a command without the network, like koji and mock run the builds: with the loopback interface only, up, and
without gaining privileges, so that sudo cannot change the build root once recorded.

Run in the build environment, as the user of the command: ``offline.py <command>...``. It runs again as root, with
sudo, for a user namespace mapping the same ids as the container, all of them: with only the user's, changing the
owner of a file to root would fail with EINVAL, which fakeroot, unlike EPERM, does not ignore. Root maps them from
outside the new user namespace, which takes CAP_SETUID there, and in it, brings up the loopback interface of a new
network namespace, then runs the command as the user, with its environment, which sudo would have trimmed.

Under emulation, the command runs in the namespaces of the container, which has no network either, but for xcraft
shell: qemu-user, multithreaded, cannot create a user namespace.
"""

import ctypes
import errno
import fcntl
import json
import os
import signal
import socket
import struct
import sys
from pathlib import Path

SIOCGIFFLAGS, SIOCSIFFLAGS, IFF_UP = 0x8913, 0x8914, 0x1
PR_SET_NO_NEW_PRIVS = 38

if os.getuid():
    command = [str(os.getuid()), str(os.getgid()), json.dumps(dict(os.environ)), *sys.argv[1:]]
    os.execvp("sudo", ["sudo", sys.executable, __file__, *command])  # noqa: S606
uid, gid, environment, *command = sys.argv[1:]
unshared, mapped = os.pipe(), os.pipe()
# each process closes the write end of the pipe it reads: an EOF when the other one dies first
if pid := os.fork():
    os.close(unshared[1])
    # the ranges of ids of the container, mapped to themselves, which only a process outside the new user namespace
    # can do, with CAP_SETUID there
    if os.read(unshared[0], 1) == b"x":
        for name in ("uid_map", "gid_map"):
            ranges = [line.split() for line in Path(f"/proc/self/{name}").read_text().splitlines()]
            Path(f"/proc/{pid}/{name}").write_text("".join(f"{r[0]} {r[0]} {r[2]}\n" for r in ranges))
        os.write(mapped[1], b"x")
    sys.exit(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
os.close(mapped[1])
try:
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
except OSError as e:
    if e.errno != errno.EINVAL:
        raise
    os.write(unshared[1], b"-")
else:
    os.write(unshared[1], b"x")
    os.read(mapped[0], 1)
    with socket.socket() as s:
        flags = struct.unpack("16sH", fcntl.ioctl(s, SIOCGIFFLAGS, struct.pack("16sH", b"lo", 0)))[1]
        fcntl.ioctl(s, SIOCSIFFLAGS, struct.pack("16sH", b"lo", flags | IFF_UP))
os.setgroups([])
os.setgid(int(gid))
os.setuid(int(uid))
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
    raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
# Python ignores them, and the command would inherit it, like rpmbuild does: writing to a closed pipe, like sed in a
# pipeline whose reader exits first, would fail with EPIPE, and an error, rather than being killed quietly
for sig in (signal.SIGPIPE, signal.SIGXFSZ):
    signal.signal(sig, signal.SIG_DFL)
os.execvpe(command[0], command, json.loads(environment))  # noqa: S606, the command of the caller
