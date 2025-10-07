import requests
from typing import Optional
from urllib.parse import urljoin
import json
from dotenv import load_dotenv, find_dotenv
import os
from pymongo.results import BulkWriteResult
from pymongo import UpdateOne
from mongo_util import get_mongo_collection



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


def ticks_from_seconds(seconds: float) -> int:
    """
    Converts seconds to ticks (10,000 ticks per millisecond).

    Args:
        seconds (float): The time in seconds.

    Returns:
        int: The equivalent time in ticks.
    """
    return int(seconds * 10_000_000)


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
            print(
                f"Successfully updated playback status for Item {item_id} to {position_ticks} ticks."
            )
            print(json.dumps(response.json(), indent=2))
            return True
        else:
            print(
                f"Failed to update playback status. Status Code: {response.status_code}"
            )
            # Try to print error details if available
            try:
                print("Response content:", response.json())
            except json.JSONDecodeError:
                print("Response content (text):", response.text)
            return False

    except requests.exceptions.RequestException as e:
        print(f"An error occurred during the request: {e}")
        return False


def jelly_pull(session: JellySession):
    """
    Fetch all watch status of all items from Jellyfin server and save into an sqlite database.
    Args:
        jellyfin_url (str): Base URL of the Jellyfin server (e.g., http://localhost:8096)
        api_key (str): Jellyfin API key
        db_path (str): Path to the sqlite database file
        user_id (Optional[str]): Jellyfin user ID. If not provided, will fetch the first user.
    """

    # Get user id if not provided
    users = get_users(session)
    all_users_items = []
    jellyfin_item_ids = set()
    for user in users:
        print(f"Processing user ID: {user['Name']}:{user['Id']}")
        user_id = user["Id"]
        # Get all items for the user
        items_url = f"/Users/{user_id}/Items"
        params = {"Recursive": "true", "Fields": "UserData,Path"}
        items_resp = session.get(items_url, params=params)
        items_resp.raise_for_status()
        items = items_resp.json().get("Items", [])
        for item in items:
            print(f"Processing item: {item['Name']}:{item['Id']}")
            item["UserId"] = user_id
            item["UserName"] = user["Name"]
            # A unique identifier for a user's item is the combination of UserId and the item's Id
            jellyfin_item_ids.add(f"{user_id}_{item['Id']}")

        all_users_items.extend(items)
    sync_db(all_users_items, jellyfin_item_ids)


def sync_db(all_users_items: list[dict], jellyfin_item_ids: set[str]):
    """
    Synchronizes the fetched Jellyfin items with the MongoDB database.
    It upserts new/updated items and deletes stale items.
    """
    mongo_collection = get_mongo_collection("items")

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
            print(f"Upserted items. Matched: {result.matched_count}, Upserted: {len(result.upserted_ids)}, Modified: {result.modified_count}")

    # 2. Delete items from MongoDB that are no longer in Jellyfin
    mongo_items = mongo_collection.find({}, {"_id": 1, "Id": 1, "UserId": 1})
    ids_to_delete = [item["_id"] for item in mongo_items if f"{item['UserId']}_{item['Id']}" not in jellyfin_item_ids]
    if ids_to_delete:
        delete_result = mongo_collection.delete_many({"_id": {"$in": ids_to_delete}})
        print(f"Deleted {delete_result.deleted_count} stale items from MongoDB.")


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


if __name__ == "__main__":
    dotenvloc = find_dotenv()
    print(f"Loading environment from {dotenvloc}")
    load_dotenv(dotenvloc)
    jellyfin_url = os.getenv("JELLYFIN_URL")
    api_key = os.getenv("JELLYFIN_API_KEY")
    if not jellyfin_url or not api_key:
        raise ValueError("JELLYFIN_URL and JELLYFIN_API_KEY must be set in environment variables.")
    session = JellySession(jellyfin_url, api_key)
    users = get_users(session)
    items = get_items(session)

    jelly_pull(session) # Removed db_path argument

    for u in users:
        if u["Name"] == "venkman":
            print(f"Found user venkman with ID {u['Id']}")
            venkat_id = u["Id"]
        if u["Name"] == "anita":
            print(f"Found user anita with ID {u['Id']}")
            anita_id = u["Id"]

    cra = "9ef0401d9b2ed05107e38a3d30904d51"  # crazy rich asians
    # "UserData": {"PlayedPercentage": 36.725314546682675, "PlaybackPositionTicks": 26586460287,
    #  "PlayCount": 2, "IsFavorite": false, "LastPlayedDate": "2022-01-04T23:49:52.0522607Z", 
    # "Played": false,
    update_playback_position(session, anita_id, cra, 0, True) # mark as played
    ticks = 26586460287  
    update_playback_position(session, anita_id, cra, ticks, False) # mark as in progress with a time index
