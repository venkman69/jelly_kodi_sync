"""Shared Kodi<->Jellyfin watch-status sync operations.

These were originally inline in ``main.py``'s ``sync`` command. They live here so
both the CLI (``main.sync``) and the web UI (``web.py`` "Jelly-Kodi Sync" tab) run
the exact same logic.

Each ``*_step`` wrapper performs one leg of the sync and returns ``(ok, message)``
so callers can report progress uniformly -- log lines in the CLI, green/red tick
rows in the UI. The web tab runs these sequentially: each step is a separate HTMX
request that only fires after the previous one succeeds.
"""
import logging
import os

from . import jelly_util, kodi_util
from .jelly_util import JellySession, jelly_pull, jelly_library_refresh
from .kodi_util import getKodi, kodi_pull, kodi_library_scan
from .sqlite_util import find_jelly_items_by_file, find_kodi_items_by_file

logger = logging.getLogger(__name__)


def set_watch_from_jelly_to_kodi(jelly_watched_items: list[dict]) -> tuple[int, int]:
    """Push each watched Jellyfin item's status onto its matching Kodi item.

    Returns ``(matched, total)`` -- how many Jellyfin items had a Kodi match.
    """
    found_counter = 0
    for item in jelly_watched_items:
        file_location = item["unified_file"]
        found_items = find_kodi_items_by_file(file_location)
        if len(found_items) > 1:
            logger.warning("More than one match")
            found_counter += 1
            for found_item in found_items:
                logger.debug(f"Found: {file_location}")
        elif len(found_items) == 1:
            logger.debug(f"Found: {file_location}")
            found_counter += 1
            kodi_util.sync_watch_status_from_jelly_to_kodi(item, found_items[0])
        else:
            logger.debug(f"No match found: {file_location}")
    logger.info(
        f"Found {found_counter} Kodi items out of {len(jelly_watched_items)} JellyFin items in kodi"
    )
    return found_counter, len(jelly_watched_items)


def set_watch_from_kodi_to_jelly(kodi_watched_items: list[dict]) -> tuple[int, int]:
    """Push each watched Kodi item's status onto its matching Jellyfin item(s).

    Returns ``(matched, total)`` -- how many Jellyfin item rows were updated.
    """
    jellyfin_url = os.getenv("JELLYFIN_URL")
    api_key = os.getenv("JELLYFIN_API_KEY")
    if not jellyfin_url or not api_key:
        raise ValueError(
            "JELLYFIN_URL and JELLYFIN_API_KEY must be set in environment variables."
        )
    session = JellySession(jellyfin_url, api_key)
    found_counter = 0
    for item in kodi_watched_items:
        file_location = item.get("unified_file")
        if not file_location:
            logger.warning(
                f"Kodi item '{item.get('title')}' is missing 'unified_file', skipping."
            )
            continue

        found_items = find_jelly_items_by_file(file_location)

        if found_items:
            logger.debug(
                f"Found {len(found_items)} match(es) in Jellyfin for Kodi item: {file_location}"
            )
            found_counter += len(found_items)
            # A single Kodi item can match multiple Jellyfin users' libraries. Sync all.
            for found_item in found_items:
                jelly_util.sync_watch_status_from_kodi_to_jelly(item, found_item, session)
        else:
            logger.debug(f"No Jellyfin match found for Kodi item: {file_location}")

    logger.info(
        f"Found {found_counter} Jellyfin items out of {len(kodi_watched_items)} Kodi items in JellyFin."
    )
    return found_counter, len(kodi_watched_items)


# --- Step wrappers: each returns (ok, message) -----------------------------------


def kodi_library_scan_step() -> tuple[bool, str]:
    try:
        kodi_library_scan()
        return True, "Kodi library scan triggered"
    except Exception as e:  # noqa: BLE001
        logger.exception("Kodi library scan failed")
        return False, f"Kodi library scan failed: {e}"


def jelly_library_refresh_step() -> tuple[bool, str]:
    try:
        ok = jelly_library_refresh()
        if ok:
            return True, "Jellyfin library refresh triggered"
        return False, "Jellyfin library refresh returned unexpected response"
    except Exception as e:  # noqa: BLE001
        logger.exception("Jellyfin library refresh failed")
        return False, f"Jellyfin library refresh failed: {e}"


def preflight_kodi_step() -> tuple[bool, str]:
    try:
        getKodi()
        return True, "Kodi is reachable"
    except Exception as e:  # noqa: BLE001 - report any connectivity failure
        logger.error("Kodi preflight failed: %s", e)
        return False, f"Kodi is unavailable: {e}"


def pull_jelly_step() -> tuple[bool, str]:
    try:
        jelly_pull()
        return True, "Pulled items from Jellyfin"
    except Exception as e:  # noqa: BLE001
        logger.exception("Jellyfin pull failed")
        return False, f"Jellyfin pull failed: {e}"


def pull_kodi_step() -> tuple[bool, str]:
    try:
        kodi_pull()
        return True, "Pulled items from Kodi"
    except Exception as e:  # noqa: BLE001
        logger.exception("Kodi pull failed")
        return False, f"Kodi pull failed: {e}"


def push_jelly_to_kodi_step() -> tuple[bool, str]:
    try:
        watched = jelly_util.get_watched_items_from_db()
        matched, total = set_watch_from_jelly_to_kodi(watched)
        return True, f"Pushed Jellyfin watch status to Kodi ({matched}/{total} matched)"
    except Exception as e:  # noqa: BLE001
        logger.exception("Push Jellyfin -> Kodi failed")
        return False, f"Push Jellyfin → Kodi failed: {e}"


def push_kodi_to_jelly_step() -> tuple[bool, str]:
    try:
        watched = kodi_util.get_watched_items_from_db()
        matched, total = set_watch_from_kodi_to_jelly(watched)
        return True, f"Pushed Kodi watch status to Jellyfin ({matched}/{total} matched)"
    except Exception as e:  # noqa: BLE001
        logger.exception("Push Kodi -> Jellyfin failed")
        return False, f"Push Kodi → Jellyfin failed: {e}"


# Ordered auto-sync sequence, mirroring the CLI ``sync`` command's 8 steps
# (the "find watched items" reads are folded into the push steps that consume them).
AUTO_STEPS: list[tuple[str, callable]] = [
    ("Preflight: check Kodi is reachable", preflight_kodi_step),
    ("Pull from Jellyfin", pull_jelly_step),
    ("Push Jellyfin → Kodi", push_jelly_to_kodi_step),
    ("Pull from Kodi", pull_kodi_step),
    ("Push Kodi → Jellyfin", push_kodi_to_jelly_step),
    ("Re-pull Jellyfin", pull_jelly_step),
    ("Re-pull Kodi", pull_kodi_step),
]
