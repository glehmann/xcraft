"""The installation ISO, made from a snapshot of the distribution, like XCP-ng's create-install-image does.

Its repository is the closure of xcp-ng-deps in the snapshot, and install.img, the installer's root filesystem, is
installed from the snapshot too, which has its packages. The tools making it, which are not in the build
environment image, are installed in its containers, in the versions of the lock.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from rich.markup import escape

from xcraft import buildid, remote
from xcraft.cache import inputs
from xcraft.container import DNF_LOCK, buildenv_hash, run
from xcraft.display import activity, console
from xcraft.lock import Locked, load_lock, save_lock
from xcraft.project import INTERNAL
from xcraft.publish import REPO, snapshot
from xcraft.resolution import lock_installed, prepare, resolve_image
from xcraft.signing import signing_key
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from xcraft.project import Project, Remote
    from xcraft.signing import Signer

TOOLS = ["dosfstools", "memtest86+", "mtools", "syslinux-nonlinux", "xorriso"]
_INSTALL_TOOLS = f"{DNF_LOCK} dnf -q -y --setopt=install_weak_deps=False install {' '.join(TOOLS)}"
KEY_FILE = "RPM-GPG-KEY-xcpng"
"""The public key of the signed repository of the ISO, as named in its .treeinfo."""


class Image(BaseModel):
    """The cached ISO's version, of the distribution, and content digest."""

    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _templates(project: Project) -> str:
    """Hash template files and symlink destinations, including masked units pointing to /dev/null."""
    root = project.root / "iso"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            digest.update(f"{path.relative_to(root)}\0link\0{path.readlink()}\0".encode())
        elif path.is_file():
            digest.update(f"{path.relative_to(root)}\0file\0{bool(path.stat().st_mode & 0o111)}\0".encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def iso_key(project: Project, *, netinstall: bool, volid: str | None, signer: Signer | None) -> str:
    """Hash checkout inputs without preparing containers or accessing signing secrets."""
    lock = load_lock(project)
    config = project.config.model_dump(
        exclude={"remote", "logo", "import_", "sign", "ccache_size", "same_version", "publish"}
    )
    config["extra"] = project.config.publish.extra
    data = [
        project.platform,
        config,
        inputs(project),
        # the numbers of the builds, in their releases, and the reuses of builds
        {name: i.model_dump() for name, i in buildid.read(project).items()},
        _templates(project),
        lock.sources,
        *[getattr(lock, f).of(project.arch) for f in ("requires", "provides", "buildrequires")],
        lock.runtime.get(project.arch, []),
        lock.iso.get(project.arch, []),
        buildenv_hash(),
        [
            Path(__file__).with_name(module).read_text()
            for module in (
                "iso.py",
                "publish.py",
                "container.py",
                "cache.py",
                "specs.py",
                "resolution.py",
                "localrepo.py",
                "repodata.py",
                "signing.py",
            )
        ],
        netinstall,
        volid,
        signer.key.upper() if signer else None,
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def cached_iso(project: Project, key: str) -> Path:
    """Return the directory of a cached ISO."""
    return project.state / "cache/isos" / project.arch / key


def iso_inputs(project: Project, *, signer: Signer | None) -> dict[str, str]:
    """Return the checkout keys of the full and netinstall ISOs."""
    return {
        f"{project.arch}/{'netinstall' if netinstall else 'full'}": iso_key(
            project, netinstall=netinstall, volid=None, signer=signer
        )
        for netinstall in (False, True)
    }


def iso_id(project: Project, *, signer: Signer | None) -> str:
    """Return the identifier of the ISOs of the checkout, the same for all the architectures, full or netinstall: the
    beginning of a hash of the keys of all of them, which cover what they are made of.
    """
    keys = sorted(key for platform in project.all_platforms() for key in iso_inputs(platform, signer=signer).values())
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:6]


def iso_name(project: Project, version: str, *, netinstall: bool, signer: Signer | None) -> str:
    """Return the file name of an ISO of the distribution `version`, with its identifier and architecture."""
    return f"xcp-ng-{version}-{iso_id(project, signer=signer)}-{project.arch}{'-netinstall' if netinstall else ''}.iso"


def fetch_isos(project: Project, *, signer: Signer | None) -> bool:
    """Put the full and netinstall ISOs of the checkout in the cache, fetched from the remote storage when missing,
    and return whether they are all there.
    """
    try:
        with activity("looking for the ISOs in the remote storage"):
            return all(
                (tree := cached_iso(project, key)).exists() or remote.fetch_tree(project, iso_path(project, key), tree)
                for key in iso_inputs(project, signer=signer).values()
            )
    except remote.RemoteError:
        return False


def iso_path(project: Project, key: str) -> str:
    """Return the input-keyed ISO tree's path in remote storage."""
    return f"{INTERNAL}/isos/{project.arch}/{key}"


def _checkout(project: Project) -> tuple[dict[str, str], str, str]:
    return inputs(project), _templates(project), sha256_of(project.root / "xcraft.toml")


def _image(tree: Path) -> Image:
    image = Image.model_validate_json((tree / "image.json").read_text())
    if sha256_of(tree / "image.iso") != image.sha256:
        msg = f"cached ISO does not match its SHA-256: {tree}"
        raise RuntimeError(msg)
    return image


def _output(project: Project, tree: Path, output: Path | None, *, netinstall: bool, signer: Signer | None) -> Path:
    """Return the cached ISO in `tree` as `output`, or with its name in .xcraft/iso/ by default."""
    image = _image(tree)
    name = iso_name(project, image.version, netinstall=netinstall, signer=signer)
    output = (output or project.state / "iso" / name).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists() or sha256_of(output) != image.sha256:
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".iso-") as tmp:
            part = Path(tmp) / "image.iso"
            shutil.copy2(tree / "image.iso", part)
            part.replace(output)
    return output


def _find(project: Project, key: str, output: Path | None, *, netinstall: bool, signer: Signer | None) -> Path | None:
    tree = cached_iso(project, key)
    if tree.exists():
        return _output(project, tree, output, netinstall=netinstall, signer=signer)
    if not project.config.remote:
        return None
    try:
        with activity("looking for the ISO in the remote storage") as on_line:
            if remote.fetch_tree(project, iso_path(project, key), tree, on_line):
                return _output(project, tree, output, netinstall=netinstall, signer=signer)
    except remote.RemoteError as e:
        console.print(f"[yellow]remote storage unavailable, making the ISO locally[/]: {escape(str(e))}")
    return None


def push_isos(
    project: Project, storage: Remote, *, signer: Signer | None, upload_signer: Signer | None = None
) -> dict[str, tuple[str, str]]:
    """Upload cached ISOs matching the checkout and return their remote paths and digests by output name."""
    count = present = 0
    stored = {}
    for netinstall in (False, True):
        key = iso_key(project, netinstall=netinstall, volid=None, signer=signer)
        tree, path = cached_iso(project, key), iso_path(project, key)
        if not tree.exists():
            continue
        image = _image(tree)
        stored[iso_name(project, image.version, netinstall=netinstall, signer=signer)] = (
            f"{path}/image.iso",
            image.sha256,
        )
        if remote.tree_exists(storage, path, trusted=upload_signer or signer or signing_key(project)):
            present += 1
            continue
        with activity("uploading the ISO", log=True):
            remote.upload_tree(
                storage, path, tree, ["image.iso", "image.json"], signer=upload_signer or signer or signing_key(project)
            )
        remote.update_pages(project, storage, [f"{path}/"])
        count += 1
    console.print(f"{count} ISOs pushed, {present} already in the remote storage")
    return stored


def build_iso(
    project: Project,
    output: Path | None = None,
    *,
    netinstall: bool = False,
    volid: str | None = None,
    signer: Signer | None = None,
) -> Path:
    """Build or fetch the current packages, assemble a local snapshot, and make its installation ISO."""
    with project.lock():
        key = iso_key(project, netinstall=netinstall, volid=volid, signer=signer)
        if found := _find(project, key, output, netinstall=netinstall, signer=signer):
            return found
        checkout = _checkout(project)
        prepare(project)
        dest = (project.state / "publish").absolute()
        snapshot([project], dest, signer=signer, storage=None, build=False)
        if checkout != _checkout(project):
            msg = "ISO inputs changed while assembling the snapshot: run again"
            raise RuntimeError(msg)
        return make_iso(project, dest / REPO / project.arch, output, netinstall=netinstall, volid=volid, signer=signer)


def _lock_tools(project: Project, recorded: dict[str, Locked]) -> bool:
    """Add the upstream packages installed with the tools to the lock, and return whether they have the locked
    versions: not when it locked other ones.
    """
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


def make_iso(
    project: Project,
    snapshot: Path,
    output: Path | None,
    *,
    netinstall: bool,
    volid: str | None,
    signer: Signer | None,
) -> Path:
    """Reuse, fetch or make an ISO, storing it under its final checkout inputs."""
    with project.lock():
        key = iso_key(project, netinstall=netinstall, volid=volid, signer=signer)
        if found := _find(project, key, output, netinstall=netinstall, signer=signer):
            return found
        checkout = _checkout(project)
        made, version = _make_iso(project, snapshot, netinstall=netinstall, volid=volid, signer=signer)
        if checkout != _checkout(project):
            msg = "ISO inputs changed while making the image: run again"
            raise RuntimeError(msg)
        key = iso_key(project, netinstall=netinstall, volid=volid, signer=signer)
        tree = cached_iso(project, key)
        with project.work("cache-iso") as work:
            staged = work / "tree"
            staged.mkdir()
            shutil.move(made, staged / "image.iso")
            (staged / "image.json").write_text(
                Image(version=version, sha256=sha256_of(staged / "image.iso")).model_dump_json()
            )
            tree.parent.mkdir(parents=True, exist_ok=True)
            if not tree.exists():  # of the same inputs, kept from before the lock went back to them
                staged.rename(tree)
        return _output(project, tree, output, netinstall=netinstall, signer=signer)


def _make_iso(
    project: Project, snapshot: Path, *, netinstall: bool, volid: str | None, signer: Signer | None
) -> tuple[Path, str]:
    """Make the ISO of `snapshot`, its binaries for the architecture, in .xcraft/tmp/, and return it, with the
    version of the distribution it installs.
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
            keys = tree / KEY_FILE
            keys.write_text(signer.public_key() + "\n" + keys.read_text())
        info = json.loads((work / "info.json").read_text())
        # hybrid, for BIOS and UEFI, from a CD or a USB stick
        xorriso = (
            f"xorriso -as mkisofs -quiet -o /work/image.iso -V {shlex.quote(volid or info['volid'])} -R -J -joliet-long"
            " -b boot/isolinux/isolinux.bin -c boot/isolinux/boot.cat -no-emul-boot -boot-load-size 4 -boot-info-table"
            " -isohybrid-mbr /usr/share/syslinux/isohdpfx.bin"
            " -eltorito-alt-boot -e boot/efiboot.img -no-emul-boot -isohybrid-gpt-basdat /work/tree"
        )
        run(project, "iso", f"{_INSTALL_TOOLS} && {xorriso}", volumes=volumes, show="making the ISO")
        made = project.state / "tmp" / f"{work.name}.iso"
        shutil.move(work / "image.iso", made)
    return made, info["version"]


def make_isos(project: Project, snapshot: Path, dest: Path, *, signer: Signer | None) -> list[Path]:
    """Make the ISOs of `snapshot`, its binaries for the architecture, with the packages and netinstall, in `dest`,
    the directory of the ISOs of all the architectures, with their detached signatures by `signer`, if any, and return
    them.
    """
    dest.mkdir(parents=True, exist_ok=True)
    isos = []
    for netinstall in (False, True):
        iso = make_iso(project, snapshot, None, netinstall=netinstall, volid=None, signer=signer)
        isos.append(Path(shutil.move(iso, dest / iso.name)))
        if signer:
            signer.sign_file(isos[-1])
    return isos
