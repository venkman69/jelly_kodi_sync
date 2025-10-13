import logging
import re
import requests
from typing import Optional
from urllib.parse import urljoin
import json
import os
from pymongo.results import BulkWriteResult
from pymongo import UpdateOne
from mongo_util import get_mongo_collection
from utils import convert_windows_to_unix_path, load_dotenvs


logger = logging.getLogger(__name__)



class JellySession(object):
    def __init__(self, jellyfin_url: str, api_key: str):
        """retrieves a requests session from server"""
        headers = {"X-Emby-Token": api_key}
        self.session = requests.Session()
        self.session.headers.update(headers)
        self.session.get(jellyfin_url)  # Initial request to establish session
        self.jellyfin_url = jellyfin_url
        self.api_key = api_key

    def get(self, endpoint, **kwargs):
        url = urljoin(self.jellyfin_url, endpoint)
        return self.session.get(url, **kwargs)

    def post(self, endpoint, **kwargs):
        url = urljoin(self.jellyfin_url, endpoint)
        return self.session.post(url, **kwargs)


def seconds_to_ticks(seconds: float) -> int:
    """
    Converts seconds to ticks (10,000 ticks per millisecond).

    Args:
        seconds (float): The time in seconds.

    Returns:
        int: The equivalent time in ticks.
    """
    return int(seconds * 10_000_000)

def ticks_to_seconds(ticks: int) -> float:
    """
    Converts ticks to seconds.

    Args:
        ticks (int): The time in ticks.

    Returns:
        float: The equivalent time in seconds.
    """
    return ticks / 10_000_000


def update_playback_position(
    session: JellySession,
    user_id: str,
    item_id: str,
    position_ticks: int,
    is_played: Optional[bool] = None,
    play_count: Optional[int] = None,
) -> bool:
    """
        Updates the playback position for a media item on a Jellyfin/Emby server.

        Args:
            server_url: The base URL of the Jellyfin/Emby server (e.g., "http://localhost:8096").
            api_key: The API key for authentication.
            user_id: The ID of the user whose playback status is being updated.
            item_id: The ID of the media item (movie, episode, etc.).
            position_ticks: The new playback position in 10,000 ticks per millisecond.
            is_played: Optional: True to mark as played, False otherwise.
            play_count: Optional: The new play count.

    # Example: 1 hour, 30 minutes, 15 seconds, and 500 milliseconds (90 minutes)
    # 1 minute = 60,000,000 ticks
    # 1.5 hours = 90 minutes = 90 * 60,000,000 = 5,400,000,000 ticks
    # 15 seconds = 15 * 10,000,000 = 150,000,000 ticks
    # 500 milliseconds = 500 * 10,000 = 5,000,000 ticks
    # Total: 5,555,000,000 ticks

        Returns:
            True if the update was successful (HTTP 204), False otherwise.
    """

    # Construct the API endpoint URL
    url = f"/Users/{user_id}/Items/{item_id}/UserData"

    # Construct the JSON payload (Data Transfer Object)
    # The API endpoint expects a full or partial UserData DTO.
    # PlaybackPositionTicks is the critical field for saving progress.
    payload = {
        "PlaybackPositionTicks": position_ticks,
    }

    if is_played is not None:
        payload["Played"] = is_played

    if play_count is not None:
        payload["PlayCount"] = play_count

    try:
        # Send the POST request to the API
        response = session.post(url, json=payload)

        # The server typically returns an HTTP 204 (No Content) for a successful update
        if response.status_code == 200:
            logger.debug(
                f"Successfully updated playback status for Item {item_id} to {position_ticks} ticks."
            )
            logger.debug(json.dumps(response.json(), indent=2))
            return True
        else:
            logger.debug(
                f"Failed to update playback status. Status Code: {response.status_code}"
            )
            # Try to print error details if available
            try:
                logger.debug("Response content:", response.json())
            except json.JSONDecodeError:
                logger.debug("Response content (text):", response.text)
            return False

    except requests.exceptions.RequestException as e:
        logger.debug(f"An error occurred during the request: {e}")
        return False


def jelly_pull()->bool:
    """
    Fetch all watch status of all items from Jellyfin server and save into mongodb
    """
    jellyfin_url = os.getenv("JELLYFIN_URL")
    api_key = os.getenv("JELLYFIN_API_KEY")
    if not jellyfin_url or not api_key:
        raise ValueError("JELLYFIN_URL and JELLYFIN_API_KEY must be set in environment variables.")
    session = JellySession(jellyfin_url, api_key)

    # Get user id if not provided
    users = get_users(session)
    all_users_items = []
    jellyfin_item_ids = set()
    for user in users:
        logger.debug(f"Processing user ID: {user['Name']}:{user['Id']}")
        user_id = user["Id"]
        # Get all items for the user
        items_url = f"/Users/{user_id}/Items"
        params = {"Recursive": "true", "Fields": "UserData,Path"}
        items_resp = session.get(items_url, params=params)
        items_resp.raise_for_status()
        items = items_resp.json().get("Items", [])
        for item in items:
            logger.debug(f"Processing item: {item['Name']}:{item['Id']}")
            item["UserId"] = user_id
            item["UserName"] = user["Name"]
            # A unique identifier for a user's item is the combination of UserId and the item's Id
            if item.get("Path"):
                item["unified_root"], item["unified_file"] = get_root_file_path(item["Path"])

            jellyfin_item_ids.add(f"{user_id}_{item['Id']}")

        all_users_items.extend(items)
    sync_db(all_users_items, jellyfin_item_ids)
    return True
def get_root_file_path(path:str)->tuple:
    """
    Get the path parsed as RIP, <folder>/<file>
    or TRANSCODED, <file>
    or EPISODIC, <folder>/<season>/<file>
    key is the three root folders

    """
    # jellyfin is running on windows with <nas>/movies/ mounted at M: with RIP|TRANSCODED|EPISODIC under it
    jelly_path_pat = re.compile(os.getenv("JELLY_MOUNT_PAT",""))
    jelly_match = jelly_path_pat.match(path)
    if not jelly_match:
        logger.error(f"Match not found for: {path}")
        return None,None
    if len(jelly_match.groups()) == 3:
        unified_root = jelly_match.groups()[1]
        unified_file = convert_windows_to_unix_path(jelly_match.groups()[2])
        return unified_root, unified_file
    else:
        logger.error(f"Incorrect matches found for: {path}: {jelly_match.groups()}")
        return None, None 
   


def sync_db(all_users_items: list[dict], jellyfin_item_ids: set[str]):
    """
    Synchronizes the fetched Jellyfin items with the MongoDB database.
    It upserts new/updated items and deletes stale items.
    """
    JELLY_COLLECTION = os.getenv("JELLY_COLLECTION", "jellyitems")
    mongo_collection = get_mongo_collection(JELLY_COLLECTION)

    # 1. Upsert all items from Jellyfin into MongoDB
    if all_users_items:
        operations = [
            UpdateOne(
                {"Id": item["Id"], "UserId": item["UserId"]},
                {"$set": item},
                upsert=True
            )
            for item in all_users_items
        ]
        result: BulkWriteResult = mongo_collection.bulk_write(operations)
        if result.upserted_ids is not None:
            logger.debug(f"Upserted items. Matched: {result.matched_count}, Upserted: {len(result.upserted_ids)}, Modified: {result.modified_count}")

    # 2. Delete items from MongoDB that are no longer in Jellyfin
    mongo_items = mongo_collection.find({}, {"_id": 1, "Id": 1, "UserId": 1})
    ids_to_delete = [item["_id"] for item in mongo_items if f"{item['UserId']}_{item['Id']}" not in jellyfin_item_ids]
    if ids_to_delete:
        delete_result = mongo_collection.delete_many({"_id": {"$in": ids_to_delete}})
        logger.debug(f"Deleted {delete_result.deleted_count} stale items from MongoDB.")


def get_users(session) -> list[dict]:
    users_resp = session.get("/Users")
    users_resp.raise_for_status()
    users = users_resp.json()
    if not users:
        raise Exception("No users found on Jellyfin server.")
    return users


def get_items(session: JellySession) -> list[dict]:
    items_url = "/Items"
    items_resp = session.get(items_url)
    items_resp.raise_for_status()
    items = items_resp.json().get("Items", [])
    return items


def get_watched_items_from_mongo():
    JELLY_COLLECTION = os.getenv("JELLY_COLLECTION", "jellyitems")
    # TODO: add option to allow 'all' users and a list of users
    JELLYFIN_SYNC_USER = os.getenv("JELLYFIN_SYNC_USER", "venkman")
    mongo_collection = get_mongo_collection(JELLY_COLLECTION)
    query={"UserName":JELLYFIN_SYNC_USER,"$or": [{"UserData.PlayCount": {"$gt": 0}}, {"UserData.PlaybackPositionTicks": {"$gt": 0}}]}
    result = list(mongo_collection.find(query))
    return result

def sync_watch_status_to_jelly_from_kodi(kodi_item: dict, jelly_item: dict, session: JellySession):
    """
    Using the Jellyfin API, set the playcount and resume position in Jellyfin based on a Kodi item.
    """
    dry_run = os.getenv("DRY_RUN", "false") == "true"

    # Extract watch status from the Kodi item
    kodi_playcount = kodi_item.get("playcount", 0)
    kodi_resume_seconds = kodi_item.get("resume", {}).get("position", 0)

    # Extract current status from Jellyfin item to compare
    jelly_user_data = jelly_item.get("UserData", {})
    jelly_playcount = jelly_user_data.get("PlayCount", 0)
    jelly_resume_ticks = jelly_user_data.get("PlaybackPositionTicks", 0)

    # Convert Kodi seconds to Jellyfin ticks
    new_position_ticks = seconds_to_ticks(kodi_resume_seconds)

    # Check if an update is necessary to avoid redundant API calls
    position_diff_seconds = abs(kodi_resume_seconds - ticks_to_seconds(jelly_resume_ticks))
    if kodi_playcount == jelly_playcount and position_diff_seconds < 2:
        logger.debug(f"Watch status for '{jelly_item.get('Name')}' is already in sync. Skipping.")
        return

    logger.debug(f"Syncing to Jellyfin '{jelly_item.get('Name')}': playcount={kodi_playcount}, resume_ticks={new_position_ticks}")

    if not dry_run:
        update_playback_position(
            session=session,
            user_id=jelly_item["UserId"],
            item_id=jelly_item["Id"],
            position_ticks=new_position_ticks,
            play_count=kodi_playcount,
        )
    else:
        logger.info(f"Dry-Run enabled: setting watch status for '{jelly_item.get('Name')}'.")



if __name__ == "__main__":
    load_dotenvs()
    jellyfin_url = os.getenv("JELLYFIN_URL")
    api_key = os.getenv("JELLYFIN_API_KEY")
    if not jellyfin_url or not api_key:
        raise ValueError("JELLYFIN_URL and JELLYFIN_API_KEY must be set in environment variables.")
    session = JellySession(jellyfin_url, api_key)
    users = get_users(session)
    items = get_items(session)

    jelly_pull() # Removed db_path argument

    for u in users:
        if u["Name"] == "venkman":
            logger.debug(f"Found user venkman with ID {u['Id']}")
            venkat_id = u["Id"]
        if u["Name"] == "anita":
            logger.debug(f"Found user anita with ID {u['Id']}")
            anita_id = u["Id"]

    cra = "9ef0401d9b2ed05107e38a3d30904d51"  # crazy rich asians
    # "UserData": {"PlayedPercentage": 36.725314546682675, "PlaybackPositionTicks": 26586460287,
    #  "PlayCount": 2, "IsFavorite": false, "LastPlayedDate": "2022-01-04T23:49:52.0522607Z", 
    # "Played": false,
    update_playback_position(session, anita_id, cra, 0, True) # mark as played
    ticks = 26586460287  
    update_playback_position(session, anita_id, cra, ticks, False) # mark as in progress with a time index
