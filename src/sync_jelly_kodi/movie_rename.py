"""Discover and rename misnamed movies in the TRANSCODED folder.

Discovery reads Jellyfin metadata already cached in the SQLite ``jellyitems`` table
(populated by ``jelly_pull``). Renaming happens on the locally mounted TRANSCODED
share (``TRANSCODED_LOCAL_PATH``). Because that share is CIFS (case-insensitive), a
rename that only changes case is bounced through an intermediate temp filename.
"""
import logging
import os

from .naming import is_kodi_named, proposed_filename
from .sqlite_util import get_transcoded_movie_items

logger = logging.getLogger(__name__)


def _transcoded_dir() -> str:
    return os.getenv("TRANSCODED_LOCAL_PATH", "")


def get_transcoded_movies() -> list[dict]:
    """Return misnamed TRANSCODED movies with their proposed canonical names.

    Deduplicates by ``unified_file`` (the same physical file appears once per
    Jellyfin user). Only rows whose current filename does NOT already follow the
    ``Title_(YEAR)`` convention are returned.
    """
    directory = _transcoded_dir()
    seen: set[str] = set()
    rows: list[dict] = []

    for item in get_transcoded_movie_items():
        # unified_file is a root-relative path (e.g. "/Akira_(1988).mkv"); TRANSCODED
        # movies are flat, so reduce to the bare filename for naming + on-disk checks.
        current_file = os.path.basename(item.get("unified_file") or "")
        if not current_file or current_file in seen:
            continue
        seen.add(current_file)

        if is_kodi_named(current_file):
            continue  # already correctly named

        title = item.get("Name") or ""
        year = item.get("ProductionYear") or item.get("Year")
        _stem, ext = os.path.splitext(current_file)
        ext = ext.lstrip(".")
        has_metadata = bool(title) and bool(year)
        proposed = proposed_filename(title, year, ext) if has_metadata else ""

        exists_on_disk = bool(directory) and os.path.isfile(
            os.path.join(directory, current_file)
        )

        rows.append(
            {
                "current_file": current_file,
                "title": title,
                "year": year,
                "ext": ext,
                "proposed": proposed,
                "has_metadata": has_metadata,
                "exists_on_disk": exists_on_disk,
            }
        )

    rows.sort(key=lambda r: r["current_file"].lower())
    return rows


def case_safe_rename(directory: str, src: str, dst: str) -> None:
    """Rename ``src`` to ``dst`` within ``directory``, safe for case-insensitive shares.

    Raises FileNotFoundError if ``src`` is missing, FileExistsError if ``dst`` already
    names a *different* file.
    """
    src_path = os.path.join(directory, src)
    dst_path = os.path.join(directory, dst)

    if not os.path.isfile(src_path):
        raise FileNotFoundError(f"Source file does not exist: {src_path}")

    case_only = src != dst and src.lower() == dst.lower()

    # A destination that already exists is only OK when it's the same file we're
    # renaming via a case-only change (CIFS reports the existing entry).
    if os.path.exists(dst_path) and not case_only:
        raise FileExistsError(f"Destination already exists: {dst_path}")

    if case_only:
        tmp_path = os.path.join(directory, f"{dst}.tmp.{os.getpid()}")
        os.rename(src_path, tmp_path)
        os.rename(tmp_path, dst_path)
    else:
        os.rename(src_path, dst_path)


def rename_movie(current_file: str, proposed: str) -> tuple[bool, str]:
    """Rename a TRANSCODED movie file to ``proposed``. Returns (ok, message)."""
    directory = _transcoded_dir()
    if not directory:
        return False, "TRANSCODED_LOCAL_PATH is not configured."

    # Guard against path traversal; operate on bare filenames only.
    current_file = os.path.basename(current_file or "")
    proposed = os.path.basename(proposed or "")
    if not current_file or not proposed:
        return False, "Both current and proposed filenames are required."

    try:
        case_safe_rename(directory, current_file, proposed)
    except (FileNotFoundError, FileExistsError) as e:
        logger.warning("Rename failed: %s", e)
        return False, str(e)
    except OSError as e:
        logger.error("Rename failed: %s", e)
        return False, f"OS error: {e}"

    logger.info("Renamed '%s' -> '%s' in %s", current_file, proposed, directory)
    return True, f"Renamed to {proposed}"
