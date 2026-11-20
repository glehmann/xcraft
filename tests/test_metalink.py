from __future__ import annotations

import hashlib
import re

from xcraft import metalink


def _repomd(revision: int) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<repomd xmlns="http://linux.duke.edu/metadata/repo">'
        f"<revision>{revision}</revision></repomd>\n"
    ).encode()


def _hashes(text: str) -> list[str]:
    return re.findall(r'<hash type="sha256">([0-9a-f]{64})</hash>', text)


def test_metalink() -> None:
    urls = ["https://mirror.example/x/repodata/repomd.xml", "http://ours.example/x/repodata/repomd.xml"]
    first = metalink.text(urls, _repomd(1))
    assert _hashes(first) == [hashlib.sha256(_repomd(1)).hexdigest()]
    assert "<mm0:timestamp>1</mm0:timestamp>" in first
    assert f"<size>{len(_repomd(1))}</size>" in first
    # the servers in order of preference
    assert re.findall(r'type="(\w+)" preference="(\d+)">([^<]*)<', first) == [
        ("https", "100", urls[0]), ("http", "99", urls[1]),
    ]  # fmt: skip
    # the previous versions as alternates, the newest first, a few only, and never the current one
    texts = [first]
    for revision in range(2, 7):
        texts.append(metalink.text(urls, _repomd(revision), texts[-1]))
    assert _hashes(texts[-1]) == [hashlib.sha256(_repomd(r)).hexdigest() for r in (6, 5, 4, 3)]
    assert _hashes(metalink.text(urls, _repomd(6), texts[-1])) == _hashes(texts[-1])
