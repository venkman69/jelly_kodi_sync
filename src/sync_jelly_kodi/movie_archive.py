"""Archive watched TRANSCODED movies to the ARCHIVE directory.

A movie qualifies when PlayCount > 0 for at least one Jellyfin user OR Kodi
playcount > 0. Only kodi-named files (Title_(Year).ext) are archived; others are
flagged so the user can rename them first via the Movie Renamer tab.

Archive layout: ARCHIVE/<Title_(Year)>/<Title_(Year).ext>
Sidecars (subtitles, .nfo, artwork) are moved alongside the video.
"""
import logging
import os
import shutil

from .movie_rename import _find_sidecars, _normalize_illegal, _resolve_source
from .naming import is_kodi_named
from .sqlite_util import delete_jelly_items_by_file, find_kodi_items_by_file, get_sqlite_connection

logger = logging.getLogger(__name__)


def _archive_dir() -> str:
    return os.getenv("ARCHIVE", "")


def _transcoded_dir() -> str:
    return os.getenv("TRANSCODED_LOCAL_PATH") or os.getenv("TRANSCODED", "")


def get_watched_transcoded_movies() -> list[dict]:
    """Return fully-watched TRANSCODED movies, deduped by unified_file.

    Checks Jellyfin (PlayCount > 0 for any user) and Kodi (playcount > 0) — a
    movie qualifies if either source reports it as watched. Non-kodi-named files
    are included but flagged with ``needs_rename=True``.
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    # One representative row per unified_file from TRANSCODED Movies.
    cursor.execute("""
        SELECT unified_file,
               MAX(json_extract(item_json, '$.Name'))           AS name,
               MAX(json_extract(item_json, '$.ProductionYear')) AS year
        FROM jellyitems
        WHERE unified_root = 'TRANSCODED'
          AND json_extract(item_json, '$.Type') = 'Movie'
        GROUP BY unified_file
    """)
    all_transcoded = cursor.fetchall()

    directory = _transcoded_dir()
    rows: list[dict] = []

    for row in all_transcoded:
        unified_file = row[0]
        if not unified_file:
            continue
        current_file = os.path.basename(unified_file)
        if not current_file:
            continue

        title = row[1] or ""
        year = row[2]

        # Jellyfin: any user has fully watched (PlayCount > 0)
        cursor.execute("""
            SELECT COUNT(*) FROM jellyitems
            WHERE unified_file = ?
              AND json_extract(userdata_json, '$.PlayCount') > 0
        """, (unified_file,))
        jelly_played = cursor.fetchone()[0] > 0

        # Jellyfin: any user still has an in-progress position
        cursor.execute("""
            SELECT COUNT(*) FROM jellyitems
            WHERE unified_file = ?
              AND json_extract(userdata_json, '$.PlaybackPositionTicks') > 0
        """, (unified_file,))
        jelly_in_progress = cursor.fetchone()[0] > 0

        # Kodi: playcount > 0 and no active resume position
        kodi_items = find_kodi_items_by_file(unified_file)
        kodi_played = any(k.get("playcount", 0) > 0 for k in kodi_items)
        kodi_in_progress = any(
            k.get("resume", {}).get("position", 0.0) > 0 for k in kodi_items
        )

        # A non-zero resume position anywhere means the movie is still in progress —
        # do not propose archiving even if PlayCount > 0.
        any_in_progress = jelly_in_progress or kodi_in_progress
        any_played = jelly_played or kodi_played
        if not any_played or any_in_progress:
            continue

        jelly_watched = jelly_played and not jelly_in_progress
        kodi_watched = kodi_played and not kodi_in_progress

        _, ext = os.path.splitext(current_file)
        ext = ext.lstrip(".")
        real_source = _resolve_source(directory, current_file) if directory else None

        rows.append({
            "current_file": current_file,
            "unified_file": unified_file,
            "title": title,
            "year": year,
            "ext": ext,
            "needs_rename": not is_kodi_named(current_file),
            "exists_on_disk": real_source is not None,
            "jelly_watched": jelly_watched,
            "kodi_watched": kodi_watched,
        })

    rows.sort(key=lambda r: r["current_file"].lower())
    logger.debug("get_watched_transcoded_movies: %d result(s)", len(rows))
    return rows


def archive_movie(current_file: str) -> list[dict]:
    """Archive a TRANSCODED movie (+ sidecars) to ARCHIVE/<Title_(Year)>/.

    Returns a list of step dicts: {label, ok, detail, current_state}.
    Stops after any critical step failure. Sidecar failures are recorded
    individually but don't halt the remaining sidecars or DB cleanup.
    """
    steps: list[dict] = []

    def record(label: str, ok: bool, detail: str, current_state: str = "") -> bool:
        entry = {"label": label, "ok": ok, "detail": detail, "current_state": current_state}
        steps.append(entry)
        logger.log(logging.INFO if ok else logging.WARNING,
                   "archive step '%s': ok=%s — %s", label, ok, detail)
        return ok

    archive_root = _archive_dir()
    transcoded_dir = _transcoded_dir()
    current_file = os.path.basename(current_file or "")

    # 1. Validate source on disk
    if not transcoded_dir:
        record("Validate source", False, "TRANSCODED is not configured.")
        return steps
    real_source = _resolve_source(transcoded_dir, current_file)
    if real_source is None:
        record("Validate source", False, f"'{current_file}' not found on disk.",
               f"expected: {os.path.join(transcoded_dir, current_file)}")
        return steps
    record("Validate source", True, f"Found '{real_source}' in TRANSCODED.")

    # 2. Compute target path — requires kodi-named source
    if not is_kodi_named(real_source):
        record("Compute target path", False,
               f"'{real_source}' is not kodi-named. Rename it first via the Movie Renamer tab.",
               f"source: {os.path.join(transcoded_dir, real_source)}")
        return steps
    if not archive_root:
        record("Compute target path", False, "ARCHIVE is not configured.")
        return steps
    stem = os.path.splitext(real_source)[0]
    dir_name = _normalize_illegal(stem)   # safe for CIFS archive mount
    target_dir = os.path.join(archive_root, dir_name)
    target_file = os.path.join(target_dir, real_source)
    record("Compute target path", True, f"→ {target_file}")

    # 3. Check the target file doesn't already exist
    if os.path.isfile(target_file):
        record("Check target is clear", False,
               f"Target already exists: {target_file}",
               f"existing: {target_file}")
        return steps
    record("Check target is clear", True, "Target path is available.")

    # 4. Verify ARCHIVE root is mounted / accessible
    if not os.path.isdir(archive_root):
        record("Verify ARCHIVE root", False,
               f"ARCHIVE directory not found (mount missing?): {archive_root}",
               f"ARCHIVE: {archive_root}")
        return steps
    record("Verify ARCHIVE root", True, f"Accessible: {archive_root}")

    # Collect sidecars before touching the filesystem.
    sidecars = _find_sidecars(transcoded_dir, real_source)

    # 5. Create target directory (plain mkdir catches pre-existing collisions)
    try:
        os.mkdir(target_dir)
        record("Create target directory", True, f"Created '{target_dir}'")
    except FileExistsError:
        # Directory exists but the target file doesn't — safe to reuse (prior failed attempt).
        record("Create target directory", True, f"Directory already exists (reusing): '{target_dir}'")
    except OSError as e:
        record("Create target directory", False, f"mkdir failed: {e}",
               f"target: {target_dir}")
        return steps

    # 6. Move video file
    source_path = os.path.join(transcoded_dir, real_source)
    try:
        shutil.move(source_path, target_file)
        record("Move video", True, f"'{real_source}' → archive")
    except OSError as e:
        record("Move video", False, f"shutil.move failed: {e}",
               f"source: {source_path}; target dir created at: {target_dir}")
        return steps

    # 7. Move sidecars (non-fatal per file; all are attempted regardless)
    for sidecar in sidecars:
        sidecar_src = os.path.join(transcoded_dir, sidecar)
        sidecar_dst = os.path.join(target_dir, sidecar)
        try:
            shutil.move(sidecar_src, sidecar_dst)
            record(f"Move sidecar '{sidecar}'", True, "Moved to archive directory.")
        except OSError as e:
            record(f"Move sidecar '{sidecar}'", False, f"Move failed: {e}",
                   f"sidecar still at: {sidecar_src}; video is at: {target_file}")

    # 8. Remove from jellyitems DB cache so the file doesn't reappear on next load
    unified_file = f"/{real_source}"
    removed = delete_jelly_items_by_file(unified_file)
    record("Remove from Jellyfin cache", True, f"Removed {removed} cached row(s) for '{unified_file}'.")

    return steps
