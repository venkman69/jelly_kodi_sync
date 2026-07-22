"""Kodi/kodidash movie naming helpers.

Ported from ../kodidash (src/kodidash/util.py and dashdb.py) so this project does
not need to import the sibling `kodidash` package. The canonical movie name is:

    Title_With_Underscores_(YEAR).ext

e.g. ``The_Matrix_(1999).mkv``.
"""
import os
import re

# Matches a Kodi-style filename stem: <title>_(YEAR)
# e.g. "The_Matrix_(1999)" -> title "The_Matrix", year "1999".
KODI_NAME_RE = re.compile(r"^(.+)_\((\d{4})\)$", re.UNICODE)


def is_kodi_named(filename: str) -> bool:
    """Return True if ``filename`` already follows the ``Title_(YEAR)`` convention.

    A name is considered correct when its stem is ``<title>_(YEAR)`` AND the title
    part is already in canonical form — i.e. running ``windows_compatible_title`` on
    it changes nothing. This keeps the check consistent with the name we would rename
    to, so canonical names that contain punctuation the builder preserves (``!``,
    ``&``, etc.) are not flagged as misnamed.
    """
    stem, _ext = os.path.splitext(filename)
    match = KODI_NAME_RE.fullmatch(stem)
    if not match:
        return False
    title_part = match.group(1)
    return windows_compatible_title(title_part) == title_part


def windows_compatible_title(title: str) -> str:
    """Convert a plain title to a Windows/Kodi-safe, underscore-joined form.

    Port of kodidash ``dashdb.windows_compatible_title``:
    strip, spaces -> ``_``, drop ``'``, ``:`` -> ``_``, collapse ``__``, drop ``?`` and ``,``.
    """
    if not title:
        return ""
    moviename = title.strip().replace(" ", "_")
    moviename = moviename.replace("'", "")
    moviename = moviename.replace(":", "_")
    moviename = moviename.replace("__", "_")
    moviename = moviename.replace("?", "")
    moviename = moviename.replace(",", "")
    return moviename


def proposed_filename(title: str, year, ext: str) -> str:
    """Build the canonical filename from a title, year and extension.

    ``ext`` may include or omit a leading dot. Returns e.g. ``The_Matrix_(1999).mkv``.
    """
    safe_title = windows_compatible_title(title)
    ext = ext.lstrip(".")
    return f"{safe_title}_({year}).{ext}"
