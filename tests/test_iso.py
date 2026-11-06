"""Input-keyed ISO lookup without preparing a build environment."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from tests import PROJECT, TOML
from tests.test_build import xcraft
from xcraft import buildkey, iso, publish
from xcraft.builds import BuildId, load_builds, save_builds
from xcraft.cache import BuildError
from xcraft.container import BUILDENV
from xcraft.iso import Image, cached_iso, checkout_iso, iso_id, iso_key, iso_name
from xcraft.lock import Pin, load_lock, save_lock
from xcraft.project import Iso, Project, Remote
from xcraft.signing import Signer
from xcraft.sources import sha256_of


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    root = tmp_path / "project"
    shutil.copytree(PROJECT, root)
    (root / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return Project.find()


def cache_image(project: Project, image: Path, *, signer: Signer | None = None, netinstall: bool = False) -> Path:
    """Seed a cached image to exercise lookups and transfers independently of distribution assembly."""
    tree = cached_iso(project, iso_key(project, netinstall=netinstall, volid=None, signer=signer))
    tree.mkdir(parents=True)
    shutil.copy2(image, tree / "image.iso")
    (tree / "image.json").write_text(Image(sha256=sha256_of(image)).model_dump_json())
    return tree


def test_iso_inputs(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    def key() -> str:
        return iso_key(project, netinstall=False, volid=None, signer=None)

    original = key()
    project.config.remote["main"] = Remote(url="https://example.com")
    project.config.publish.stage = "other"
    project.config.logo = "https://example.com/logo.png"
    project.config.iso.unattended = "rt_answerfile=file:///answerfile.xml"  # given at boot, not in the ISO
    assert key() == original
    project.config.iso.packages = ["other"]
    assert key() != original
    project.config.iso.packages = []
    assert iso_key(project, netinstall=True, volid=None, signer=None) != original
    assert iso_key(project, netinstall=False, volid="OTHER", signer=None) != original
    assert iso_key(project, netinstall=False, volid=None, signer=Signer("A" * 40)) != original
    for path in (project.root / "base/trivial/trivial.spec", project.root / "iso/tree/config"):
        before = key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed")
        assert key() != before
    before = key()
    lock = load_lock(project)
    lock.sources["kernel"] = "1-1"
    save_lock(project, lock)
    assert key() != before
    # not the bytecode Python leaves next to the scripts of the build environment, which differs by checkout
    before = key()
    bytecode = BUILDENV / "__pycache__/xcraft-test.cpython-0.pyc"
    bytecode.parent.mkdir(exist_ok=True)
    bytecode.write_bytes(b"bytecode")
    try:
        assert key() == before
    finally:
        bytecode.unlink()
    # but any other file of xcraft, like project.py, which gives the default volume id: of a copy, which the other
    # tests do not read
    code = shutil.copytree(Path(iso.__file__).parent, project.root.parent / "xcraft")
    monkeypatch.setattr(iso, "__file__", str(code / "iso.py"))
    before = key()
    (code / "project.py").write_text(f"{(code / 'project.py').read_text()}\n")
    assert key() != before
    # the build IDs: the numbers of the builds, and the reuses of builds
    for accepts in (None, "45" * 32):
        before = key()
        builds = load_builds(project)
        builds.ids["trivial"] = BuildId(number=2, key="01" * 32, accepts=accepts)
        save_builds(project, builds)
        assert key() != before


def test_iso_inputs_of_the_builds(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    def key() -> str:
        return iso_key(project, netinstall=False, volid=None, signer=None)

    # what the build keys cover, before they are computed again, on all the architectures: the pins of the images and
    # of the build roots, even of the same versions, and how xcraft makes the builds
    pin = Pin(evr="1-1", arch="noarch", sha256="1" * 64, source="kernel")
    for arch in (project.arch, "aarch64"):
        for pinned in ("image", "roots"):
            lock = load_lock(project)
            lock.sources["kernel"] = "1-1"
            lock.binaries.setdefault(arch, {})["tool"] = pin
            if pinned == "image":
                lock.image.setdefault(arch, []).append("tool")
            else:
                lock.roots.arches.setdefault(arch, {})["trivial"] = ["tool"]
            save_lock(project, lock)
            before = key()
            lock.binaries[arch]["tool"] = pin.model_copy(update={"sha256": "2" * 64})
            save_lock(project, lock)
            assert key() != before
    before = key()
    monkeypatch.setattr(buildkey, "FORMAT", buildkey.FORMAT + 1)
    assert key() != before


def test_iso_lookup_before_preparation(project: Project, tmp_path: Path) -> None:
    # The cache transport treats ISO contents as opaque bytes. No RPM resolution is available in this project.
    image = tmp_path / "test.iso"
    image.write_bytes(b"cached ISO contents")
    tree = cache_image(project, image)
    assert xcraft("iso", "--locked", "--unsigned")[0] == 0
    output = project.state / "iso" / iso_name(project, netinstall=False, signer=None)
    assert output.read_bytes() == image.read_bytes()
    # the file of the cache, read-only: replaced, rather than changed
    with pytest.raises(PermissionError):
        output.write_bytes(b"changed output")
    output.unlink()
    output.write_bytes(b"changed output")
    assert checkout_iso(project) == output
    assert output.read_bytes() == image.read_bytes()
    assert checkout_iso(project, tmp_path / "other.iso").read_bytes() == image.read_bytes()
    assert (tree / "image.iso").read_bytes() == image.read_bytes()
    (tree / "image.iso").chmod(0o644)
    (tree / "image.iso").write_bytes(b"corrupted cache")
    with pytest.raises(RuntimeError, match="does not match its SHA-256"):
        checkout_iso(project)


def test_iso_config_checked_before_the_snapshot(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "snapshot", lambda *_, **__: pytest.fail("snapshot"))
    with pytest.raises(RuntimeError, match="the ISO needs the installer's packages"):
        checkout_iso(project)
    (project.root / "xcraft.toml").write_text(f'{TOML}installer = ["bash"]\n')
    with pytest.raises(RuntimeError, match="the ISOs are for x86_64, for their boot loaders, not aarch64"):
        checkout_iso(Project.find("aarch64"))
    (project.root / "iso/hook").chmod(0o644)
    with pytest.raises(RuntimeError, match="the ISO needs the hook of the project"):
        checkout_iso(Project.find("x86_64_v2"))


def test_iso_not_cached_when_the_lock_changed_while_making_it(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    # like a checkout of another lock, while the ISO of the one before is assembled
    key = iso_key(project, netinstall=False, volid=None, signer=None)

    def assembled(project: Project, *_: object, **__: object) -> Path:
        lock = load_lock(project)
        lock.sources["kernel"] = "1-1"
        save_lock(project, lock)
        (made := project.state / "tmp/made.iso").parent.mkdir(parents=True, exist_ok=True)
        made.write_bytes(b"ISO of the lock before")
        return made

    monkeypatch.setattr(iso, "_assemble_iso", assembled)
    with pytest.raises(RuntimeError, match="ISO inputs changed while making the image"):
        iso._snapshot_iso(
            project, project.root, publish.current_checkout(project), None, netinstall=False, volid=None, signer=None
        )
    assert not cached_iso(project, key).exists()
    assert not cached_iso(project, iso_key(project, netinstall=False, volid=None, signer=None)).exists()


def test_iso_not_made_of_a_snapshot_of_another_checkout(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    # like a git checkout after the snapshot, before its ISO is made: its key would be the new checkout's
    checkout = publish.current_checkout(project)
    lock = load_lock(project)
    lock.sources["kernel"] = "1-1"
    save_lock(project, lock)
    monkeypatch.setattr(iso, "_assemble_iso", lambda *_, **__: pytest.fail("assembled"))
    with pytest.raises(BuildError, match="the checkout changed while making the snapshot"):
        iso._snapshot_iso(project, project.root, checkout, None, netinstall=False, volid=None, signer=None)


def test_iso_masked_units_invalidate_cache(project: Project) -> None:
    def key() -> str:
        return iso_key(project, netinstall=False, volid=None, signer=None)

    original = key()
    unit = project.root / "iso/installimg/etc/systemd/system/masked.service"
    unit.symlink_to("/dev/null")
    masked = key()
    assert masked != original
    unit.unlink()
    unit.symlink_to("/another/missing/target")
    assert key() != masked
    unit.unlink()
    assert key() == original


def test_iso_names_are_not_paths() -> None:
    with pytest.raises(ValueError, match="version"):
        Iso(name="toy", version="../../outside")


def test_iso_names(project: Project) -> None:
    full = iso_name(project, netinstall=False, signer=None)
    assert re.fullmatch(rf"toy-1\.0-[0-9a-f]{{6}}-{project.arch}\.iso", full)
    # the same identifier for the netinstall ISO, and those of the other architectures
    ident = full.split("-")[2]
    assert iso_name(project, netinstall=True, signer=None) == f"toy-1.0-{ident}-{project.arch}-netinstall.iso"
    for platform in project.all_platforms():
        assert iso_id(platform, signer=None) == ident
    # another one for other contents
    (project.root / "base/trivial/trivial.spec").write_text("changed")
    assert iso_id(project, signer=None) != ident
