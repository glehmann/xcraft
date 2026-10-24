"""The installation ISO, made from a snapshot of the distribution, like XCP-ng's create-install-image does.

Its repository is the closure of xcp-ng-deps in the snapshot, and install.img, the installer's root filesystem, is
installed from the snapshot too, which has its packages. The tools making it, which are not in the build
environment image, are installed in its containers, in the versions of the lock.
"""

from __future__ import annotations

import json
import shlex
import shutil
from typing import TYPE_CHECKING

from xcraft.container import DNF_LOCK, run
from xcraft.lock import Locked, load_lock, save_lock
from xcraft.resolution import lock_installed, resolve_image

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project
    from xcraft.signing import Signer

TOOLS = ["dosfstools", "memtest86+", "mtools", "syslinux-nonlinux", "xorriso"]
_INSTALL_TOOLS = f"{DNF_LOCK} dnf -q -y --setopt=install_weak_deps=False install {' '.join(TOOLS)}"
KEY_FILE = "RPM-GPG-KEY-xcpng"
"""The public key of the signed repository of the ISO, as named in its .treeinfo."""


def _lock_tools(project: Project, recorded: dict[str, Locked]) -> bool:
    """Add the upstream packages installed with the tools to the lock, and return whether they have the locked
    versions: not when it locked other ones.
    """  # noqa: D205
    image = set(resolve_image(project).image)
    installed = {name: p for name, p in recorded.items() if name not in image}
    if not lock_installed(project, installed):
        return False
    with project.lock():
        lock = load_lock(project)
        lock.iso[project.arch] = sorted(installed)
        save_lock(project, lock)
    return True


def _install_tools(project: Project) -> None:
    """Lock the upstream packages of the tools making the ISO, installed until they have the locked versions."""
    for _ in range(3):
        with project.work("tools") as work:
            run(
                project,
                "tools",
                f"{_INSTALL_TOOLS} && {DNF_LOCK} python3 /xcraft/record.py /work/tools.json",
                volumes=[f"{work}:/work"],
                show="installing the tools making the ISO",
            )
            recorded = json.loads((work / "tools.json").read_text())["installed"]
        if _lock_tools(project, {name: Locked.model_validate(p) for name, p in recorded.items()}):
            return
    msg = "the tools making the ISO are not available in the versions locked for all the architectures"
    raise RuntimeError(msg)


def make_iso(  # noqa: PLR0913
    project: Project,
    snapshot: Path,
    output: Path | None,
    *,
    netinstall: bool,
    volid: str | None,
    signer: Signer | None,
) -> Path:
    """Make the ISO of `snapshot`, its binaries for the architecture, as `output`, or in .xcraft/iso/ by default.

    Return the path of the ISO.
    """
    iso = project.config.iso
    _install_tools(project)  # in the versions of the lock, which the ISO is then made with
    with project.work("iso") as work:
        (work / "config.json").write_text(
            json.dumps({
                "snapshot": "/snapshot", "templates": "/templates", "tree": "/work/tree", "installer": iso.installer,
                "packages": iso.packages, "netinstall": netinstall, "signed": bool(signer),
            })
        )  # fmt: skip
        volumes = [f"{snapshot}:/snapshot:ro", f"{project.root / 'iso'}:/templates:ro", f"{work}:/work"]
        run(
            project,
            "iso",
            f"{_INSTALL_TOOLS} && sudo python3 /xcraft/iso.py /work/config.json",
            volumes=volumes,
            show="making the tree of the ISO",
        )
        tree = work / "tree"
        if signer and not netinstall:
            signer.sign_file(tree / "repodata/repomd.xml")
            (tree / KEY_FILE).write_text(signer.public_key())
        info = json.loads((work / "info.json").read_text())
        suffix = "-netinstall" if netinstall else ""
        output = (output or project.state / "iso" / f"xcp-ng-{info['version']}{suffix}.iso").absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        # hybrid, for BIOS and UEFI, from a CD or a USB stick
        xorriso = (
            f"xorriso -as mkisofs -quiet -o /work/image.iso -V {shlex.quote(volid or info['volid'])} -R -J -joliet-long"
            " -b boot/isolinux/isolinux.bin -c boot/isolinux/boot.cat -no-emul-boot -boot-load-size 4 -boot-info-table"
            " -isohybrid-mbr /usr/share/syslinux/isohdpfx.bin"
            " -eltorito-alt-boot -e boot/efiboot.img -no-emul-boot -isohybrid-gpt-basdat /work/tree"
        )
        run(project, "iso", f"{_INSTALL_TOOLS} && {xorriso}", volumes=volumes, show="making the ISO")
        shutil.move(work / "image.iso", output)
    return output
