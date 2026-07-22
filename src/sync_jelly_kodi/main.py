import logging
import os
from .jelly_util import JellySession, jelly_pull
from . import jelly_util
from .kodi_util import kodi_pull
from . import kodi_util
from .sqlite_util import find_kodi_items_by_file, find_jelly_items_by_file
from . import utils
from pathlib import Path
from datetime import datetime
import typer


utils.load_dotenvs()
log_dir = os.getenv("LOG_DIR", "./logs")
log_file = os.getenv("LOG_FILE", "jelly_kodi_sync.log")
utils.config_logger(log_file, Path(log_dir))

app = typer.Typer()


logger = logging.getLogger(__name__)


def set_watch_from_jelly_to_kodi(jelly_watched_items:list[dict]):
    found_counter=0
    for item in jelly_watched_items:
        # find this item from db
        file_location = item["unified_file"]
        found_items = find_kodi_items_by_file(file_location)
        if len(found_items) > 1:
            logger.warning("More than one match")
            found_counter+=1
            for found_item in found_items:
                logger.debug(f"Found: {file_location}")
        elif len(found_items) == 1:
            logger.debug(f"Found: {file_location}")
            found_counter+=1
            kodi_util.sync_watch_status_from_jelly_to_kodi(item, found_items[0])
        else:
            logger.debug(f"No match found: {file_location}")
    logger.info(f"Found {found_counter} Kodi items out of {len(jelly_watched_items)} JellyFin items in kodi")

def set_watch_from_kodi_to_jelly(kodi_watched_items:list[dict]):
    jellyfin_url = os.getenv("JELLYFIN_URL")
    api_key = os.getenv("JELLYFIN_API_KEY")
    if not jellyfin_url or not api_key:
        raise ValueError("JELLYFIN_URL and JELLYFIN_API_KEY must be set in environment variables.")
    session = JellySession(jellyfin_url, api_key)
    found_counter=0
    for item in kodi_watched_items:
        # find this item from db
        file_location = item.get("unified_file")
        if not file_location:
            logger.warning(f"Kodi item '{item.get('title')}' is missing 'unified_file', skipping.")
            continue

        found_items = find_jelly_items_by_file(file_location)

        if found_items:
            logger.debug(f"Found {len(found_items)} match(es) in Jellyfin for Kodi item: {file_location}")
            found_counter += len(found_items)
            # A single Kodi item can match against multiple Jellyfin users' libraries. Sync all.
            for found_item in found_items:
                jelly_util.sync_watch_status_from_kodi_to_jelly(item, found_item, session)
        else:
            logger.debug(f"No Jellyfin match found for Kodi item: {file_location}")

    logger.info(f"Found {found_counter} Jellyfin items out of {len(kodi_watched_items)} Kodi items in JellyFin.")


@app.command()
def pull_jelly():
    jellyfin_url = os.getenv("JELLYFIN_URL")
    api_key = os.getenv("JELLYFIN_API_KEY")
    if not jellyfin_url or not api_key:
        raise ValueError("JELLYFIN_URL and JELLYFIN_API_KEY must be set in environment variables.")
    logger.info(f"Starting jellyfin data pull at {datetime.now()}")
    jelly_pull()
    logger.info("Jellyfin data pull complete")


@app.command()
def pull_kodi():
    logger.info(f"Starting kodi data pull at {datetime.now()}")
    try:
        kodi_conn = kodi_util.getKodi()
        logger.info("Kodi is up")
    except Exception as e:
        logger.error(f"Kodi is down: {e}")
        logger.info("Exiting as Kodi is unavailable")
        exit(1)
    kodi_pull()
    logger.info("Kodi data pull complete")


@app.command()
def sync():
    # - get data
    # run sync with jelly first
    # run kodi sync
    # - sync kodi watch into jelly
    # find items in kodi that have playcount
    # find jelly items that match and update the playcount
    # find items in kodi that have resume.position >0
    # find jelly items that match and update the position using ticks
    # -- sync jelly watch into kodi
    # do the same steps in reverse
    logger.info("Preflight check - is Kodi up...")
    try:
        kodi_conn = kodi_util.getKodi()
        logger.info("Kodi is up")
    except Exception as e:
        logger.error(f"Kodi is down: {e}")
        logger.info("Exiting as Kodi is unavailable")
        exit(1)

    logger.info(f"Starting sync at {datetime.now()}")
    start_time = datetime.now()
    logger.info(f"Starting sync at {start_time}")

    step_start_time = datetime.now()
    logger.info("Step 1/8: get jelly items")
    jelly_pull()
    step_end_time = datetime.now()
    logger.info(f"Step 1/8 completed in {step_end_time - step_start_time}")

    step_start_time = datetime.now()
    logger.info("Step 2/8: Find jelly watched items")
    jelly_watched = jelly_util.get_watched_items_from_db()
    logger.info(f"Found {len(jelly_watched)} jelly watched items")
    step_end_time = datetime.now()
    logger.info(f"Step 2/8 completed in {step_end_time - step_start_time}")

    step_start_time = datetime.now()
    logger.info("Step 3/8: Sync jelly watch into kodi")
    set_watch_from_jelly_to_kodi(jelly_watched)
    step_end_time = datetime.now()
    logger.info(f"Step 3/8 completed in {step_end_time - step_start_time}")

    step_start_time = datetime.now()
    logger.info("Step 4/8: get kodi items")
    kodi_pull()
    step_end_time = datetime.now()
    logger.info(f"Step 4/8 completed in {step_end_time - step_start_time}")

    step_start_time = datetime.now()
    logger.info("Step 5/8: Find kodi watched items")
    kodi_watched = kodi_util.get_watched_items_from_db()
    logger.info(f"Found {len(kodi_watched)} kodi watched items")
    step_end_time = datetime.now()
    logger.info(f"Step 5/8 completed in {step_end_time - step_start_time}")

    logger.info("Step 6/8: Sync kodi watch into jelly")
    step_start_time = datetime.now()
    set_watch_from_kodi_to_jelly(kodi_watched)
    step_end_time = datetime.now()
    logger.info(f"Step 6/8 completed in {step_end_time - step_start_time}")

    logger.info("step 7/8 resync jelly items")
    step_start_time = datetime.now()
    jelly_pull()
    step_end_time = datetime.now()
    logger.info(f"Step 7/8 completed in {step_end_time - step_start_time}")

    logger.info("step 8/8 resync kodi items")
    step_start_time = datetime.now()
    kodi_pull()
    step_end_time = datetime.now()
    logger.info(f"Step 8/8 completed in {step_end_time - step_start_time}")

    logger.info("Done")


@app.command()
def web(host: str = "127.0.0.1", port: int = 5001):
    """Launch the FastHTML UI to rename misnamed TRANSCODED movies (auto-reloads on change)."""
    from .web import serve
    serve(host, port)


if __name__ == "__main__":
    app()