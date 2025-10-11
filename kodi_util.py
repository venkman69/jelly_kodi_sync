import re
from kodipydent import Kodi
import logging
import os
from pymongo.results import BulkWriteResult
from pymongo import UpdateOne
import jelly_util
from mongo_util import get_mongo_collection
import utils
from pathlib import Path

logger = logging.getLogger(__name__)


def getKodi() -> Kodi: # type: ignore
    KODIHOST = os.getenv("KODIHOST", "localhost")
    KODIPORT = int(os.getenv("KODIPORT", "8080"))
    KODIUSER = os.getenv("KODIUSER", "kodi")
    KODIPASS = os.getenv("KODIPASS", "1234")
    logger.info(f"Connecting to {KODIHOST} on port: {KODIPORT}")
    mk = Kodi(KODIHOST, port=KODIPORT, username=KODIUSER, password=KODIPASS)
    # with open("kodi_rpc.txt", "w") as f:
        # f.write(str(mk))
    return mk

def kodi_clean():
    mk = getKodi()
    mk.VideoLibrary.Clean()

def kodi_fetch_all_movies():
    mk = getKodi()
    movies = mk.VideoLibrary.GetMovies(properties=
       ["file","title","year","playcount","imdbnumber","resume"])
    if movies and movies.get('result', {}).get('movies'):
        for movie in movies["result"]["movies"]:
            movie["unified_root"], movie["unified_file"] = get_root_file_path(movie["file"])
            movie["uniqueid"] = movie['movieid']
    return movies['result']['movies']

def kodi_movie_details(movie_id:str):
    mk = getKodi()
    movie_detail = mk.VideoLibrary.GetMovieDetails(movie_id, properties=
       ["file","title","year","playcount","imdbnumber","resume"])
    return movie_detail

def kodi_fetch_all_tv_shows():
    mk = getKodi()
    # First, get all TV shows to retrieve their IDs
    tv_shows_result = mk.VideoLibrary.GetTVShows(properties=["title", "year"])
    
    all_episodes = []
    if tv_shows_result and tv_shows_result.get('result', {}).get('tvshows'):
        tv_shows = tv_shows_result['result']['tvshows']
        logger.info(f"Found {len(tv_shows)} TV shows. Fetching episodes for each.")
        
        for show in tv_shows:
            tvshowid = show['tvshowid']
            seasons_query = mk.VideoLibrary.GetSeasons(tvshowid=tvshowid, properties=["season"])
            seasons = seasons_query['result']['seasons']
            
            # For each show, get all its episodes with detailed properties
            for season in seasons:
                episodes_result = mk.VideoLibrary.GetEpisodes(
                    tvshowid=tvshowid, season=season['season'], 
                    properties=["playcount", "resume", "file", "title", "season", "episode"]
                )
                
                if episodes_result and episodes_result.get('result', {}).get('episodes'):
                    episodes = episodes_result['result']['episodes']
                    logger.debug(f"Found {len(episodes)} episodes for '{show['title']}'.")
                    for episode in episodes:
                        episode["unified_root"], episode["unified_file"] = get_root_file_path(episode["file"])
                        episode["uniqueid"] = episode['episodeid']
                        episode["tvshowid"] = show["tvshowid"]
                        episode["tvshowtitle"] = show["title"]
                        episode["tvshowyear"] = show["year"]
                    all_episodes.extend(episodes)

    return all_episodes

def kodi_tv_show_details(tv_show_id:str):
    mk = getKodi()

def get_root_file_path(path:str)->tuple:
    """
    Get the path parsed as RIP, <folder>/<file>
    or TRANSCODED, <file>
    or EPISODIC, <folder>/<season>/<file>
    key is the three root folders

    """
    # jellyfin is running on windows with <nas>/movies/ mounted at M: with RIP|TRANSCODED|EPISODIC under it
    kodi_path_pat = re.compile(os.getenv("KODI_MOUNT_PAT",""))
    kodi_match = kodi_path_pat.match(path)
    if not kodi_match:
        logger.error(f"Match not found for: {path}")
        return None,None
    if len(kodi_match.groups()) == 3:
        unified_root = kodi_match.groups()[1]
        unified_file = utils.convert_windows_to_unix_path(kodi_match.groups()[2])
        return unified_root, unified_file
    else:
        logger.error(f"Incorrect matches found for: {path}: {kodi_match.groups()}")
        return None, None 

def kodi_pull():
    all_movies = kodi_fetch_all_movies()
    all_items=[]
    kodi_item_ids = set()
    for item in all_movies:
        all_items.append(item)
        kodi_item_ids.add(item['uniqueid'])
    
    all_tv_shows = kodi_fetch_all_tv_shows()
    for item in all_tv_shows:
        all_items.append(item)
        kodi_item_ids.add(item['uniqueid'])


    sync_db(all_items,kodi_item_ids)

def sync_db(all_users_items: list[dict], kodi_item_ids: set[str]):
    """
    Synchronizes the fetched Kodi items with the MongoDB database.
    It upserts new/updated items and deletes stale items.
    """
    KODI_COLLECTION = os.getenv("KODI_COLLECTION", "kodiitems")
    mongo_collection = get_mongo_collection(KODI_COLLECTION)

    # 1. Upsert all items from Kodi into MongoDB
    if all_users_items:
        operations = [
            UpdateOne(
                {"uniqueid": item["uniqueid"]},
                {"$set": item},
                upsert=True
            )
            for item in all_users_items
        ]
        result: BulkWriteResult = mongo_collection.bulk_write(operations)
        if result.upserted_ids is not None:
            print(f"Upserted items. Matched: {result.matched_count}, Upserted: {len(result.upserted_ids)}, Modified: {result.modified_count}")

    # 2. Delete items from MongoDB that are no longer in Kodi
    mongo_items = mongo_collection.find({}, {"_id": 1, "uniqueid": 1})
    ids_to_delete = [item["_id"] for item in mongo_items if item["uniqueid"] not in kodi_item_ids]
    if ids_to_delete:
        delete_result = mongo_collection.delete_many({"_id": {"$in": ids_to_delete}})
        logger.info(f"Deleted {delete_result.deleted_count} stale items from Kodi DB.")

def get_watched_items_from_mongo():
    KODI_COLLECTION = os.getenv("KODI_COLLECTION", "kodiitems")
    mongo_collection = get_mongo_collection(KODI_COLLECTION)
    query={"$or": [{"playcount": {"$gt": 0}}, {"resume.position": {"$gt": 0}}]}
    result = list(mongo_collection.find(query))
    return result

def sync_watch_status_in_kodi_from_jelly(jelly_item:dict, kodi_item:dict):
    """using kodi api set the playcount and resume.position in kodi based on jellyfin item"""
    mk = getKodi()
    resume_position = jelly_item["UserData"]["PlaybackPositionTicks"]
    playcount = jelly_item["UserData"]["PlayCount"]
    resume_position_in_seconds = jelly_util.ticks_to_seconds(resume_position)
    if kodi_item["playcount"] == playcount and abs(kodi_item["resume"]["position"] - resume_position_in_seconds) < 1:
        logger.info(f"Watch status for '{kodi_item['title']}' is already in sync. Skipping.")
        return
    if "tvshowid" in kodi_item:
        episode_id = kodi_item["episodeid"]
        mk.VideoLibrary.SetEpisodeDetails(episodeid=episode_id,
            playcount=playcount, resume={"position": resume_position_in_seconds})
    elif "movieid" in kodi_item:
        movie_id = kodi_item["movieid"]
        mk.VideoLibrary.SetMovieDetails(movieid=movie_id,
            playcount=playcount, resume={"position": resume_position_in_seconds})
    else:
        logger.error(f"Unknown item type: {kodi_item}")




if __name__ == "__main__":
    utils.load_dotenvs()
    utils.config_logger("kodi_log.log",Path("./logs"))
    kodi_client = getKodi()
    all_movies = kodi_fetch_all_movies()
    if all_movies and all_movies.get('result', {}).get('movies'):
        for movie in all_movies['result']['movies']:
            if movie.get('resume', {}).get('position', 0) > 0:
                print(movie['title'])
        first_movie_id = all_movies['result']['movies'][0].get("movieid")
        if first_movie_id:
            movie_details = kodi_movie_details(first_movie_id)
            print(movie_details)