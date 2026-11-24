"""The remote storage, with a Garage server, which serves the public reads with its website endpoint: what is pushed
to it and fetched from it, whom it trusts, and its pages.
"""

from __future__ import annotations

import contextlib
import fcntl
import http.client
import http.server
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import keyring.core
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from tests import (
    ARCH,
    REMOTE,
    S3_PORT,
    STAGE,
    WEB,
    WEB_PORT,
    cache_image,
    commit_base,
    free_port,
    garage,
    git,
    page,
    pushed_iso,
    results,
    runner,
    unsigned,
    write_spec,
    xcraft,
)  # fmt: skip
from xcraft import ccache, container, pinned, remote, remote_pages, repodata, resolution, upstream_copy
from xcraft.__main__ import app
from xcraft.cache import build_keys, build_path
from xcraft.iso import checkout_iso, fetch_isos, iso_key, iso_path
from xcraft.lock import load_lock
from xcraft.project import Project, Remote
from xcraft.rpmbuild import prepare_tree
from xcraft.signing import Signer, create_dev_key, dev_signer, key_signer, release_signer
from xcraft.sources import SourceError, cache_source, sha256_of, source_path, write_manifest

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def root(storage_root: Path) -> Path:
    return storage_root


def should_fetch_pushed_isos_without_preparing_the_checkout(root: Path, tmp_path: Path) -> None:
    project = Project.find()
    signer, image, tree = pushed_iso(project, tmp_path)
    shutil.rmtree(project.state / "cache")
    # No snapshot, RPMs, prepared container state or signing operations are needed to fetch the image.
    project.frozen = True
    assert checkout_iso(project, signer=signer).read_bytes() == image.read_bytes()
    assert tree.exists()
    # a checkout signing with another key, like the developer's, gets it rather than making it, the file of its cache
    # rather than a copy
    assert checkout_iso(project, signer=Signer("0" * 40), published=True).samefile(tree / "image.iso")
    assert not (project.state / "publish").exists()
    assert not (project.state / "cache/builds").exists()


def _source_archive(root: Path, tmp_path: Path) -> tuple[Path, str]:
    """Return a new source archive of trivial, in the source cache and its manifest, and its SHA-256."""
    archive = tmp_path / f"trivial-{secrets.token_hex(4)}.tar.gz"
    archive.write_text(archive.name)
    digest = cache_source(Project.find().source_cache, archive)
    write_manifest(root / "base/trivial/sources", {archive.name: digest})
    return archive, digest


def should_push_the_source_archives_missing_from_the_storage(
    root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    archive, digest = _source_archive(root, tmp_path)
    cache = Project.find().source_cache
    code, output = xcraft("push", "sources", "--check", "--to", "main")
    assert code == 1
    assert f"✗ {archive.name} is not in the remote storage" in output
    with caplog.at_level(logging.DEBUG, logger="xcraft"):  # what xcraft --debug shows
        assert xcraft("push", "sources")[1].endswith("1 pushed, 0 already in the remote storage\n")
    assert (
        f"uploading {cache / digest / archive.name} to s3://xcraft-dev/.xcraft/sources/{digest}/{archive.name}"
        in caplog.messages
    )
    assert xcraft("push", "sources", "--check", "--to", "main")[0] == 1
    shutil.rmtree(cache)
    assert (prepare_tree(Project.find(), "trivial") / "SOURCES" / archive.name).read_text() == archive.name
    assert xcraft("push", "--to", "main")[0] == 1  # sources are uploaded before reporting the missing builds
    assert xcraft("push", "sources", "--check", "--to", "main") == (0, "1 archives in the remote storage, 0 missing\n")


def should_download_the_pushed_source_archives(root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    archive, digest = _source_archive(root, tmp_path)
    assert xcraft("push", "sources", "--to", "main")[0] == 0
    cache = Project.find().source_cache
    # a fresh checkout downloads it
    shutil.rmtree(cache)
    assert (prepare_tree(Project.find(), "trivial") / "SOURCES" / archive.name).read_text() == archive.name
    opened = []
    for fd in Path("/proc/self/fd").iterdir():
        with contextlib.suppress(OSError):  # the one listing them, closed since
            opened.append(str(fd.readlink()))
    assert not [path for path in opened if archive.name in path]  # closed, once downloaded
    shutil.rmtree(cache)
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        assert xcraft("fetch", "sources") == (0, "all the source archives are in the source cache\n")
    assert any(m.startswith(f"downloading {WEB}/.xcraft/sources/{digest}/") for m in caplog.messages)
    assert (cache / digest / archive.name).read_text() == archive.name


def should_check_the_downloaded_source_archives(root: Path, tmp_path: Path) -> None:
    archive, digest = _source_archive(root, tmp_path)
    assert xcraft("push", "sources")[0] == 0
    assert xcraft("push", "sources", "--to", "main")[0] == 0
    cache = Project.find().source_cache
    shutil.rmtree(cache)
    storage = Project.find().config.remote.get("main")
    assert storage
    remote.upload(storage, source_path(archive.name, digest), root / "xcraft.toml")
    # the copy of dev rather than the corrupted one of main
    assert (prepare_tree(Project.find(), "trivial") / "SOURCES" / archive.name).read_text() == archive.name
    shutil.rmtree(cache)
    remote.upload(Project.find().config.remote["dev"], source_path(archive.name, digest), root / "xcraft.toml")
    with pytest.raises(SourceError, match="does not match the sha256 of the manifest"):
        prepare_tree(Project.find(), "trivial")


def should_download_source_archives_from_the_urls_of_the_spec_and_report_those_failing(
    root: Path, tmp_path: Path
) -> None:
    archive, extra = (tmp_path / f"{name}-{secrets.token_hex(4)}.tar.gz" for name in ("trivial", "extra"))
    for path in (archive, extra):
        path.write_text(path.name)
    write_manifest(root / "base/trivial/sources", {path.name: sha256_of(path) for path in (archive, extra)})
    spec = root / "base/trivial/trivial.spec"
    sources = f"Source0: file://{archive}\nSource1: file://{extra}"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", f"BuildArch: noarch\n{sources}"))
    assert xcraft("fetch", "sources", "trivial") == (0, "all the source archives are in the source cache\n")
    for path in (archive, extra):  # Source1's too
        assert (Project.find().source_cache / sha256_of(path) / path.name).read_text() == path.name
    shutil.rmtree(Project.find().source_cache)
    code, output = xcraft("push", "--to", "main")
    assert code == 1
    assert "not in the source cache" in output
    assert xcraft("fetch", "sources", "trivial")[0] == 0
    code, output = xcraft("push", "--to", "main")
    assert code == 1, output
    assert "2 pushed, 0 already in the remote storage" in output
    assert xcraft("push", "sources", "trivial", "--check", "--to", "main") == (
        0,
        "2 archives in the remote storage, 0 missing\n",
    )
    # checked too
    write_manifest(root / "base/trivial/sources", {archive.name: "0" * 64})
    code, output = xcraft("fetch", "sources", "trivial")
    assert code == 1
    assert f"{archive.name} from file://{archive} does not match the sha256 of the manifest" in output
    # a download failing, for the package only
    spec.write_text(
        spec.read_text().replace(f"Source0: file://{archive}", f"Source0: file://{tmp_path}/gone/{archive.name}")
    )
    code, output = xcraft("fetch", "sources")
    assert code == 1
    assert f"✗ trivial: {archive.name} is not in the source cache, and cannot be downloaded: " in output
    # found nowhere, without a directory left in the cache
    spec.write_text(
        spec.read_text().replace(f"Source0: file://{tmp_path}/gone/{archive.name}", "Source0: other.tar.gz")
    )
    code, output = xcraft("fetch")
    assert code == 1
    assert output.endswith(
        f"✗ trivial: {archive.name} found nowhere: not in the remote storage, nor at a URL of the spec\n"
    )
    assert not (Project.find().source_cache / ("0" * 64)).exists()


def should_fail_when_the_storage_reports_files_not_deleted(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = Project.find().config.remote["main"]
    with remote.writing(storage, "delete") as s3:
        # in a response of success, which no storage gives on purpose
        error = {"Key": "file", "Code": "InternalError", "Message": "We encountered an internal error"}
        monkeypatch.setattr(s3, "delete_objects", lambda **_: {"Deleted": [], "Errors": [error]})
    with pytest.raises(remote.RemoteError, match="cannot delete file: We encountered an internal error"):
        remote.delete(storage, ["file"])


def should_trust_only_trees_signed_by_a_build_key(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote.get("main")
    assert storage
    src = tmp_path / "tree"
    src.mkdir()
    (src / "file").write_text("trusted")
    prefix = f"builds/security/{secrets.token_hex(4)}"
    signer = release_signer(Project.find())
    assert signer
    # Verification keys neither select the signer nor need to be configured for uploads.
    remote.upload_tree(storage.model_copy(update={"build_keys": []}), prefix, src, ["file"], signer=signer)
    assert remote.download_tree(storage, prefix, tmp_path / "valid")
    assert (tmp_path / "valid/file").read_text() == "trusted"
    assert remote.tree_exists(storage.model_copy(update={"build_keys": ["0" * 40, *storage.build_keys]}), prefix)
    assert not remote.tree_exists(storage.model_copy(update={"build_keys": []}), prefix)
    key = tmp_path / "key.asc"
    key.write_text(signer.public_key())
    remote.upload(storage, f"{remote.KEYS}/{'0' * 40}.asc", key)
    assert not remote.tree_exists(storage.model_copy(update={"build_keys": ["0" * 40]}), prefix)
    # A signed manifest cannot be transplanted to another build key.
    generation = remote.read_tree(storage, prefix)[0]
    remote.transfer(
        storage,
        {
            f"{prefix}-other/{remote.MANIFEST}": f"{prefix}/{remote.MANIFEST}",
            f"{prefix}-other/{generation.rsplit('/')[-1]}/file": f"{generation}/file",
        },
    )
    assert not remote.download_tree(storage, f"{prefix}-other", tmp_path / "wrong-path")
    # Changed bytes fail even when the manifest is authentic.
    (src / "file").write_text("tampered")
    remote.upload(storage, f"{generation}/file", src / "file")
    with pytest.raises(remote.RemoteError, match="corrupted"):
        remote.download_tree(storage, prefix, tmp_path / "corrupt")
    assert not (tmp_path / "corrupt").exists()
    # The manifest is signed in clear: the path of the files of the tree, then its files, with their sizes.
    signed = urllib.request.urlopen(f"{storage.url}/{prefix}/{remote.MANIFEST}").read().decode()  # noqa: S310
    valid = tmp_path / "valid/file"
    text = f"{generation}\n{sha256_of(valid)} {valid.stat().st_size} file\n"
    assert signed.startswith("-----BEGIN PGP SIGNED MESSAGE-----\nHash: ")
    assert text in signed
    # Only its signed part is read: lines around it do not add files.
    tampered = f"{sha256_of(src / 'file')}  file"
    (src / remote.MANIFEST).write_text(f"{signed}{tampered}\n")
    remote.upload(storage, f"{prefix}/{remote.MANIFEST}", src / remote.MANIFEST)
    assert remote.read_manifest(storage, prefix) == {"file": sha256_of(tmp_path / "valid/file")}
    # Replacing both bytes and manifest still cannot forge the signature, nor remove it.
    for forged in (signed.replace(text, f"{generation}\n{tampered}\n"), f"{generation}\n{tampered}\n"):
        (src / remote.MANIFEST).write_text(forged)
        remote.upload(storage, f"{prefix}/{remote.MANIFEST}", src / remote.MANIFEST)
        assert not remote.tree_exists(storage, prefix)
    # nor is a signed one with a size that is not a number
    (src / "malformed").write_text(f"{generation}\n{sha256_of(valid)} x file\n")
    signer.clearsign(src / "malformed", src / remote.MANIFEST)
    remote.upload(storage, f"{prefix}/{remote.MANIFEST}", src / remote.MANIFEST)
    remote.forget()
    assert not remote.tree_exists(storage, prefix)


def should_update_a_tree_when_some_of_its_files_fail_to_download(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Project.find().config.remote["main"]
    for name in ("good", "failing"):
        (tmp_path / name).write_text(name)
    prefix = f"trees/{secrets.token_hex(4)}"
    remote.upload_tree(storage, prefix, tmp_path, ["good", "failing"], signer=release_signer(Project.find()))
    download = remote.download

    def failing(storage: Remote, path: str, dest: Path, **kwargs: Callable[..., None]) -> bool:
        if path.endswith("/failing"):
            raise remote.RemoteError(path)  # transient, which no storage can do on purpose
        return download(storage, path, dest, **kwargs)

    monkeypatch.setattr(remote, "download", failing)
    assert remote.update_tree(Project.find(), prefix, root / "dest", lambda *_: True)
    assert [p.name for p in (root / "dest").iterdir()] == ["good"]


def should_fetch_trees_from_dev_when_the_main_copy_is_untrusted_or_corrupted(
    root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = Project.find()
    main, dev = project.config.remote["main"], project.config.remote["dev"]
    signer = release_signer(project)
    assert signer
    src = tmp_path / "src"
    src.mkdir()
    (src / "file").write_text("trusted payload")
    prefix = f"builds/security/{secrets.token_hex(4)}"
    manifest = remote.upload_tree(dev, prefix, src, ["file"], signer=signer)
    # Main has an unsigned tree. Only its metadata may be downloaded before trying dev.
    remote.upload(main, f"{prefix}/file", src / "file")
    (src / remote.MANIFEST).write_text(remote.manifest_text(prefix, manifest))
    remote.upload(main, f"{prefix}/{remote.MANIFEST}", src / remote.MANIFEST)
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        assert remote.tree_available(project, prefix)
        assert remote.fetch_tree(project, prefix, tmp_path / "valid")
    assert (tmp_path / "valid/file").read_text() == "trusted payload"
    assert not any(f"downloading {main.url}/{prefix}/file " in line for line in caplog.messages)
    generation = remote.generation(prefix, manifest)
    assert any(f"downloading {dev.url}/{generation}/file " in line for line in caplog.messages)
    # The bucket does not grant trust. An unknown signer must not trigger payload downloads in either bucket.
    project.config.remote["main"] = main.model_copy(update={"build_keys": ["0" * 40]})
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        assert not remote.tree_available(project, prefix)
        assert not remote.fetch_tree(project, prefix, tmp_path / "untrusted")
    assert not any(f"/{prefix}/file " in line for line in caplog.messages)
    assert not (tmp_path / "untrusted").exists()
    # A signed but corrupt main payload can fall back to a valid dev copy too.
    project.config.remote["main"] = main
    remote.upload_tree(main, prefix, src, ["file"], signer=signer)
    (src / "file").write_text("corrupted main payload")
    remote.upload(main, f"{prefix}/file", src / "file")
    assert remote.fetch_tree(project, prefix, tmp_path / "fallback")
    assert (tmp_path / "fallback/file").read_text() == "trusted payload"


def should_push_to_main_only_the_developer_source_archives_of_their_manifest(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote.get("main")
    assert storage
    dev_storage = Project.find().config.remote["dev"]
    archive = tmp_path / f"source-{secrets.token_hex(4)}"
    archive.write_text("source archive")
    digest = sha256_of(archive)
    path = source_path(archive.name, digest)
    write_manifest(root / "base/trivial/sources", {archive.name: digest})
    remote.upload(dev_storage, path, archive)  # by a developer
    code, output = xcraft("push", "--to", "main")
    assert code == 1
    assert "not in the source cache" in output
    assert xcraft("fetch", "sources")[0] == 0
    code, output = xcraft("push", "--to", "main")
    assert code == 1, output
    assert f"↑ {archive.name}" in output
    assert remote.download(storage, path, tmp_path / "uploaded")
    assert sha256_of(tmp_path / "uploaded") == digest
    archive.write_text("corrupted developer archive")
    remote.upload(dev_storage, path, archive)
    shutil.rmtree(Project.find().source_cache)
    assert xcraft("push", "--to", "main")[0] == 1  # a changed developer copy cannot replace an existing main copy
    assert remote.download(storage, path, tmp_path / "still-trusted")
    assert sha256_of(tmp_path / "still-trusted") == digest
    remote.delete(storage, [path])
    code, output = xcraft("push", "--to", "main")
    assert code == 1
    assert "not in the source cache" in output
    code, output = xcraft("fetch", "sources")
    assert code == 1
    assert "does not match the sha256 of the manifest" in output
    assert not remote.exists(storage, path)


def _dev_only(root: Path) -> tuple[str, Signer]:
    """Configure the project with the dev bucket only, and return that configuration, and the developer's key made."""
    config = re.sub(r"\[remote.main\]\n.*?(?=\[remote.dev\])", "", unsigned(REMOTE), flags=re.DOTALL)
    (root / "xcraft.toml").write_text(config)
    return config, create_dev_key(Project.find(), "Developer")


def should_push_and_fetch_sources_with_the_dev_bucket_only(root: Path, tmp_path: Path) -> None:
    _dev_only(root)
    archive = tmp_path / f"source-{secrets.token_hex(4)}"
    archive.write_text("developer source")
    digest = cache_source(Project.find().source_cache, archive)
    write_manifest(root / "base/trivial/sources", {archive.name: digest})
    assert xcraft("push", "sources")[0] == 0
    shutil.rmtree(Project.find().source_cache)
    assert xcraft("fetch")[0] == 0  # a developer-only configuration can fetch sources without a main bucket
    assert (Project.find().source_cache / digest / archive.name).read_text() == archive.read_text()


def should_push_and_fetch_builds_with_the_dev_bucket_only(root: Path) -> None:
    _, signer = _dev_only(root)
    assert xcraft("build", "trivial", "--fetch=none")[0] == 0
    assert xcraft("push", "builds", "trivial")[0] == 0
    storage = Project.find().config.remote["dev"]
    trees = remote.listing(storage, f".xcraft/builds/{signer.key}/{ARCH}/trivial/")
    assert any(path.endswith(remote.MANIFEST) for path in trees)
    assert not any(path.endswith(".asc") for path in trees if not path.endswith(remote.MANIFEST))
    assert signer.key not in storage.build_keys
    shutil.rmtree(root / ".xcraft/cache/builds")
    code, output = xcraft("fetch", "builds", "trivial")
    assert code == 0, output
    assert "1 fetched" in output


def should_fetch_compiler_caches_from_dev_when_main_is_unreachable(root: Path) -> None:
    config, _ = _dev_only(root)
    (root / "xcraft.toml").write_text(config + '\n[remote.main]\nurl = "http://localhost:1"\n')
    cache = root / ".xcraft/ccache/trivial"
    (cache / "0").mkdir(parents=True, exist_ok=True)  # like those of ccache
    (cache / "0/entry").write_text("cached object")
    assert xcraft("push", "ccache", "trivial")[0] == 0
    shutil.rmtree(cache)
    code, output = xcraft("fetch", "ccache", "trivial")
    assert code == 0, output
    assert (cache / "0/entry").read_text() == "cached object"


def should_refuse_to_push_without_the_dev_bucket(root: Path) -> None:
    (root / "xcraft.toml").write_text(
        re.sub(r"\[remote.dev\]\n.*?(?=\[publish\])", "", _dev_only(root)[0], flags=re.DOTALL)
    )
    code, output = xcraft("push", "sources")
    assert code == 2
    assert "remote.dev" in output


def should_keep_embargoed_builds_in_the_private_storage(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_signer: tuple[str, Path]
) -> None:
    garage("bucket", "create", bucket := f"xcraft-private-{secrets.token_hex(4)}")
    garage("bucket", "allow", "--read", "--write", bucket, "--key", "test")
    branches = 'maintenance_branches = ["maint/*", "embargo/*"]\nembargo_branches = ["embargo/*"]\n'
    private = f'[remote.private]\ns3_endpoint = "http://localhost:{S3_PORT}"\nbucket = "{bucket}"\n'
    config = unsigned(REMOTE)
    (root / "xcraft.toml").write_text(f'{branches}{config}{private}build_keys = ["{build_signer[0]}"]\n')
    (root / ".gitignore").write_text(".xcraft/\n")
    commit_base(root)
    monkeypatch.setenv("GNUPGHOME", str(build_signer[1]))
    # not used off the embargo branches
    assert [s.bucket for s in remote.storages(Project.find())] == ["xcraft", "xcraft-dev"]
    git(root, "checkout", "-qb", "embargo/xsa-123")
    remote.forget()  # for a new command
    assert [s.bucket for s in remote.storages(Project.find())] == ["xcraft", "xcraft-dev", bucket]
    assert xcraft("build", "trivial")[0] == 0
    # nothing goes to the public buckets
    code, output = xcraft("push", "builds", "trivial", "--key", build_signer[0], "--to", "main")
    assert code == 2
    # in the box of the error, wrapped
    assert "on an embargo branch, nothing goes to the main remote storage" in " ".join(
        re.sub("[│╭╮╰╯─]", " ", output).split()
    )
    code, output = xcraft("push", "builds", "trivial", "--key", build_signer[0], "--to", "private")
    assert code == 0, output
    # fetched by another checkout of the branch, through S3
    other = tmp_path / "elsewhere" / "xcraft"
    shutil.copytree(root, other, ignore=shutil.ignore_patterns(".xcraft"))
    monkeypatch.setenv("XCRAFT_PROJECT", str(other))
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert "↓ trivial fetched" in output.splitlines()
    # released: copied to the main bucket, server-side, as it is
    project = Project.find()
    path = build_path(project, "trivial", build_keys(project)["trivial"], build_signer[0])
    assert not remote.tree_exists(project.config.remote["main"], path)
    assert xcraft("push", "--from", "private", "--to", "main", "builds")[0] == 2
    code, output = xcraft("push", "--from", "private", "--to", "main")
    assert code == 0, output
    assert output.splitlines()[-1] == "0 source archives and 1 builds copied"
    assert remote.tree_exists(project.config.remote["main"], path)
    assert (
        xcraft("push", "--from", "private", "--to", "main")[1].splitlines()[-1]
        == "0 source archives and 0 builds copied"
    )


def should_share_builds_between_checkouts_with_their_rpm_dependencies(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_signer: tuple[str, Path]
) -> None:
    """The builds are shared by the checkouts, whatever their paths, with what rpm generated for them, which a checkout
    fetching them all does not resolve again.
    """
    # CI selects its key without a private-key setting in the project.
    (root / "xcraft.toml").write_text(unsigned(REMOTE))
    monkeypatch.setenv("GNUPGHOME", str(build_signer[1]))
    assert xcraft("build")[0] == 0
    for what in ("builds", "resolutions"):  # those of the build environment, rather than resolving them again
        assert xcraft("push", "--key", build_signer[0], "--to", "main", what)[0] == 0
    recorded = {p.parts[-3]: p.read_text() for p in root.glob(".xcraft/cache/builds/*/*/*/*/rpm-deps.json")}
    assert set(recorded) == {"trivial", "with-deps"}
    # another checkout, elsewhere, with what is committed: the same keys, whatever the path
    other = tmp_path / "elsewhere" / "xcraft"
    shutil.copytree(root, other, ignore=shutil.ignore_patterns(".xcraft"))
    monkeypatch.setenv("XCRAFT_PROJECT", str(other))
    assert Project.find().id != Project(root, Project.find().config).id
    # resolving here, not reading what the other tests resolved, see conftest.py
    monkeypatch.setattr(resolution, "_run", getattr(resolution._run, "__wrapped__", resolution._run))
    code, output = xcraft("build")
    assert code == 0, output
    assert "made the developer's key" in output  # of this checkout, for its builds
    assert output.splitlines()[-1] == "2 fetched"
    assert not (other / f".xcraft/logs/_resolve-{ARCH}.log").exists()
    assert {p.parts[-3]: p.read_text() for p in other.glob(".xcraft/cache/builds/*/*/*/*/rpm-deps.json")} == recorded


def should_push_what_it_can_and_report_the_rest(root: Path, tmp_path: Path) -> None:
    # an archive of its own
    archive = tmp_path / f"trivial-{secrets.token_hex(4)}.tar.gz"
    archive.write_text(archive.name)
    write_manifest(root / "base/trivial/sources", {archive.name: cache_source(Project.find().source_cache, archive)})
    # and one of a package not built, which the source cache has not, like those of the builds that failed, with the
    # same name as the one of trivial, which is pushed still
    write_manifest(root / "base/with-deps/sources", {archive.name: "0" * 64})
    assert xcraft("build", "trivial")[0] == 0
    code, output = xcraft("push", "--to", "main")
    # all that can be pushed, and then what could not
    assert code == 1
    assert f"↑ {archive.name}\n1 pushed, 0 already in the remote storage\n↑ trivial\n1 pushed, 0 already" in output
    # the pages of what was pushed only: xcraft push index makes them all
    assert output.endswith(
        "already in the remote storage\n"  # the resolutions, which the other tests may have pushed
        f"not in the source cache: {archive.name}; not built: with-deps"
    )
    assert re.search(r"^resolutions: \d+ pushed, \d+ already in the remote storage$", output, re.MULTILINE)
    assert f'href="{archive.name}"' in page(f".xcraft/sources/{sha256_of(archive)}/")
    assert xcraft("push", "sources", "trivial", "--check", "--to", "main")[0] == 0


def should_not_reuse_resolutions_of_other_package_contents(root: Path, tmp_path: Path) -> None:
    """The resolutions of the storage are of the pins they were made with: neither an update pinning other contents
    of the same versions, nor another machine with the lock it made, get those of the former contents.
    """
    # two RPMs of the same name, version and architecture, the second requiring a package whose source is locked
    (specs := tmp_path / "specs").mkdir()
    (specs / "toypayload.spec").write_text(
        "Name: toypayload\nVersion: 1\nRelease: 1\nSummary: test\nLicense: MIT\nBuildArch: noarch\n"
        "%if %{needs}\nRequires: toyrun\n%endif\n%description\ntest\n"
        "%install\nmkdir -p %{buildroot}/usr/share\necho %{payload} > %{buildroot}/usr/share/toypayload\n"
        "%files\n/usr/share/toypayload\n"
    )
    (rpms := tmp_path / "rpms").mkdir()
    container.run(
        Project.find(), "payloads",
        " && ".join(
            f"rpmbuild -bb --define '_topdir /tmp/{payload}' --define '_rpmdir /out/{payload}'"
            f" --define 'payload {payload}' --define 'needs {needs}' /specs/toypayload.spec"
            f" && createrepo_c -q /out/{payload}"
            for payload, needs in (("a", 0), ("b", 1))
        ),
        volumes=[f"{specs}:/specs:ro", f"{rpms}:/out"],
    )  # fmt: skip
    shutil.copytree(rpms / "a", repo := tmp_path / "repo")
    config = REMOTE.replace('"bash", "bzip2"', '"toypayload", "bash", "bzip2"')
    config = config.replace("[publish]\n", '[publish]\nextra = ["toyrun"]\n')
    (root / "xcraft.toml").write_text(f'{config}\n[repos.payload]\nbaseurl = "file://{repo}/"\ngpgcheck = false\n')
    assert xcraft("build", "--dry-run")[0] == 0
    sources = load_lock(Project.find()).sources
    assert xcraft("push", "resolutions")[0] == 0
    # published again: the update pins it again, from the repositories rather than the storage
    shutil.rmtree(repo)
    shutil.copytree(rpms / "b", repo)
    code, output = xcraft("lock", "--update", "toypayload")
    assert code == 0, output
    assert "pinned again, of the same versions: toypayload" in output
    project = Project.find()
    lock = load_lock(project)
    assert lock.sources == sources
    assert lock.binaries[ARCH]["toypayload"].sha256 == sha256_of(next((rpms / "b").rglob("*.rpm")))
    assert "toyrun" in lock.image[ARCH]
    # another machine, with the lock updated, and the resolutions of the former contents in the storage
    locked = (root / "xcraft.lock").read_bytes()
    shutil.rmtree(project.state / "cache/resolved")
    code, output = xcraft("build", "--dry-run", "--locked")
    assert code == 0, output
    assert (root / "xcraft.lock").read_bytes() == locked


def should_fetch_resolutions_rather_than_resolving_again(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # resolving here, not reading what the other tests resolved, see conftest.py
    monkeypatch.setattr(resolution, "_run", getattr(resolution._run, "__wrapped__", resolution._run))
    # build roots of their own, not those of the other tests, which share the storage, which the version of trivial
    # changes: with-deps requires one
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text().replace("BuildArch: noarch", f"BuildArch: noarch\nProvides: t-{secrets.token_hex(4)}")
    )
    deps = root / "base/with-deps/with-deps.spec"
    deps.write_text(deps.read_text().replace("BuildRequires: trivial", "BuildRequires: trivial >= 1.0"))

    def pushed() -> str:
        """Push the resolutions, like CI."""
        code, output = xcraft("push", "resolutions", "--to", "main")
        assert code == 0, output
        return output

    assert re.search(r"^resolutions: [1-9]\d* pushed", pushed(), re.MULTILINE)
    assert re.search(r"^resolutions: 0 pushed", pushed(), re.MULTILINE)
    resolved, log = root / ".xcraft/cache/resolved", root / f".xcraft/logs/_resolve-{ARCH}.log"
    before = {c: c.read_bytes() for c in resolved.rglob("*.json")}
    # another release of trivial: the same key, resolved again, which replaces those of the storage
    spec.write_text(spec.read_text().replace("Release: 1%{?dist}", "Release: 2%{?dist}"))
    assert re.search(r"^resolutions: [1-9]\d* pushed", pushed(), re.MULTILINE)
    caches = sorted(c for c in resolution.resolutions_read() if c.is_relative_to(resolved))
    # one resolved again for that release: of this test only, with its trivial
    changed = next(c for c in caches if c in before and c.read_bytes() != before[c])

    def new_checkout() -> str:
        """Run xcraft status like in a new checkout, without the resolutions."""
        shutil.rmtree(resolved)
        log.unlink(missing_ok=True)
        code, output = xcraft("status")
        assert code == 0, output
        return output

    # fetched, signed, rather than resolved, by a new checkout
    new_checkout()
    assert not log.exists()
    assert sorted(resolved.rglob("*.json")) == caches
    # resolved, when what the storage has is not what was signed
    project = Project.find()
    (tampered := root / "tampered.json").write_text("{}")
    storage, tree = remote.storages(project)[0], resolution.remote_tree(changed)
    remote.upload(storage, f"{remote.read_tree(storage, tree)[0]}/{changed.name}", tampered)
    assert "no resolutions from the remote storage" in new_checkout()
    assert log.exists()
    assert sorted(resolved.rglob("*.json")) == caches


def should_list_the_pushed_builds_in_browsable_pages(root: Path) -> None:
    assert xcraft("build", "trivial")[0] == 0
    assert xcraft("push", "builds", "trivial", "--to", "main")[0] == 0
    # browsable, as the storage does not list directories
    assert 'href=".xcraft/"' in page("")
    assert 'href="builds/"' in page(".xcraft/")
    signer = key_signer(Project.find())
    assert signer
    assert 'href="trivial/"' in page(f".xcraft/builds/{signer.key}/{ARCH}/")
    # with the size of all that is below them and the date of the newest file, from their pages when only their
    # parent is listed
    assert re.search(
        r'href="builds/">builds/</a></td><td data-k="[1-9]\d*"[^>]*>[\d.]+ \w+</td><td data-k="\d{4}-', page(".xcraft/")
    )
    assert xcraft("push", "index", "--to", "main")[1].endswith(" pages\n")


def should_fetch_pushed_builds_rather_than_building_them(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert xcraft("build", "trivial")[0] == 0
    assert (
        xcraft("push", "builds", "trivial", "--to", "main")[1]
        == "↑ trivial\n1 pushed, 0 already in the remote storage\n"
    )
    assert xcraft("push", "builds", "--to", "main") == (
        1,
        "0 pushed, 1 already in the remote storage\nnot built: with-deps",
    )

    # a fresh checkout downloads it, with xcraft fetch, or instead of building it
    shutil.rmtree(root / ".xcraft/cache/builds")
    assert xcraft("fetch", "builds") == (0, "↓ trivial\n1 fetched, 0 already cached, 1 not in the remote storage\n")
    shutil.rmtree(root / ".xcraft/cache/builds")
    # shown so, unless --local
    for args, state in (((), "fetchable"), (("--local",), "missing")):
        code, output = xcraft("status", *args)
        assert code == 0, output
        assert {line.split()[0]: line.split()[2] for line in output.splitlines()[:-1]} == {
            "trivial": state,
            "with-deps": "missing",
        }
    assert xcraft("build", "with-deps", "--dry-run")[1].splitlines()[:2] == ["fetch    trivial", "build    with-deps"]
    assert xcraft("build", "with-deps", "--dry-run", "--fetch=none")[1].splitlines()[:2] == [
        "build    trivial",
        "build    with-deps",
    ]
    monkeypatch.setenv("CI", "true")  # the steps, in the output
    code, output = xcraft("build", "with-deps")
    monkeypatch.delenv("CI")
    assert code == 0, output
    assert results(output)[0] == "↓ trivial fetched"
    assert results(output)[-1] == "1 built, 1 fetched"
    assert "✓ with-deps built" in output  # but trivial, fetched rather than built
    (fetched,) = root.glob(f".xcraft/cache/builds/*/{ARCH}/trivial")
    assert list(fetched.glob("*/RPMS/noarch/trivial-1.0-1+b1.xcpng8.99.noarch.rpm"))
    # built, even though the storage has it
    shutil.rmtree(fetched)
    code, output = xcraft("build", "trivial", "--fetch=none")
    assert code == 0, output
    assert output.splitlines()[-1] == "1 built"


def should_fetch_only_the_builds_others_are_built_against(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # trivial and other in the storage, with-deps, built against trivial, not yet
    lock = (root / "xcraft.lock").read_text()
    write_spec(root, "other")
    assert xcraft("build", "trivial", "other")[0] == 0
    assert xcraft("push", "builds", "trivial", "other", "--to", "main")[0] == 0
    shutil.rmtree(root / ".xcraft/cache/builds")
    # without what rpm generated for trivial in the lock: fetched for with-deps, it changes the key of with-deps
    (root / "xcraft.lock").write_text(lock)
    signer = key_signer(Project.find())  # of [sign], which built and pushed them
    assert signer
    builds = root / ".xcraft/cache/builds" / signer.key / ARCH
    # only trivial is fetched, for the build of with-deps
    code, output = xcraft("build", "--dry-run", "--fetch=needed")
    assert code == 0, output
    assert output.splitlines() == [
        "fetch    trivial",
        "build    with-deps",
        "1 to build, 1 to fetch, 1 in the remote storage",
    ]
    monkeypatch.setenv("CI", "true")  # the steps, in the output
    code, output = xcraft("build", "--fetch=needed")
    monkeypatch.delenv("CI")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == [
        "↓ trivial fetched",
        "✓ with-deps built",
        "1 built, 1 fetched, 1 stored",
    ]
    assert sorted(p.name for p in builds.iterdir()) == ["trivial", "with-deps"]
    # the build in the storage only is there already
    assert xcraft("push", "builds", "--to", "main") == (0, "↑ with-deps\n1 pushed, 2 already in the remote storage\n")
    # nothing to fetch, with all in the storage
    shutil.rmtree(root / ".xcraft/cache/builds")
    assert xcraft("build", "--fetch=needed") == (0, "3 stored\n")
    assert not builds.exists() or not list(builds.iterdir())


def ccache_hits(cache: Path) -> int:
    """Return how many compilations were found in the compiler cache `cache`."""
    container.run(
        Project.find(),
        "test-ccache",
        f"{ccache.INSTALL} && CCACHE_DIR=/cache ccache --print-stats",
        volumes=[f"{cache}:/cache"],
        install=True,
    )
    log = (Project.find().state / "logs/_test-ccache.log").read_text()
    stats = {k: int(v) for k, v in (line.split("\t") for line in log.splitlines() if "\t" in line)}
    return stats["direct_cache_hit"] + stats["preprocessed_cache_hit"]


def _ci(root: Path, monkeypatch: pytest.MonkeyPatch, build_signer: tuple[str, Path]) -> list[str]:
    """Configure the project like CI, with the key of the builds of the storage, and return the options to push."""
    (root / "xcraft.toml").write_text(unsigned(REMOTE))
    monkeypatch.setenv("GNUPGHOME", str(build_signer[1]))
    return ["--key", build_signer[0]]


def _developer(root: Path, *_: object) -> list[str]:
    """Configure the project like a developer's, trusting no build key but theirs, and return the options to push."""
    (root / "xcraft.toml").write_text(re.sub(r"build_keys = \[.*?\]", "build_keys = []", unsigned(REMOTE)))
    create_dev_key(Project.find(), "Developer <dev@example.com>")
    return []


@pytest.mark.parametrize("signing", [_ci, _developer])
def should_share_compiler_caches_through_the_storage(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_signer: tuple[str, Path],
    caplog: pytest.LogCaptureFixture,
    signing: Callable[[Path, pytest.MonkeyPatch, tuple[str, Path]], list[str]],
) -> None:
    options = signing(root, monkeypatch, build_signer)
    write_spec(root, "compiled", files="/usr/lib/compiled/t.o")
    spec = root / "base/compiled/compiled.spec"
    build = "%build\necho 'int f(void) { return 0; }' > t.c\ngcc -c t.c -o t.o\n"
    spec.write_text(
        spec.read_text()
        .replace("BuildArch: noarch\n", "BuildRequires: gcc\n")
        .replace("%install\n", build + "%install\n")
        .replace("touch %{buildroot}/usr/lib/compiled/t.o", "cp t.o %{buildroot}/usr/lib/compiled/t.o")
    )
    assert xcraft("build", "compiled")[0] == 0
    cache = root / ".xcraft/ccache/compiled"
    results = sorted(cache.rglob("*R"))  # the results of the compilations
    assert results

    def levels() -> set[int]:
        return {path.read_bytes()[5] for path in cache.rglob("*R")}  # in the header of ccache's entries

    assert levels() == {1}  # ccache's, for the builds

    def push() -> list[str]:
        """Push the cache, and return the entries uploaded."""
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="xcraft"):
            assert xcraft("push", "ccache", "compiled", "--to", "main", *options) == (0, "↑ compiled\n1 pushed\n")
        return sorted(m.split(f"/{tree}/")[1] for m in caplog.messages if m.startswith("uploading ") and tree in m)

    tree = f".xcraft/ccache/{ARCH}/compiled"
    storage = Project.find().config.remote["main"]
    entries = [p.relative_to(cache).as_posix() for p in results]
    dates = {p: p.stat().st_mtime for p in cache.rglob("*") if p.is_file()}
    assert set(entries) <= set(push())
    # compressed again in place, once, with the dates which tell the entries of the last build
    assert levels() == {ccache.LEVEL}
    assert {p: p.stat().st_mtime for p in dates} == dates
    # an object by entry, in a signed tree, with a page, but not its directories, too many
    stored = remote.read_manifest(storage, tree, trusted=dev_signer(Project.find()))
    assert set(entries) <= stored.keys()
    assert sorted(remote.listing(storage, f"{tree}/")) == sorted([*stored, remote.MANIFEST])
    assert 'href="manifest.asc"' in page(f"{tree}/")
    assert 'href="compiled/"' in page(f".xcraft/ccache/{ARCH}/")
    with pytest.raises(urllib.error.HTTPError):
        page(f"{tree}/{entries[0].split('/')[0]}/")
    # only what the storage does not have is uploaded, and compressed: nothing
    files = {p: p.stat().st_ino for p in cache.rglob("*") if p.is_file()}
    assert push() == []
    assert {p: p.stat().st_ino for p in files} == files
    shutil.rmtree(cache)
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert sorted(cache.rglob("*R")) == results
    assert levels() == {ccache.LEVEL}  # compressed again, for smaller transfers
    # a fetch adds what the local cache misses to it
    (results[0]).unlink()
    (cache / "local").write_text("kept")
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert sorted(cache.rglob("*R")) == results
    assert (cache / "local").read_text() == "kept"
    (cache / "local").unlink()
    # a build without a compiler cache gets the one of the remote storage, and uses it, unless --fetch=none
    for release, options, hits in (("2", ["--fetch=none"], 0), ("3", [], 1)):
        shutil.rmtree(cache)
        spec.write_text(re.sub(r"Release: \d+", f"Release: {release}", spec.read_text()))
        assert xcraft("build", "compiled", *options)[0] == 0
        assert ccache_hits(cache) == hits


def should_refuse_to_push_compiler_caches_without_a_signing_key(root: Path) -> None:
    (root / "xcraft.toml").write_text(unsigned(REMOTE))
    cache = root / ".xcraft/ccache/trivial/0"
    cache.mkdir(parents=True)
    (cache / "entry").write_text("cached")
    # refused before anything is uploaded, which no manifest would list
    code, output = xcraft("push", "ccache", "trivial", "--to", "main")
    assert code == 1
    assert "no key to sign uploads" in output
    storage = Project.find().config.remote["main"]
    assert not remote.listing(storage, f".xcraft/ccache/{ARCH}/trivial/")


def should_push_only_the_compiler_cache_entries_of_the_last_build(root: Path, tmp_path: Path) -> None:
    write_spec(root, "compiled", files="/usr/lib/compiled/t.o")
    spec = root / "base/compiled/compiled.spec"
    template = (
        spec.read_text()
        .replace("BuildArch: noarch\n", "BuildRequires: gcc\n")
        .replace("touch %{buildroot}/usr/lib/compiled/t.o", "cp t.o %{buildroot}/usr/lib/compiled/t.o")
    )
    cache = root / ".xcraft/ccache/compiled"

    def build(release: str, value: str) -> None:
        compile_ = f"%build\necho 'int f(void) {{ return {value}; }}' > t.c\ngcc -c t.c -o t.o\n"
        spec.write_text(
            template.replace("Release: 1", f"Release: {release}").replace("%install\n", compile_ + "%install\n")
        )
        assert xcraft("build", "compiled", "--fetch=none")[0] == 0

    build("1", "1")
    build("2", "2")
    first, second = sorted(cache.rglob("*R"), key=lambda path: path.stat().st_mtime)
    # only a hit on the second: its entries only are pushed, with their last use
    build("3", "2")
    assert ccache_hits(cache) == 1
    assert xcraft("push", "ccache", "compiled", "--to", "main") == (0, "↑ compiled\n1 pushed\n")
    shutil.rmtree(cache)
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert sorted(cache.rglob("*R")) == [second]  # not first, of the first build
    assert first != second
    # the next push deletes what the last build did not use, and what a push interrupted before its manifest left
    storage = Project.find().config.remote["main"]
    tree = f".xcraft/ccache/{ARCH}/compiled"
    remote.upload(storage, f"{tree}/orphan", spec)
    build("4", "4")
    # nor what a fetch after the last build gets
    second.unlink()
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert second.exists()
    assert xcraft("push", "ccache", "compiled", "--to", "main") == (0, "↑ compiled\n1 pushed\n")
    stored = remote.read_manifest(storage, tree, trusted=dev_signer(Project.find()))
    assert second.relative_to(cache).as_posix() not in stored
    assert sorted(remote.listing(storage, f"{tree}/")) == sorted([*stored, remote.MANIFEST])
    # the archive of a cache of an older xcraft is not an entry
    (tmp_path / "compiled.ccache").write_text("archive")
    remote.upload_tree(storage, tree, tmp_path, ["compiled.ccache"], signer=release_signer(Project.find()))
    shutil.rmtree(cache)
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert not (cache / "compiled.ccache").exists()


class _Keyring(KeyringBackend):
    """A keyring in memory, rather than the developer's."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        if not self.passwords.pop((service, username), None):
            raise PasswordDeleteError(username)


def should_store_checked_credentials_in_the_keyring(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keyring.core, "_keyring_backend", stored := _Keyring())
    given = remote.Credentials(os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"])
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    config = Project.find().config.remote
    archive = tmp_path / f"source-{secrets.token_hex(4)}"
    archive.write_text("source archive")
    path = source_path(archive.name, sha256_of(archive))
    with pytest.raises(remote.RemoteError, match="xcraft login"):
        remote.upload(config["dev"], path, archive)
    assert "no S3 credentials of http://localhost" in xcraft("doctor")[1]
    # checked before being stored
    result = runner.invoke(app, ["login"], input=f"{given.key}\nwrong\n")
    assert result.exit_code == 1
    assert not stored.passwords
    result = runner.invoke(app, ["login"], input=f"{given.key}\n{given.secret}\n")
    assert result.exit_code == 0, result.output
    assert f"credentials of {config['dev'].s3_endpoint} in the keyring" in result.output
    # those of the server, for its buckets: the dev one, and the main one, with the rights of the key
    remote.upload(config["dev"], path, archive)
    remote.upload(config["main"], path, archive)
    assert "S3 credentials of http://localhost:" in xcraft("doctor")[1]
    assert ", from the keyring" in xcraft("doctor")[1]
    assert xcraft("logout")[1].endswith(" removed\n")
    assert "no credentials of" in xcraft("logout")[1]
    assert not stored.passwords


def should_not_ask_developers_for_the_credentials_of_ci_storage(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring.core, "_keyring_backend", _Keyring())
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    # the main bucket, of build_keys, on a server of its own: CI's only, which the developers do not push to
    toml, main = root / "xcraft.toml", f's3_endpoint = "http://localhost:{S3_PORT}"\nbucket = "xcraft"\n'
    assert main in toml.read_text()
    toml.write_text(toml.read_text().replace(main, main.replace("localhost", "127.0.0.1")))
    output = xcraft("doctor")[1]
    assert f"no S3 credentials of http://localhost:{S3_PORT}" in output  # the dev bucket's
    assert "127.0.0.1" not in output


def should_build_locally_when_the_storage_is_unavailable(root: Path) -> None:
    port = free_port()  # of no server
    (root / "xcraft.toml").write_text(REMOTE.replace(str(WEB_PORT), str(port)))
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert f"remote storage unavailable, building locally: http://xcraft-dev.web.garage.localhost:{port}/" in output
    assert output.splitlines()[-1] == "1 built"


def should_refuse_to_push_without_credentials(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    # not the build the other tests pushed: the same key everywhere
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "no credentials"'))
    assert xcraft("build", "trivial")[0] == 0
    code, output = xcraft("push", "builds", "trivial", "--to", "main")
    assert code == 1
    assert "pushing needs the S3 credentials" in output


def should_not_push_builds_made_without_their_tests(root: Path) -> None:
    # not the build the other tests pushed: the same key everywhere
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "without its tests"'))
    assert xcraft("build", "trivial")[0] == 0
    # like the final builds an earlier xcraft made with --nocheck: the final builds run the tests now
    (build,) = root.glob(f".xcraft/cache/builds/*/{ARCH}/trivial/*")
    (build / "nocheck").touch()
    code, output = xcraft("push", "builds", "trivial", "--to", "main")
    assert code == 1
    assert "built without their tests, with --nocheck, not pushed: trivial\n" in output
    assert output.endswith("not built: trivial")


def should_keep_trees_complete_when_uploaded_concurrently(
    s3: dict[str, str], build_signer: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uploads of other files as the same tree, like the build of a key made on two machines at once, leave it
    complete, as its last manifest says, and a reader of the other one gets its files.
    """
    for name, value in s3.items():
        monkeypatch.setenv(name, value)
    signer = Signer(*build_signer)
    storage = Remote(
        url=f"http://xcraft-dev.web.garage.localhost:{WEB_PORT}",
        s3_endpoint=f"http://localhost:{S3_PORT}",
        bucket="xcraft-dev",
        build_keys=[signer.key],
    )
    prefix = f".xcraft/builds/race/{secrets.token_hex(4)}"
    for host in ("a", "b", "c"):
        (tmp_path / host).mkdir()
        (tmp_path / host / "build.log").write_text(f"built on {host}\n")

    def read(dest: str) -> str:
        remote.forget()  # as another command
        assert remote.download_tree(storage, prefix, tmp_path / dest)
        return (tmp_path / dest / "build.log").read_text()

    write = remote.write_manifest

    # a, its files uploaded, waits for b to complete the tree
    def raced(
        target: Remote,
        path: str,
        manifest: dict[str, str],
        *,
        signer: Signer | None,
        root: str | None = None,
        sizes: dict[str, int] | None = None,
    ) -> None:
        monkeypatch.setattr(remote, "write_manifest", write)
        remote.upload_tree(storage, prefix, tmp_path / "b", ["build.log"], signer=signer)
        assert read("read-b") == "built on b\n"
        write(target, path, manifest, signer=signer, root=root, sizes=sizes)

    monkeypatch.setattr(remote, "write_manifest", raced)
    remote.upload_tree(storage, prefix, tmp_path / "a", ["build.log"], signer=signer)
    assert read("read-a") == "built on a\n"

    # one interrupted before its manifest leaves the tree as it was
    def interrupted(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(remote, "write_manifest", interrupted)
    with pytest.raises(KeyboardInterrupt):
        remote.upload_tree(storage, prefix, tmp_path / "c", ["build.log"], signer=signer)
    monkeypatch.setattr(remote, "write_manifest", write)
    assert read("read-after") == "built on a\n"

    # the trees of older versions, with their files in them, and not a generation of other files
    remote.upload(storage, f"{prefix}/build.log", tmp_path / "c" / "build.log")
    digest = sha256_of(tmp_path / "c" / "build.log")
    remote.write_manifest(storage, prefix, {"build.log": digest}, signer=signer)
    assert read("read-old") == "built on c\n"
    remote.write_manifest(storage, prefix, {"build.log": digest}, signer=signer, root=f"{prefix}/{'0' * 64}")
    remote.forget()
    assert not remote.tree_exists(storage, prefix)


@pytest.mark.parametrize("href", ["{outside}", "repodata/../../outside", "../outside", "repodata/sub/../../../x"])
def should_reject_upstream_metadata_pointing_out_of_its_directory(
    href: str, s3: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unsigned metadata of the copy cannot write out of its work directory, even when it cannot be read."""
    for name, value in s3.items():
        monkeypatch.setenv(name, value)
    storage = Remote(
        url=f"http://xcraft-dev.web.garage.localhost:{WEB_PORT}",
        s3_endpoint=f"http://localhost:{S3_PORT}",
        bucket="xcraft-dev",
    )
    (outside := tmp_path / "outside").write_text("kept")
    directory = f"evil-{secrets.token_hex(4)}"
    (repomd := tmp_path / "repomd.xml").write_text(
        '<repomd xmlns="http://linux.duke.edu/metadata/repo">'
        f'<data type="primary"><location href="{href.format(outside=outside)}"/></data></repomd>'
    )
    prefix = f"{upstream_copy.PREFIX}/{directory}"
    remote.upload(storage, f"{prefix}/repodata/repomd.xml", repomd)
    remote.upload(storage, f"{prefix}/{outside.name}", repomd)
    try:
        with pytest.raises(upstream_copy.UpstreamError, match="invalid metadata location"):
            upstream_copy.read(storage, directory, tmp_path / "work" / "deep")
        assert outside.read_text() == "kept"
        assert [p.name for p in (tmp_path / "work").rglob("*") if p.is_file()] == ["repomd.xml"]
    finally:
        remote.delete(storage, [f"{prefix}/repodata/repomd.xml", f"{prefix}/{outside.name}"])


def _copied(storage: Remote, directory: str, work: Path) -> dict[str, str]:
    """Return the packages of the metadata of the upstream copy `directory` of `storage`, by SHA-256."""
    prefix = f"{upstream_copy.PREFIX}/{directory}"
    assert remote.download(storage, f"{prefix}/repodata/repomd.xml", work / "repodata/repomd.xml")
    for href in repodata.files((work / "repodata/repomd.xml").read_bytes()).values():
        assert remote.download(storage, f"{prefix}/{href}", work / href)
    return {digest: repodata.location(e) for digest, e in (repodata.read(work) or {}).items()}


def _pins_from_the_copy(caplog: pytest.LogCaptureFixture) -> None:
    """Check that a new machine gets the pinned packages from the upstream copy, with its metadata, without the
    upstream repositories, nor a container.
    """
    pins = pinned.pins(Project.find())
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pinned, "VOLUME", volume := f"xcraft-pinned-test-{os.getpid()}-{secrets.token_hex(4)}")
        patch.setattr(pinned, "_checked", set())
        try:
            with caplog.at_level(logging.DEBUG, logger="xcraft"):
                repo = pinned.repository(Project.find())
            assert not [m for m in caplog.messages if m.startswith("running ") and ("pin.py" in m or "createrepo" in m)]
            files = {pinned.file_name(n, p) for n, p in pins.items()}
            assert {repodata.location(e) for e in (repodata.read(repo) or {}).values()} == files
            assert {f.name for f in repo.glob("*.rpm")} == files
        finally:
            subprocess.run(["podman", "volume", "rm", "-f", volume], check=False, capture_output=True)
    caplog.clear()


def should_copy_the_pinned_upstream_packages_to_the_storage_once(
    root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The pinned upstream packages are copied to the storage once, with their metadata, and publishing copies them
    from there rather than uploading them.
    """
    (root / "xcraft.toml").write_text(f'{REMOTE}extra = ["bc"]\n')
    storage = Project.find().config.remote["dev"]
    prefix = upstream_copy.PREFIX

    def clean() -> None:  # the copy of the bucket, which the other tests share
        if files := remote.listing(storage, f"{prefix}/"):
            remote.delete(storage, [f"{prefix}/{f}" for f in files])

    clean()
    try:
        # dnf only gets the copies the storages have: rather than ignoring them, with a message
        assert "[xcraft-upstream-dev]" not in upstream_copy.repo_file(Project.find())
        assert xcraft("build", "--dry-run")[0] == 0  # the lock, with bc
        code, output = xcraft("push", "upstream", "--to", "dev")
        assert code == 0, output
        assert "[xcraft-upstream-dev]" in upstream_copy.repo_file(Project.find())
        # browsable, like the other trees of the storage
        assert remote.exists(storage, f"{prefix}/{remote.INDEX}")
        assert remote.exists(storage, f"{prefix}/{ARCH}/{remote.INDEX}")
        pins = pinned.pins(Project.find())
        assert _copied(storage, ARCH, tmp_path / "1") == {p.sha256: pinned.file_name(n, p) for n, p in pins.items()}
        assert set(remote.listing(storage, f"{prefix}/{ARCH}/")) >= {pinned.file_name(n, p) for n, p in pins.items()}
        _pins_from_the_copy(caplog)
        # the source packages of what is published, not of the build environment, as [publish] build is false
        bc = pins["bc"]
        assert f"bc-{bc.evr.split(':')[-1]}.src.rpm" in _copied(storage, "Source", tmp_path / "2").values()
        # nothing to copy again, even during a build
        with caplog.at_level(logging.DEBUG, logger="xcraft"), (root / ".xcraft/lock").open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # as a build does
            code, output = xcraft("push", "upstream", "--to", "dev")
        assert code == 0, output
        assert not [m for m in caplog.messages if m.startswith(("uploading ", "copying "))]
        # metadata updated meanwhile without bc, which the next push adds again, alone
        work = tmp_path / "3"
        _copied(storage, ARCH, work)
        entries = {repodata.location(e): e for e in (repodata.read(work) or {}).values()}
        repodata.write(work, {f: e for f, e in entries.items() if not f.startswith("bc-")})
        for f in sorted((work / "repodata").iterdir(), key=lambda f: f.name == "repomd.xml"):
            remote.upload(storage, f"{prefix}/{ARCH}/repodata/{f.name}", f)
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="xcraft"):
            code, output = xcraft("push", "upstream", "--to", "dev")
        assert code == 0, output
        uploads = [m.split(" to ")[-1] for m in caplog.messages if m.startswith(("uploading ", "copying "))]
        assert [Path(u).name for u in uploads if u.endswith(".rpm")] == [pinned.file_name("bc", bc)]
        assert bc.sha256 in _copied(storage, ARCH, tmp_path / "4")
        # the metadata of before the previous update is gone, but not that of the previous one
        assert len([f for f in remote.listing(storage, f"{prefix}/{ARCH}/repodata/") if "primary" in f]) == 2
        # publishing in dev mode copies them from the copy, as they are, while final mode signs them again
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="xcraft"):
            code, output = xcraft("publish", "repos", "--to", "dev", "--stage", f"copy-{STAGE}", "--dev", "copy")
        assert code == 0, output
        copied = [m.split()[1] for m in caplog.messages if m.startswith("copying ")]
        assert f"s3://xcraft-dev/{prefix}/{ARCH}/{pinned.file_name('bc', bc)}" in copied
        assert not [m for m in caplog.messages if m.startswith("uploading ") and "/bc-" in m]
    finally:
        clean()


def should_add_the_given_rpms_to_the_upstream_copy(root: Path, tmp_path: Path) -> None:
    (root / "xcraft.toml").write_text(f'{REMOTE}extra = ["bc"]\n')
    storage = Project.find().config.remote["dev"]
    prefix = upstream_copy.PREFIX

    def clean() -> None:  # the copy of the bucket, which the other tests share
        if files := remote.listing(storage, f"{prefix}/"):
            remote.delete(storage, [f"{prefix}/{f}" for f in files])

    clean()
    try:
        assert xcraft("build", "trivial")[0] == 0  # the lock, with bc, and a package of ours
        bc = pinned.pins(project := Project.find())["bc"]
        pinned.download(project, {"bc": bc})
        rpm = pinned.stored("bc", bc)
        assert rpm
        # signed with the keys of the repositories only
        ours = next(root.glob(f".xcraft/cache/builds/*/{ARCH}/trivial/*/RPMS/**/*.rpm"))
        code, output = xcraft("push", "upstream", str(ours))
        assert code == 1
        assert "SIGNATURES NOT OK" in output
        assert not remote.listing(storage, f"{prefix}/")
        code, output = xcraft("push", "upstream", str(rpm))
        assert code == 0, output
        assert "upstream copy: 1 added, 0 there already" in output
        assert _copied(storage, ARCH, tmp_path / "1") == {bc.sha256: pinned.file_name("bc", bc)}
        assert remote.exists(storage, f"{prefix}/{ARCH}/{remote.INDEX}")
        code, output = xcraft("push", "upstream", str(rpm))
        assert code == 0, output
        assert "upstream copy: 0 added, 1 there already" in output
        # of the architecture of the copy only
        code, output = xcraft("push", "upstream", str(tmp_path / "bc-1.0-1.s390x.rpm"))
        assert code == 1
        assert f"not of {ARCH}: bc-1.0-1.s390x.rpm" in output
    finally:
        clean()


def should_fetch_the_isos_of_a_publication_but_the_corrupted_ones(root: Path, tmp_path: Path) -> None:
    project = Project.find()
    signer = release_signer(project)
    assert signer
    image = tmp_path / "test.iso"
    image.write_bytes(secrets.token_bytes(2048))
    trees = [cache_image(project, image, signer=signer, netinstall=netinstall) for netinstall in (False, True)]
    assert xcraft("push", "isos")[0] == 0
    for tree in trees:
        shutil.rmtree(tree)
    assert fetch_isos(project, signer=signer)
    assert [tree for tree in trees if not tree.exists()] == []
    # one corrupted: not there, for the publish to get the packages it is made from, but the others
    for tree in trees:
        shutil.rmtree(tree)
    key = iso_key(project, netinstall=True, volid=None, signer=signer)
    storage = project.config.remote["dev"]
    remote.upload(
        storage,
        f"{remote.read_tree(storage, iso_path(project, key), trusted=signer)[0]}/image.iso",
        root / "xcraft.toml",
    )
    assert not fetch_isos(project, signer=signer)
    assert trees[0].exists()
    assert not trees[1].exists()


def should_reject_manifest_paths_out_of_their_tree(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote.get("main")
    assert storage
    signer = release_signer(Project.find())
    assert signer
    key = tmp_path / "key.asc"
    key.write_text(signer.public_key())
    remote.upload(storage, f"{remote.KEYS}/{signer.key}.asc", key)
    payload = tmp_path / "payload"
    # nor be changed by the signature in clear, which drops the spaces ending its lines
    with pytest.raises(ValueError, match="invalid tree path"):
        remote.write_manifest(storage, "trees/bad", {"escaped ": "0" * 64}, signer=signer)
    for name in ("../escaped", "/var/escaped", "a/../../escaped"):
        payload.write_text(f"trees/bad\n{'0' * 64}  {name}\n")
        signer.clearsign(payload, tmp_path / remote.MANIFEST)
        remote.upload(storage, f"trees/bad/{remote.MANIFEST}", tmp_path / remote.MANIFEST)
        assert not remote.download_tree(storage, "trees/bad", tmp_path / "tree")
    assert not list(tmp_path.glob("**/escaped"))


def should_reject_key_files_with_another_key(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote.get("main")
    signer = release_signer(Project.find())
    assert storage
    assert signer
    other = create_dev_key(Project.find(), "intruder")
    key = tmp_path / "key.asc"
    key.write_text(signer.public_key() + other.public_key())
    remote.upload(storage, f"{remote.KEYS}/{signer.key}.asc", key)
    assert remote.public_key(storage, signer.key) is None
    key.write_text(signer.public_key())
    remote.upload(storage, f"{remote.KEYS}/{signer.key}.asc", key)
    remote.forget()
    assert remote.public_key(storage, signer.key) == signer.public_key()


def should_show_the_sizes_of_nested_directories_in_their_pages(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote["main"]
    (tmp_path / "file").write_bytes(b"x" * 1000)
    remote.upload(storage, "nested/a/b/file", tmp_path / "file")
    remote_pages.update_pages(Project.find(), storage, ["nested/a/", "nested/"])
    assert 'href="b/">b/</a></td><td data-k="1000"' in page("nested/a/")
    assert 'href="a/">a/</a></td><td data-k="1000"' in page("nested/")


def should_show_the_logo_of_the_project_in_the_pages(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote["main"]
    (tmp_path / "file").write_bytes(b"x" * 1000)
    remote.upload(storage, "logo/file", tmp_path / "file")
    remote_pages.update_pages(Project.find(), storage, ["logo/"])
    assert "<img" not in page("logo/")
    before, sep, after = (root / "xcraft.toml").read_text().partition("\n[")
    (root / "xcraft.toml").write_text(f'{before}logo = "https://xcp-ng.org/assets/img/mainlogo.png"\n{sep}{after}')
    remote_pages.update_pages(Project.find(), storage, ["logo/"])
    assert '<img src="https://xcp-ng.org/assets/img/mainlogo.png" alt="">' in page("logo/")


def should_read_directory_totals_through_a_proxy(root: Path, tmp_path: Path) -> None:
    """The totals of the pages of the subdirectories are read from their metadata, whose names a proxy may change the
    case of.
    """
    project = Project.find()
    storage = project.config.remote["main"]
    file = tmp_path / "file"
    file.write_bytes(b"x" * 1000)
    prefix = f"cased-{secrets.token_hex(4)}"
    for directory in ("listed", "paged"):
        remote.upload(storage, f"{prefix}/{directory}/file", file)
    remote_pages.update_pages(project, storage, [f"{prefix}/paged/"])

    class Proxy(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # for Expect: 100-continue

        def do_GET(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            headers = {k: v for k, v in self.headers.items() if k.lower() != "expect"}
            with closing(http.client.HTTPConnection("localhost", S3_PORT)) as connection:
                connection.request(self.command, self.path, body, headers=headers)
                response = connection.getresponse()
                data = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(key.title(), value)
                self.send_header("Content-Length", response.getheader("Content-Length") or str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        do_HEAD = do_PUT = do_GET  # noqa: N815, as http.server names them

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    with http.server.ThreadingHTTPServer(("localhost", 0), Proxy) as proxy:
        thread = threading.Thread(target=proxy.serve_forever)
        thread.start()
        try:
            proxied = storage.model_copy(update={"s3_endpoint": f"http://localhost:{proxy.server_port}"})
            remote_pages.update_pages(project, proxied, [f"{prefix}/listed/"])
        finally:
            proxy.shutdown()
            thread.join()
    assert 'href="paged/">paged/</a></td><td data-k="1000"' in page(f"{prefix}/")


def should_list_the_signing_keys_in_a_page(root: Path, tmp_path: Path) -> None:
    project = Project.find()
    storage = project.config.remote["main"]
    signer = release_signer(project)
    assert signer
    file = tmp_path / "file"
    file.write_text("signed tree")
    remote.upload_tree(storage, "pages-with-key", tmp_path, ["file"], signer=signer)
    remote_pages.update_pages(project, storage, ["pages-with-key/"])
    assert f'href="{signer.key}.asc"' in page(".xcraft/keys/")
    internal = page(".xcraft/")
    assert 'href="keys/"' in internal
    assert re.search(r'href="keys/".*?<td data-k="[1-9][0-9]*">', internal)


def should_neither_push_nor_fetch_development_builds(root: Path) -> None:
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    assert xcraft("build", "trivial")[0] == 0
    code, output = xcraft("push", "builds", "--to", "main")
    assert code == 0, output
    assert "development builds, not pushed: trivial, with-deps" in output.splitlines()
    assert "0 pushed" in output
    code, output = xcraft("fetch", "builds")
    assert code == 0, output
    assert "development builds, not fetched: trivial, with-deps" in output.splitlines()
    # their final builds, explicitly
    assert xcraft("build", "trivial", "--final")[0] == 0
    code, output = xcraft("push", "builds", "trivial", "--final", "--to", "main")
    assert code == 0, output
    assert "↑ trivial" in output
    # and with the sources, of all the packages
    assert xcraft("build", "--final")[0] == 0
    code, output = xcraft("push", "--final", "--to", "main")
    assert code == 0, output
    assert "↑ with-deps" in output


def should_fetch_the_builds_reused_from_the_main_branch(root: Path) -> None:
    assert xcraft("build")[0] == 0
    commit_base(root)
    assert xcraft("push", "builds", "--to", "main")[0] == 0
    # on a branch, with-deps reuses the build of the main branch, built against the previous trivial
    git(root, "checkout", "-qb", "feature")
    trivial = root / "base/trivial/trivial.spec"
    trivial.write_text(f"# changed\n{trivial.read_text()}")
    assert xcraft("reuse", "with-deps")[0] == 0
    shutil.rmtree(root / ".xcraft/cache/builds")
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert "↓ with-deps fetched" in output
    assert output.splitlines()[-1] == "1 built, 1 fetched"
    # never built again
    shutil.rmtree(root / ".xcraft/cache/builds")
    code, output = xcraft("build", "with-deps", "--fetch=none")
    assert code == 1
    assert "✗ with-deps: not built, and the remote storage is not used" in output
