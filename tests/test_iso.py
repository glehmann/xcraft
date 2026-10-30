"""Input-keyed ISO lookup without preparing a build environment."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from tests import PROJECT, TOML
from tests.test_build import xcraft
from xcraft.container import BUILDENV
from xcraft.iso import Image, build_iso, cached_iso, iso_id, iso_key, iso_name
from xcraft.lock import load_lock, save_lock
from xcraft.project import Project, Remote
from xcraft.signing import Signer
from xcraft.sources import sha256_of


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    root = tmp_path / "project"
    shutil.copytree(PROJECT, root)
    (root / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return Project.find()


def cache_image(project: Project, image: Path, *, signer: Signer | None = None) -> Path:
    """Seed a cached image to exercise lookups and transfers independently of distribution assembly."""
    tree = cached_iso(project, iso_key(project, netinstall=False, volid=None, signer=signer))
    tree.mkdir(parents=True)
    shutil.copy2(image, tree / "image.iso")
    (tree / "image.json").write_text(Image(version="8.99.0", sha256=sha256_of(image)).model_dump_json())
    return tree


def test_iso_inputs(project: Project) -> None:
    def key() -> str:
        return iso_key(project, netinstall=False, volid=None, signer=None)

    original = key()
    project.config.remote["main"] = Remote(url="https://example.com")
    project.config.publish.stage = "other"
    project.config.logo = "https://example.com/logo.png"
    assert key() == original
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
    # the build IDs: the numbers of the builds, and the reuses of builds
    for text in ("2 0123", "2 0123 accepts 4567"):
        before = key()
        (project.root / ".buildid").mkdir(exist_ok=True)
        (project.root / ".buildid/trivial").write_text(f"{text}\n")
        assert key() != before


def test_iso_lookup_before_preparation(project: Project, tmp_path: Path) -> None:
    # The cache transport treats ISO contents as opaque bytes. No RPM resolution is available in this project.
    image = tmp_path / "test.iso"
    image.write_bytes(b"cached ISO contents")
    tree = cache_image(project, image)
    assert xcraft("iso", "--locked", "--unsigned")[0] == 0
    output = project.state / "iso" / iso_name(project, "8.99.0", netinstall=False, signer=None)
    assert output.read_bytes() == image.read_bytes()
    output.write_bytes(b"changed output")
    assert build_iso(project) == output
    assert output.read_bytes() == image.read_bytes()
    assert build_iso(project, tmp_path / "other.iso").read_bytes() == image.read_bytes()
    assert (tree / "image.iso").read_bytes() == image.read_bytes()
    (tree / "image.iso").write_bytes(b"corrupted cache")
    with pytest.raises(RuntimeError, match="does not match its SHA-256"):
        build_iso(project)


def test_iso_masked_units_invalidate_cache(project: Project) -> None:
    def key() -> str:
        return iso_key(project, netinstall=False, volid=None, signer=None)

    original = key()
    unit = project.root / "iso/installimg/etc/systemd/system/masked.service"
    unit.parent.mkdir(parents=True)
    unit.symlink_to("/dev/null")
    masked = key()
    assert masked != original
    unit.unlink()
    unit.symlink_to("/another/missing/target")
    assert key() != masked
    unit.unlink()
    assert key() == original


def test_iso_metadata_path(project: Project, tmp_path: Path) -> None:
    image = tmp_path / "test.iso"
    image.write_bytes(b"image")
    tree = cache_image(project, image)
    (tree / "image.json").write_text('{"version":"../../outside","sha256":"' + sha256_of(image) + '"}')
    with pytest.raises(ValueError, match="version"):
        build_iso(project)


def test_iso_names(project: Project) -> None:
    full = iso_name(project, "8.99.0", netinstall=False, signer=None)
    assert re.fullmatch(rf"xcp-ng-8\.99\.0-[0-9a-f]{{6}}-{project.arch}\.iso", full)
    # the same identifier for the netinstall ISO, and those of the other architectures
    ident = full.split("-")[3]
    assert (
        iso_name(project, "8.99.0", netinstall=True, signer=None)
        == f"xcp-ng-8.99.0-{ident}-{project.arch}-netinstall.iso"
    )
    for platform in project.all_platforms():
        assert iso_id(platform, signer=None) == ident
    # another one for other contents
    (project.root / "base/trivial/trivial.spec").write_text("changed")
    assert iso_id(project, signer=None) != ident
