from kodipydent import Kodi
import logging
import os
from dotenv import load_dotenv
import utils
from pathlib import Path


def getKodi(logger:logging.Logger) -> Kodi: # type: ignore
    KODIHOST = os.getenv("KODIHOST", "localhost")
    KODIPORT = int(os.getenv("KODIPORT", "8080"))
    KODIUSER = os.getenv("KODIUSER", "kodi")
    KODIPASS = os.getenv("KODIPASS", "1234")
    logger.info(f"Connecting to {KODIHOST} on port: {KODIPORT}")
    mk = Kodi(KODIHOST, port=KODIPORT, username=KODIUSER, password=KODIPASS)
    # with open("kodi_rpc.txt", "w") as f:
        # f.write(str(mk))
    return mk

def kodi_clean(logger:logging.Logger):
    mk = getKodi(logger)
    mk.VideoLibrary.Clean()

def kodi_fetch_all_movies(logger:logging.Logger):
    mk = getKodi(logger)
    movies = mk.VideoLibrary.GetMovies()
    return movies

def kodi_movie_details(logger:logging.Logger, movie_id:str):
    mk = getKodi(logger)
    movie_detail = mk.VideoLibrary.GetMovieDetails(movie_id, properties=
       ["file","title","year","playcount","imdbnumber"])
    return movie_detail


if __name__ == "__main__":
    load_dotenv()
    logger = utils.config_logger("kodi_log.log",Path("./logs"))
    x=getKodi(logger)
    xx = kodi_fetch_all_movies(logger)
    print(xx)