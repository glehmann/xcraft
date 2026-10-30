"""Release bumps, versions and changelog entries in specs."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

_RELEASE_RE = re.compile(r"^Release:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
_VERSION_RE = re.compile(r"^Version:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
_MACRO_RE = re.compile(r"%\{[^}]*\}|%\w+")
_CHANGELOG_RE = re.compile(r"^%changelog[ \t]*\n", re.MULTILINE)


def _release(spec: str) -> re.Match[str]:
    if not (m := _RELEASE_RE.search(spec)):
        msg = "no Release in the spec"
        raise ValueError(msg)
    return m


def bump_release(spec: str) -> str:
    """Return `spec` with the last number of its Release, outside of macros, incremented: the last part of it between
    dots and macros which is a number.

    That is the XCP-ng part in releases like ``1%{?xsrel}.3%{?dist}``, and not a number in a hash, like in
    ``2.219ab5b``.
    """
    m = _release(spec)
    release = m.group(1)
    outside_macros = _MACRO_RE.sub(lambda macro: "%" * len(macro.group()), release)
    if not (numbers := list(re.finditer(r"(?<![^.%])\d+(?![^.%])", outside_macros))):
        msg = f"no number to bump in Release: {release}"
        raise ValueError(msg)
    n = numbers[-1]
    return f"{spec[: m.start(1) + n.start()]}{int(n.group()) + 1}{spec[m.start(1) + n.end() :]}"


def reset_release(spec: str) -> str:
    """Return `spec` with 1 as its Release, followed by the macros its Release ends with, like ``%{?dist}``."""
    m = _release(spec)
    suffix = re.findall(rf"(?:{_MACRO_RE.pattern})*$", m.group(1))[0]
    return f"{spec[: m.start(1)]}1{suffix}{spec[m.end(1) :]}"


def set_version(spec: str, version: str) -> str:
    """Return `spec` with `version` as its Version, or as the macro its Version is, like ``%{usver}``."""
    if not (m := _VERSION_RE.search(spec)):
        msg = "no Version in the spec"
        raise ValueError(msg)
    if not (macro := re.fullmatch(r"%\{?(\w+)\}?", m.group(1))):
        if "%" in m.group(1):
            msg = f"cannot set a Version made of macros: {m.group(1)}"
            raise ValueError(msg)
        return f"{spec[: m.start(1)]}{version}{spec[m.end(1) :]}"
    definition = re.compile(rf"^%(?:global|define)[ \t]+{macro.group(1)}[ \t]+(\S+)[ \t]*$", re.MULTILINE)
    if len(found := list(definition.finditer(spec))) != 1:
        msg = f"not one definition of the macro of the Version, {m.group(1)}, in the spec"
        raise ValueError(msg)
    return f"{spec[: found[0].start(1)]}{version}{spec[found[0].end(1) :]}"


def add_changelog_entry(spec: str, *, evr: str, author: str, message: str, day: date) -> str:
    """Return `spec` with a changelog entry for `evr` before the others, with an empty item without `message`."""
    if "%autochangelog" in spec:
        msg = "the spec uses %autochangelog"
        raise ValueError(msg)
    entry = f"* {day:%a %b %d %Y} {author} - {evr}\n-{f' {message}' if message else ''}\n"
    if not (m := _CHANGELOG_RE.search(spec)):
        return f"{spec.rstrip()}\n\n%changelog\n{entry}"
    return f"{spec[: m.end()]}{entry}\n{spec[m.end() :]}"
