"""Discover and rename misnamed movies in the TRANSCODED folder.

Discovery reads Jellyfin metadata already cached in the SQLite ``jellyitems`` table
(populated by ``jelly_pull``). Renaming happens on the locally mounted TRANSCODED
share (``TRANSCODED_LOCAL_PATH`` / ``TRANSCODED``). Because that share is CIFS
(case-insensitive), a rename that only changes case is bounced through an
intermediate temp filename.

A rename also carries along any sidecar files that share the video's stem
(subtitles ``.srt``, metadata ``.nfo``, artwork ``-poster.jpg`` etc.), so those
associations survive.
"""
import logging
import os
import re
from collections import Counter

from .naming import is_kodi_named, proposed_filename
from .sqlite_util import get_transcoded_movie_items

logger = logging.getLogger(__name__)

# Extensions treated as the movie itself (never as a sidecar of another movie).
VIDEO_EXTS = {".mkv", ".mp4", ".ts", ".avi", ".m4v", ".mov", ".wmv", ".mpg", ".mpeg"}

# Characters Windows/CIFS cannot store in a filename. Jellyfin (which may read the
# original names) can report these, while the CIFS mount exposes a mapped variant,
# so exact-name matching fails. Normalizing both sides lets us locate the real file.
_ILLEGAL_RE = re.compile(r'[:*?"<>|]')


def _transcoded_dir() -> str:
    # Accept TRANSCODED_LOCAL_PATH, or plain TRANSCODED (kodidash-style .env naming).
    return os.getenv("TRANSCODED_LOCAL_PATH") or os.getenv("TRANSCODED", "")


def _normalize_illegal(name: str) -> str:
    return _ILLEGAL_RE.sub("_", name)


def _resolve_source(directory: str, name: str) -> str | None:
    """Return the real on-disk filename for ``name`` in ``directory``, or None.

    Tries an exact match first, then falls back to matching after normalizing
    Windows-illegal characters (to locate CIFS-mangled names, e.g. Jellyfin's
    ``foo: bar.mp4`` stored on disk as ``foo" bar.mp4``).
    """
    if not directory:
        return None
    if os.path.isfile(os.path.join(directory, name)):
        return name
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    target = _normalize_illegal(name)
    matches = [e for e in entries if _normalize_illegal(e) == target]
    return matches[0] if len(matches) == 1 else None


def _find_sidecars(directory: str, video_name: str) -> list[str]:
    """Non-video files in ``directory`` sharing ``video_name``'s stem."""
    stem = os.path.splitext(video_name)[0]
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    sidecars = []
    for e in entries:
        if e == video_name or not e.startswith(stem):
            continue
        if os.path.splitext(e)[1].lower() in VIDEO_EXTS:
            continue  # a sibling video variant, not a sidecar
        sidecars.append(e)
    return sidecars


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

        # Resolve the real on-disk name (handles CIFS-mangled illegal chars).
        real_source = _resolve_source(directory, current_file)

        rows.append(
            {
                "current_file": current_file,
                "title": title,
                "year": year,
                "ext": ext,
                "proposed": proposed,
                "has_metadata": has_metadata,
                "exists_on_disk": real_source is not None,
            }
        )

    # Flag collisions: multiple sources proposing the same name, or a proposed name
    # that already exists on disk as a different file.
    proposed_counts = Counter(r["proposed"] for r in rows if r["proposed"])
    for r in rows:
        p = r["proposed"]
        dup = bool(p) and proposed_counts[p] > 1
        target_exists = bool(p and directory) and os.path.isfile(
            os.path.join(directory, p)
        )
        r["collision"] = dup or target_exists

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
    """Rename a TRANSCODED movie (and its sidecars) to ``proposed``.

    Returns (ok, message).
    """
    directory = _transcoded_dir()
    if not directory:
        return False, "TRANSCODED_LOCAL_PATH is not configured."

    # Guard against path traversal; operate on bare filenames only.
    current_file = os.path.basename(current_file or "")
    proposed = os.path.basename(proposed or "")
    if not current_file or not proposed:
        return False, "Both current and proposed filenames are required."

    # Locate the real on-disk file (may differ from Jellyfin's name via CIFS mapping).
    real_source = _resolve_source(directory, current_file)
    if real_source is None:
        return False, f"Source file not found on disk: {current_file}"

    old_stem = os.path.splitext(real_source)[0]
    new_stem = os.path.splitext(proposed)[0]
    sidecars = _find_sidecars(directory, real_source)

    # Rename the video first; abort on failure before touching sidecars.
    try:
        case_safe_rename(directory, real_source, proposed)
    except (FileNotFoundError, FileExistsError) as e:
        logger.warning("Rename failed: %s", e)
        return False, str(e)
    except OSError as e:
        logger.error("Rename failed: %s", e)
        return False, f"OS error: {e}"

    logger.info("Renamed '%s' -> '%s' in %s", real_source, proposed, directory)

    # Carry along sidecars (best-effort; a sidecar failure doesn't undo the video).
    moved, failed = 0, []
    for side in sidecars:
        new_side = new_stem + side[len(old_stem):]
        try:
            case_safe_rename(directory, side, new_side)
            moved += 1
            logger.info("Renamed sidecar '%s' -> '%s'", side, new_side)
        except OSError as e:
            failed.append(side)
            logger.warning("Sidecar rename failed for '%s': %s", side, e)

    msg = f"Renamed to {proposed}"
    if moved:
        msg += f" (+{moved} sidecar{'s' if moved != 1 else ''})"
    if failed:
        msg += f"; {len(failed)} sidecar(s) failed"
    return True, msg
