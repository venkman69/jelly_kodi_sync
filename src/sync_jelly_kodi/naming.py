"""Kodi/kodidash movie naming helpers.

Ported from ../kodidash (src/kodidash/util.py and dashdb.py) so this project does
not need to import the sibling `kodidash` package. The canonical movie name is:

    Title_With_Underscores_(YEAR).ext

e.g. ``The_Matrix_(1999).mkv``.
"""
import logging
import os
import re

logger = logging.getLogger(__name__)

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
        logger.debug("is_kodi_named('%s'): stem '%s' does not match Title_(YEAR) pattern -> False", filename, stem)
        return False
    title_part = match.group(1)
    canonical = windows_compatible_title(title_part)
    result = canonical == title_part
    if result:
        logger.debug("is_kodi_named('%s'): stem matches pattern, title_part '%s' is already canonical -> True", filename, title_part)
    else:
        logger.debug(
            "is_kodi_named('%s'): stem matches pattern but title_part '%s' != canonical '%s' -> False (would be renamed)",
            filename, title_part, canonical,
        )
    return result


def windows_compatible_title(title: str) -> str:
    """Convert a plain title to a Windows/Kodi-safe, underscore-joined form.

    Port of kodidash ``dashdb.windows_compatible_title``:
    strip, spaces -> ``_``, drop ``'``, ``:`` -> ``_``, collapse ``__``, drop ``?`` and ``,``.
    """
    if not title:
        return ""
    original = title
    moviename = title.strip().replace(" ", "_")
    moviename = moviename.replace("'", "")
    moviename = moviename.replace(":", "_")
    moviename = moviename.replace("__", "_")
    moviename = moviename.replace("?", "")
    moviename = moviename.replace(",", "")
    if moviename != original:
        logger.debug("windows_compatible_title: '%s' -> '%s'", original, moviename)
    return moviename


def proposed_filename(title: str, year, ext: str) -> str:
    """Build the canonical filename from a title, year and extension.

    ``ext`` may include or omit a leading dot. Returns e.g. ``The_Matrix_(1999).mkv``.
    """
    safe_title = windows_compatible_title(title)
    ext = ext.lstrip(".")
    result = f"{safe_title}_({year}).{ext}"
    logger.debug("proposed_filename: title='%s' year=%s ext='%s' -> '%s'", title, year, ext, result)
    return result
