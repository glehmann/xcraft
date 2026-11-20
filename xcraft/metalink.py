"""The metalinks of the repositories: where dnf finds their repomd.xml, on our server and the mirrors, and its hash.

A metalink lists the URL of the repomd.xml of a repository on each server, with its size, SHA-256 and timestamp, and
those of the previous ones, its alternates: dnf takes the repository from the first server whose repomd.xml matches
one of them, skipping the stale or broken mirrors. It is the format of MirrorManager, which dnf reads.
"""

from __future__ import annotations

import hashlib
import re
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

FILE = "metalink.xml"
"""The metalink of a repository, in its repodata/ directory, next to repomd.xml."""
ALTERNATES = 3
"""The previous repomd.xml a metalink accepts, for the mirrors a few syncs behind."""
_ALTERNATE = re.compile(r"<mm0:alternate>.*?</mm0:alternate>", re.DOTALL)


def _version(repomd: bytes) -> str:
    """Return the timestamp, size and SHA-256 of `repomd`, as a metalink has them."""
    revision = ET.fromstring(repomd).findtext("{http://linux.duke.edu/metadata/repo}revision") or "0"  # noqa: S314
    return (
        f"<mm0:timestamp>{escape(revision)}</mm0:timestamp><size>{len(repomd)}</size>"
        f'<verification><hash type="sha256">{hashlib.sha256(repomd).hexdigest()}</hash></verification>'
    )


def text(urls: list[str], repomd: bytes, previous: str | None = None) -> str:
    """Return the metalink of a repository whose repomd.xml is `repomd`, at `urls`, the first ones preferred, with
    its version in the `previous` metalink, if any, and its alternates, as alternates.
    """
    alternates = []
    if previous and (found := re.search(r'<file name="repomd.xml">(.*?)<mm0:alternates>', previous, re.DOTALL)):
        alternates = [f"<mm0:alternate>{found[1].strip()}</mm0:alternate>", *_ALTERNATE.findall(previous)]
    current = _version(repomd)
    alternates = [a for a in alternates if current not in a][:ALTERNATES]
    resources = "".join(
        f'<url protocol="{url.split(":", 1)[0]}" type="{url.split(":", 1)[0]}" preference="{100 - i}">'
        f"{escape(url)}</url>\n"
        for i, url in enumerate(urls)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<metalink version="3.0" xmlns="http://www.metalinker.org/" '
        'xmlns:mm0="http://fedorahosted.org/mirrormanager">\n'
        f"<files><file name={quoteattr('repomd.xml')}>{current}\n"
        f"<mm0:alternates>{''.join(alternates)}</mm0:alternates>\n"
        f'<resources maxconnections="1">\n{resources}</resources>\n'
        "</file></files>\n</metalink>\n"
    )
