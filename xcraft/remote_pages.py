"""The pages of the directories of the remote storage, which S3 storages do not make: their files and
subdirectories, with their sizes and dates.
"""

from __future__ import annotations

import datetime as dt
import math
from concurrent.futures import ThreadPoolExecutor
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import quote

from xcraft.process import log
from xcraft.remote import CCACHE, INDEX, KEYS, REQUESTS, Entry, objects, writing

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

    from xcraft.project import Project, Remote

_Dirs = dict[str, tuple[int, dt.datetime | None]]
"""Subdirectories, with the size of all that is below each, and the date of the newest file, if known."""


_STYLE = """
<style>
:root { color-scheme: light dark }
body { margin: 0 auto; padding: 2.5rem 1.5rem 6rem; max-width: 64rem;
       font-family: system-ui, sans-serif; font-size: .9rem; color: #22222c; background: #fff }
img { display: block; width: 10rem; margin-bottom: 1.5rem }
h1 { font-size: 1.1rem; font-weight: 600; margin: 0 0 1.25rem }
a { color: inherit; text-decoration: none }
table { border-collapse: collapse; table-layout: fixed; width: 100% }
th { font-size: .7rem; font-weight: 600; text-transform: uppercase; letter-spacing: .08em;
     color: #8b8b94; cursor: pointer; user-select: none }
td { border-top: 1px solid #ececf1; color: #6d6d76 }
th, td { padding: .4rem 0; text-align: left }
th + th, td + td { padding-left: 1.5rem }
th:nth-child(n+2), td:nth-child(n+2) { text-align: right; white-space: nowrap }
th:nth-child(2) { width: 6rem }
th:nth-child(3) { width: 9rem }
td:first-child { color: inherit; overflow-wrap: anywhere }
tbody tr:hover { background: #f6f6f9 }
tbody a:hover { text-decoration: underline }
@media (prefers-color-scheme: dark) {
  body { color: #e4e4ec; background: #14141a }
  th { color: #9797a3 }
  td { border-color: #26262f; color: #ababb6 }
  tbody tr:hover { background: #1d1d25 }
}
</style>"""

# sorts the rows by the data-k of the cells of a column, at a click on its header, then in reverse
_SORT = """<script>
document.querySelectorAll("th").forEach((th, i) => th.onclick = () => {
  const order = th.dataset.order = th.dataset.order === "1" ? "-1" : "1", body = document.querySelector("tbody");
  body.append(...[...body.rows].sort((a, b) =>
    order * a.cells[i].dataset.k.localeCompare(b.cells[i].dataset.k, undefined, {numeric: true})));
});
</script>"""


def _total(dirs: _Dirs, files: dict[str, Entry]) -> tuple[int, dt.datetime | None]:
    """Return the size of all that is in a directory, and the date of its newest file."""
    entries = [*dirs.values(), *files.values()]
    return sum(size for size, _ in entries), max((date for _, date in entries if date), default=None)


def _stored_total(s3: S3Client, bucket: str, prefix: str) -> tuple[int, dt.datetime | None]:
    """Return the size of all that is below `prefix`, and the date of its newest file, which its page keeps."""
    log.debug("checking s3://%s/%s%s", bucket, prefix, INDEX)
    try:
        metadata = {
            key.lower(): value for key, value in s3.head_object(Bucket=bucket, Key=prefix + INDEX)["Metadata"].items()
        }
    except s3.exceptions.ClientError:  # no page yet
        return 0, None
    newest = metadata.get("newest")
    return int(metadata.get("size", 0)), dt.datetime.fromisoformat(newest) if newest else None


_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def _human_size(size: int) -> str:
    exponent = min(int(math.log(size, 1024)), len(_UNITS) - 1) if size else 0
    return f"{size / 1024**exponent:.1f} {_UNITS[exponent]}" if exponent else f"{size} B"


def _row(name: str, href: str, size: int, date: dt.datetime | None) -> str:
    """Return the row of a file or a directory, with the sort keys of its cells."""
    key, shown = (f"{date:%Y-%m-%dT%H:%M:%S}", f"{date:%Y-%m-%d %H:%M}") if date else ("", "")
    return (
        f'<tr><td data-k="{escape(name)}"><a href="{quote(href)}">{escape(href)}</a></td>'
        f'<td data-k="{size}">{_human_size(size)}</td><td data-k="{key}">{shown}</td></tr>'
    )


def _page(prefix: str, dirs: _Dirs, files: dict[str, Entry], logo: str | None) -> str:
    rows = [_row(d, f"{d}/", *total) for d, total in sorted(dirs.items())]
    rows += [_row(f, f, *entry) for f, entry in sorted(files.items())]
    parent = '<tr><td><a href="../">../</a></td></tr>' if prefix else ""
    title = escape(f"/{prefix}")
    img = f'<img src="{escape(logo)}" alt="">\n' if logo else ""
    return (
        f'<!DOCTYPE html>\n<html><head><meta charset="utf-8"><title>{title}</title>{_STYLE}</head><body>\n'
        f"{img}<h1>{title}</h1>\n<table>\n<thead><tr><th>Name</th><th>Size</th><th>Date</th></tr>{parent}</thead>\n"
        "<tbody>\n" + "\n".join(rows) + f"\n</tbody>\n</table>\n{_SORT}\n</body></html>\n"
    )


def _paged(directory: str) -> bool:
    """Return whether `directory` has a page: not those in the trees of the compiler caches, too many for their use."""
    return not directory.startswith(f"{CCACHE}/") or directory.count("/") <= CCACHE.count("/") + 3


_Pages = dict[str, tuple[_Dirs, dict[str, Entry]]]
"""The subdirectories and files of the pages, by directory."""


def _add_listing(pages: _Pages, prefix: str, files: dict[str, Entry]) -> None:
    """Add to `pages` the directories under `prefix` with the `files` below it, by path, and their own files."""
    pages.setdefault(prefix, ({}, {}))
    for path, (size, date) in files.items():
        *parts, name = path.split("/")
        for i, part in enumerate(parts):
            if not _paged(directory := prefix + "".join(f"{p}/" for p in parts[:i])):
                break
            dirs = pages.setdefault(directory, ({}, {}))[0]
            below, newest = dirs.get(part, (0, None))
            dirs[part] = (below + size, max(filter(None, (newest, date))))
        if _paged(directory := prefix + "".join(f"{p}/" for p in parts)):
            pages.setdefault(directory, ({}, {}))[1][name] = (size, date)


def _subtotal(s3: S3Client, bucket: str, pages: _Pages, directory: str) -> tuple[int, dt.datetime | None]:
    """Return the size of all that is below `directory`, and the date of its newest file: from its page in `pages`, or
    the one in the storage.
    """
    return _total(*pages[directory]) if directory in pages else _stored_total(s3, bucket, directory)


def update_pages(project: Project, storage: Remote, prefixes: list[str]) -> int:
    """Write the pages of the directories under the directories `prefixes`, and of their parents; return how many.

    The directories are the prefixes ending with ``/``, and ``""`` is the root. Those without page, see _paged(), count
    in the size of the directory above.
    """
    pages: _Pages = {}
    parents: set[str] = set()
    with writing(storage, "make the directory pages") as s3:
        if objects(s3, storage.bucket, f"{KEYS}/", below=True)[1]:
            prefixes = [*prefixes, f"{KEYS}/"]
        # those under another one are in its listing
        for prefix in sorted(p for p in set(prefixes) if not any(p != q and p.startswith(q) for q in prefixes)):
            _add_listing(pages, prefix, objects(s3, storage.bucket, prefix, below=True)[1])
            above = prefix.rstrip("/").split("/")[:-1] if prefix else []
            parents |= {"", *("".join(f"{p}/" for p in above[: i + 1]) for i in range(len(above)))}
        # the parents are only listed, the deepest first, with the totals of their subdirectories' pages
        for parent in sorted(parents - pages.keys(), key=lambda p: -p.count("/")):
            dirs, files = objects(s3, storage.bucket, parent, below=False)
            pages[parent] = ({d: _subtotal(s3, storage.bucket, pages, f"{parent}{d}/") for d in dirs}, files)

        def write(prefix: str) -> None:
            dirs, files = pages[prefix]
            size, newest = _total(dirs, files)
            log.debug("writing s3://%s/%s%s", storage.bucket, prefix, INDEX)
            s3.put_object(
                Bucket=storage.bucket,
                Key=prefix + INDEX,
                Body=_page(prefix, dirs, files, project.config.logo).encode(),
                ContentType="text/html; charset=utf-8",
                # revalidated by the browsers, by their ETag: they change with each push, under the same name
                CacheControl="no-cache",
                Metadata={"size": str(size), **({"newest": newest.isoformat()} if newest else {})},
            )

        with ThreadPoolExecutor(REQUESTS) as pool:
            list(pool.map(write, pages))
    return len(pages)
