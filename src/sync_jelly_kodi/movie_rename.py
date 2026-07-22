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
    val = os.getenv("TRANSCODED_LOCAL_PATH") or os.getenv("TRANSCODED", "")
    src = "TRANSCODED_LOCAL_PATH" if os.getenv("TRANSCODED_LOCAL_PATH") else ("TRANSCODED" if val else "unset")
    logger.debug("_transcoded_dir: using %s -> '%s'", src, val)
    return val


def _normalize_illegal(name: str) -> str:
    return _ILLEGAL_RE.sub("_", name)


def _resolve_source(directory: str, name: str) -> str | None:
    """Return the real on-disk filename for ``name`` in ``directory``, or None.

    Tries an exact match first, then falls back to matching after normalizing
    Windows-illegal characters (to locate CIFS-mangled names, e.g. Jellyfin's
    ``foo: bar.mp4`` stored on disk as ``foo" bar.mp4``).
    """
    if not directory:
        logger.debug("_resolve_source('%s'): directory is empty, returning None", name)
        return None
    exact_path = os.path.join(directory, name)
    if os.path.isfile(exact_path):
        logger.debug("_resolve_source('%s'): exact match found on disk", name)
        return name
    logger.debug("_resolve_source('%s'): no exact match, trying CIFS illegal-char normalization", name)
    try:
        entries = os.listdir(directory)
    except OSError as e:
        logger.debug("_resolve_source('%s'): listdir failed: %s", name, e)
        return None
    target = _normalize_illegal(name)
    logger.debug("_resolve_source: normalized target = '%s'", target)
    matches = [e for e in entries if _normalize_illegal(e) == target]
    if len(matches) == 1:
        logger.debug("_resolve_source('%s'): CIFS-normalized match found -> '%s'", name, matches[0])
        return matches[0]
    if len(matches) > 1:
        logger.debug("_resolve_source('%s'): ambiguous CIFS matches %s, returning None", name, matches)
    else:
        logger.debug("_resolve_source('%s'): no match found in directory", name)
    return None


def _find_sidecars(directory: str, video_name: str) -> list[str]:
    """Non-video files in ``directory`` sharing ``video_name``'s stem."""
    stem = os.path.splitext(video_name)[0]
    logger.debug("_find_sidecars: looking for sidecars of '%s' (stem='%s')", video_name, stem)
    try:
        entries = os.listdir(directory)
    except OSError as e:
        logger.debug("_find_sidecars: listdir failed: %s", e)
        return []
    sidecars = []
    for e in entries:
        if e == video_name:
            continue
        if not e.startswith(stem):
            continue
        ext = os.path.splitext(e)[1].lower()
        if ext in VIDEO_EXTS:
            logger.debug("_find_sidecars: skipping '%s' (sibling video, ext='%s')", e, ext)
            continue
        logger.debug("_find_sidecars: found sidecar '%s'", e)
        sidecars.append(e)
    logger.debug("_find_sidecars: %d sidecar(s) found for '%s': %s", len(sidecars), video_name, sidecars)
    return sidecars


def get_transcoded_movies() -> list[dict]:
    """Return misnamed TRANSCODED movies with their proposed canonical names.

    Deduplicates by ``unified_file`` (the same physical file appears once per
    Jellyfin user). Only rows whose current filename does NOT already follow the
    ``Title_(YEAR)`` convention are returned.
    """
    directory = _transcoded_dir()
    logger.debug("get_transcoded_movies: scanning directory '%s'", directory)
    seen: set[str] = set()
    rows: list[dict] = []

    all_items = get_transcoded_movie_items()
    logger.debug("get_transcoded_movies: %d item(s) from DB before dedup", len(all_items))

    for item in all_items:
        # unified_file is a root-relative path (e.g. "/Akira_(1988).mkv"); TRANSCODED
        # movies are flat, so reduce to the bare filename for naming + on-disk checks.
        current_file = os.path.basename(item.get("unified_file") or "")
        if not current_file:
            logger.debug("get_transcoded_movies: skipping item '%s' - no unified_file", item.get("Name"))
            continue
        if current_file in seen:
            logger.debug("get_transcoded_movies: skipping '%s' - already seen (duplicate user row)", current_file)
            continue
        seen.add(current_file)

        if is_kodi_named(current_file):
            logger.debug("get_transcoded_movies: '%s' is already kodi-named, skipping", current_file)
            continue  # already correctly named

        title = item.get("Name") or ""
        year = item.get("ProductionYear") or item.get("Year")
        _stem, ext = os.path.splitext(current_file)
        ext = ext.lstrip(".")
        has_metadata = bool(title) and bool(year)
        logger.debug(
            "get_transcoded_movies: '%s' -> title='%s' year=%s has_metadata=%s",
            current_file, title, year, has_metadata,
        )
        proposed = proposed_filename(title, year, ext) if has_metadata else ""
        if not has_metadata:
            logger.debug("get_transcoded_movies: '%s' missing title or year, proposed name will be empty", current_file)

        # Resolve the real on-disk name (handles CIFS-mangled illegal chars).
        real_source = _resolve_source(directory, current_file)
        if real_source is None:
            logger.debug("get_transcoded_movies: '%s' not found on disk", current_file)

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
        if dup:
            logger.debug("get_transcoded_movies: collision flagged for '%s' - proposed '%s' claimed by %d sources", r["current_file"], p, proposed_counts[p])
        elif target_exists:
            logger.debug("get_transcoded_movies: collision flagged for '%s' - proposed '%s' already exists on disk", r["current_file"], p)

    logger.debug("get_transcoded_movies: returning %d misnamed movie(s)", len(rows))
    rows.sort(key=lambda r: r["current_file"].lower())
    return rows


def case_safe_rename(directory: str, src: str, dst: str) -> None:
    """Rename ``src`` to ``dst`` within ``directory``, safe for case-insensitive shares.

    Raises FileNotFoundError if ``src`` is missing, FileExistsError if ``dst`` already
    names a *different* file.
    """
    src_path = os.path.join(directory, src)
    dst_path = os.path.join(directory, dst)
    logger.debug("case_safe_rename: src='%s' dst='%s'", src_path, dst_path)

    if not os.path.isfile(src_path):
        logger.debug("case_safe_rename: source does not exist: '%s'", src_path)
        raise FileNotFoundError(f"Source file does not exist: {src_path}")

    case_only = src != dst and src.lower() == dst.lower()
    logger.debug("case_safe_rename: case_only=%s (src.lower='%s', dst.lower='%s')", case_only, src.lower(), dst.lower())

    # A destination that already exists is only OK when it's the same file we're
    # renaming via a case-only change (CIFS reports the existing entry).
    if os.path.exists(dst_path) and not case_only:
        logger.debug("case_safe_rename: destination already exists and not a case-only rename, aborting")
        raise FileExistsError(f"Destination already exists: {dst_path}")

    if case_only:
        tmp_path = os.path.join(directory, f"{dst}.tmp.{os.getpid()}")
        logger.debug("case_safe_rename: case-only rename via temp path '%s'", tmp_path)
        os.rename(src_path, tmp_path)
        logger.debug("case_safe_rename: step 1/2 done: '%s' -> '%s'", src_path, tmp_path)
        os.rename(tmp_path, dst_path)
        logger.debug("case_safe_rename: step 2/2 done: '%s' -> '%s'", tmp_path, dst_path)
    else:
        os.rename(src_path, dst_path)
        logger.debug("case_safe_rename: direct rename done: '%s' -> '%s'", src_path, dst_path)


def rename_movie(current_file: str, proposed: str) -> tuple[bool, str]:
    """Rename a TRANSCODED movie (and its sidecars) to ``proposed``.

    Returns (ok, message).
    """
    logger.debug("rename_movie: called with current_file='%s' proposed='%s'", current_file, proposed)

    directory = _transcoded_dir()
    if not directory:
        logger.debug("rename_movie: aborting - TRANSCODED directory not configured")
        return False, "TRANSCODED_LOCAL_PATH is not configured."

    # Guard against path traversal; operate on bare filenames only.
    raw_current, raw_proposed = current_file, proposed
    current_file = os.path.basename(current_file or "")
    proposed = os.path.basename(proposed or "")
    if raw_current != current_file:
        logger.debug("rename_movie: path traversal guard stripped current_file: '%s' -> '%s'", raw_current, current_file)
    if raw_proposed != proposed:
        logger.debug("rename_movie: path traversal guard stripped proposed: '%s' -> '%s'", raw_proposed, proposed)
    if not current_file or not proposed:
        logger.debug("rename_movie: aborting - empty filename after sanitization (current='%s', proposed='%s')", current_file, proposed)
        return False, "Both current and proposed filenames are required."

    # Locate the real on-disk file (may differ from Jellyfin's name via CIFS mapping).
    logger.debug("rename_movie: resolving source file '%s' in '%s'", current_file, directory)
    real_source = _resolve_source(directory, current_file)
    if real_source is None:
        logger.debug("rename_movie: aborting - source file not found on disk")
        return False, f"Source file not found on disk: {current_file}"
    logger.debug("rename_movie: resolved source on disk -> '%s'", real_source)

    old_stem = os.path.splitext(real_source)[0]
    new_stem = os.path.splitext(proposed)[0]
    logger.debug("rename_movie: old_stem='%s' new_stem='%s'", old_stem, new_stem)

    sidecars = _find_sidecars(directory, real_source)

    # Rename the video first; abort on failure before touching sidecars.
    logger.debug("rename_movie: renaming video file '%s' -> '%s'", real_source, proposed)
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
        suffix = side[len(old_stem):]
        new_side = new_stem + suffix
        logger.debug("rename_movie: renaming sidecar '%s' -> '%s' (suffix='%s')", side, new_side, suffix)
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
    logger.debug("rename_movie: complete - ok=True, msg='%s'", msg)
    return True, msg
