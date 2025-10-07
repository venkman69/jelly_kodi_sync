import os
from pathlib import Path
import pymongo
from pymongo.collection import Collection
from pymongo.errors import ConnectionFailure
import subprocess

from utils import config_logger, load_dotenvs

def start_mongodb():
    """
    Start MongoDB server if not running.
    """
    # run bin/run_mongo using subprocess
    logging.info("Attempting to start MongoDB server...")
    try:
        result = subprocess.run(["./common_infra/mongo_podman", "start"], check=True, capture_output=True, text=True)
        if result.returncode == 0:
            logging.info("MongoDB server started successfully.")
        else:
            logging.error(f"Failed to start MongoDB server: {result.stderr}")
            raise RuntimeError("Failed to start MongoDB server.")
    except Exception as e:
        logging.error(f"Error starting MongoDB server: {e}")
        raise e

def get_mongo_connection():
    """
    Get a MongoDB connection using the configuration using dotenv
    """
    db_connection_host = os.getenv("MONGO_HOST","localhost")
    db_connection_port = int(os.getenv("MONGO_PORT", "27017"))
    
    # Set a 5-second timeout for server selection
    client = pymongo.MongoClient(db_connection_host, db_connection_port)
    try:
        print("Checking of mongo is up with timeout of 5 seconds")
        # Check if the connection is successful with a 5-second timeout for the ping command
        with pymongo.timeout(5):
            client.admin.command('ping')
        logging.info(f"Connected to MongoDB at {db_connection_host}:{db_connection_port}")
    except ConnectionFailure as e:
        logging.error(f"Failed to connect to MongoDB at {db_connection_host}:{db_connection_port}: {e}")
        # attempt to start mongodb
        start_mongodb()

    return client

    

def get_mongo_collection(db_collection_name:str="items")->Collection:
    client = get_mongo_connection()
    db_name = os.getenv("MONGO_DB_NAME", "jellykodi")
    db = client[db_name]
    collection = db[db_collection_name]
    # check if the connection is successful
    return collection


def write_to_mongo(items, db_collection_name:str="items"):
    """
    Write items to MongoDB using Mongita.
    """
    db_collection = get_mongo_collection(db_collection_name)
    # Insert new items
    if items:
        db_collection.insert_many(items)
        logging.info(f"Inserted {len(items)} items into MongoDB ")
    else:
        logging.warning("No items to insert into MongoDB.")

def read_from_mongo(db_collection_name:str="items"):
    """
    Read items from MongoDB
    """
    collection = get_mongo_collection(db_collection_name)

    items = list(collection.find())
    logging.info(f"Retrieved {len(items)} items from MongoDB")
    return items

def delete_all_items(db_collection_name:str="items"):
    """
    Delete all items from MongoDB.
    """
    collection = get_mongo_collection(db_collection_name)
    result = collection.delete_many({})
    logging.info(f"Deleted {result.deleted_count} items from MongoDB")
    return result.deleted_count

if __name__ == "__main__":
    # Example usage
    print(os.getcwd())
    if os.path.exists(".env"):
        print("Loading environment variables from .env file")
    
    print(f"Checking before loading: LOG_DIR: {os.getenv('LOG_DIR')}")
    load_dotenvs()
    print(f"Checking after loading: LOG_DIR: {os.getenv('LOG_DIR')}")
    logdir = Path(os.getenv("LOG_DIR", "./logs"))
    logfile = os.getenv("LOG_FILE", "jelly_kodi_sync.log")
    log_file_path = logdir / logfile
    logdir.mkdir(parents=True, exist_ok=True)
    logging = config_logger(logfile, logdir)
    item_collection = get_mongo_collection("items")

    
