from pathlib import Path
import logging
import os

from dotenv import load_dotenv
def config_logger(log_file_name:str, log_file_dir:Path):
    log_file_dir.mkdir(parents=True,exist_ok=True)
    log_file_path = log_file_dir / log_file_name
    logging.basicConfig(
        level=logging.DEBUG,
        format= "%(asctime)s %(levelname)s %(name)s:%(funcName)s():%(lineno)i %(message)s",
        handlers=[
            logging.FileHandler(log_file_path)
        ]
    )
    return logging.getLogger(__name__)

def load_dotenvs():
    load_dotenv()
    load_dotenv(".credentials")
    

if __name__ == "__main__":
    print("before",os.getenv("KODIUSER"))
    load_dotenvs()
    print("after",os.getenv("KODIUSER"))