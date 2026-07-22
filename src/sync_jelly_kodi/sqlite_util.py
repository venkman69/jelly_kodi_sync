import sqlite3
import json
import logging
import os
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import threading

logger = logging.getLogger(__name__)


class SQLiteDatabase:
    """
    Thread-safe SQLite connection manager with connection pooling
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._db_path = db_path
                cls._instance._local = threading.local()
            return cls._instance

    def get_connection(self) -> sqlite3.Connection:
        """Get thread-local connection"""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=30
            )
            self._local.connection.row_factory = sqlite3.Row
            # Enable foreign keys
            self._local.connection.execute("PRAGMA foreign_keys = ON")
            # Performance optimizations
            self._local.connection.execute("PRAGMA journal_mode = WAL")
            self._local.connection.execute("PRAGMA synchronous = NORMAL")
            self._local.connection.execute("PRAGMA cache_size = -10000")  # 10MB cache
        return self._local.connection

    def close(self):
        """Close thread-local connection"""
        if hasattr(self._local, 'connection') and self._local.connection:
            self._local.connection.close()
            self._local.connection = None


def get_sqlite_connection() -> sqlite3.Connection:
    """
    Get SQLite connection using configuration from environment variables
    """
    db_path = os.getenv("SQLITE_DB_PATH", "./data/jellykodi.db")
    db_dir = Path(db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    db = SQLiteDatabase(db_path)
    conn = db.get_connection()
    initialize_schema(conn)
    return conn


def initialize_schema(conn: sqlite3.Connection):
    """
    Initialize database schema if not exists
    """
    # Jellyfin items table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jellyitems (
            id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL,
            unified_root TEXT,
            unified_file TEXT,
            userdata_json TEXT NOT NULL,
            item_json TEXT NOT NULL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id, user_id)
        )
    """)

    # Kodi items table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kodiitems (
            uniqueid TEXT NOT NULL PRIMARY KEY,
            unified_root TEXT,
            unified_file TEXT,
            playcount INTEGER DEFAULT 0,
            resume_position REAL DEFAULT 0.0,
            item_json TEXT NOT NULL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create indexes if not exist
    indexes = [
        ("idx_jelly_unified_file", "CREATE INDEX IF NOT EXISTS idx_jelly_unified_file ON jellyitems(unified_file)"),
        ("idx_jelly_user_name", "CREATE INDEX IF NOT EXISTS idx_jelly_user_name ON jellyitems(user_name)"),
        ("idx_kodi_unified_file", "CREATE INDEX IF NOT EXISTS idx_kodi_unified_file ON kodiitems(unified_file)"),
        ("idx_kodi_playcount", "CREATE INDEX IF NOT EXISTS idx_kodi_playcount ON kodiitems(playcount)"),
        ("idx_kodi_resume_position", "CREATE INDEX IF NOT EXISTS idx_kodi_resume_position ON kodiitems(resume_position)"),
    ]

    for index_name, sql in indexes:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # Index might exist

    conn.commit()


def upsert_jelly_items(items: List[Dict[str, Any]]) -> Tuple[int, int, int]:
    """
    Upsert Jellyfin items into SQLite.
    Returns: (matched_count, inserted_count, modified_count)
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    matched_count = 0
    inserted_count = 0
    modified_count = 0

    for item in items:
        id_val = item.get("Id")
        user_id = item.get("UserId")
        user_name = item.get("UserName")
        unified_root = item.get("unified_root")
        unified_file = item.get("unified_file")
        userdata = item.get("UserData", {})
        item_json = json.dumps(item)
        userdata_json = json.dumps(userdata)

        # Check if exists
        cursor.execute(
            "SELECT last_updated FROM jellyitems WHERE id = ? AND user_id = ?",
            (id_val, user_id)
        )
        existing = cursor.fetchone()

        if existing:
            matched_count += 1
            # Update if item changed
            cursor.execute(
                """UPDATE jellyitems
                   SET user_name = ?, unified_root = ?, unified_file = ?,
                       userdata_json = ?, item_json = ?, last_updated = CURRENT_TIMESTAMP
                   WHERE id = ? AND user_id = ?""",
                (user_name, unified_root, unified_file, userdata_json, item_json, id_val, user_id)
            )
            if cursor.rowcount > 0:
                modified_count += 1
        else:
            inserted_count += 1
            cursor.execute(
                """INSERT INTO jellyitems
                   (id, user_id, user_name, unified_root, unified_file, userdata_json, item_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (id_val, user_id, user_name, unified_root, unified_file, userdata_json, item_json)
            )

    conn.commit()
    return (matched_count, inserted_count, modified_count)


def upsert_kodi_items(items: List[Dict[str, Any]]) -> Tuple[int, int, int]:
    """
    Upsert Kodi items into SQLite.
    Returns: (matched_count, inserted_count, modified_count)
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    matched_count = 0
    inserted_count = 0
    modified_count = 0

    for item in items:
        uniqueid = item.get("uniqueid")
        unified_root = item.get("unified_root")
        unified_file = item.get("unified_file")
        playcount = item.get("playcount", 0)
        resume_data = item.get("resume", {})
        resume_position = resume_data.get("position", 0.0)
        item_json = json.dumps(item)

        # Check if exists
        cursor.execute(
            "SELECT last_updated FROM kodiitems WHERE uniqueid = ?",
            (uniqueid,)
        )
        existing = cursor.fetchone()

        if existing:
            matched_count += 1
            # Update if item changed
            cursor.execute(
                """UPDATE kodiitems
                   SET unified_root = ?, unified_file = ?, playcount = ?,
                       resume_position = ?, item_json = ?, last_updated = CURRENT_TIMESTAMP
                   WHERE uniqueid = ?""",
                (unified_root, unified_file, playcount, resume_position, item_json, uniqueid)
            )
            if cursor.rowcount > 0:
                modified_count += 1
        else:
            inserted_count += 1
            cursor.execute(
                """INSERT INTO kodiitems
                   (uniqueid, unified_root, unified_file, playcount, resume_position, item_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (uniqueid, unified_root, unified_file, playcount, resume_position, item_json)
            )

    conn.commit()
    return (matched_count, inserted_count, modified_count)


def get_watched_jelly_items(user_name: str = None) -> List[Dict[str, Any]]:
    """
    Get watched items from jellyitems (playcount > 0 OR playback position > 0)
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    # Query with OR logic equivalent to MongoDB $or
    if user_name:
        cursor.execute("""
            SELECT item_json FROM jellyitems
            WHERE user_name = ? AND (json_extract(userdata_json, '$.PlayCount') > 0
                                     OR json_extract(userdata_json, '$.PlaybackPositionTicks') > 0)
        """, (user_name,))
    else:
        cursor.execute("""
            SELECT item_json FROM jellyitems
            WHERE json_extract(userdata_json, '$.PlayCount') > 0
               OR json_extract(userdata_json, '$.PlaybackPositionTicks') > 0
        """)

    return [json.loads(row[0]) for row in cursor.fetchall()]


def get_watched_kodi_items() -> List[Dict[str, Any]]:
    """
    Get watched items from kodiitems (playcount > 0 OR resume position > 0)
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT item_json FROM kodiitems
        WHERE playcount > 0 OR resume_position > 0
    """)

    results = [json.loads(row[0]) for row in cursor.fetchall()]
    return results


def find_kodi_items_by_file(file_path: str) -> List[Dict[str, Any]]:
    """
    Find Kodi items by unified_file path
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT item_json FROM kodiitems WHERE unified_file = ?",
        (file_path,)
    )

    return [json.loads(row[0]) for row in cursor.fetchall()]


def find_jelly_items_by_file(file_path: str) -> List[Dict[str, Any]]:
    """
    Find Jellyfin items by unified_file path
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT item_json FROM jellyitems WHERE unified_file = ?",
        (file_path,)
    )

    return [json.loads(row[0]) for row in cursor.fetchall()]


def get_transcoded_movie_items() -> List[Dict[str, Any]]:
    """
    Get Jellyfin items that are Movies living under the TRANSCODED root.
    Used by the movie-rename UI to find files that may need renaming.
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT item_json FROM jellyitems
        WHERE unified_root = 'TRANSCODED'
          AND json_extract(item_json, '$.Type') = 'Movie'
    """)

    return [json.loads(row[0]) for row in cursor.fetchall()]


def get_all_jelly_item_ids() -> List[Tuple[str, str]]:
    """
    Get all Jellyfin item (id, user_id) pairs for stale item detection
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id, user_id FROM jellyitems")
    return cursor.fetchall()


def get_all_kodi_item_ids() -> List[str]:
    """
    Get all Kodi uniqueid values for stale item detection
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT uniqueid FROM kodiitems")
    return [row[0] for row in cursor.fetchall()]


def delete_stale_jelly_items(existing_ids: List[Tuple[str, str]]) -> int:
    """
    Delete Jellyfin items that are not in the provided list of (id, user_id) pairs
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    if not existing_ids:
        # Delete all if empty list
        cursor.execute("DELETE FROM jellyitems")
    else:
        # Build IN clause with placeholders
        placeholders = ','.join(['(?,?)'] * len(existing_ids))
        flat_ids = [item for pair in existing_ids for item in pair]
        cursor.execute(
            f"DELETE FROM jellyitems WHERE (id, user_id) NOT IN ({placeholders})",
            flat_ids
        )

    deleted_count = cursor.rowcount
    conn.commit()
    return deleted_count


def delete_stale_kodi_items(existing_ids: List[str]) -> int:
    """
    Delete Kodi items that are not in the provided list of uniqueid values
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    if not existing_ids:
        # Delete all if empty list
        cursor.execute("DELETE FROM kodiitems")
    else:
        placeholders = ','.join(['?'] * len(existing_ids))
        cursor.execute(
            f"DELETE FROM kodiitems WHERE uniqueid NOT IN ({placeholders})",
            existing_ids
        )

    deleted_count = cursor.rowcount
    conn.commit()
    return deleted_count


def delete_all_items(table_name: str = "jellyitems") -> int:
    """
    Delete all items from specified table
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    valid_tables = ["jellyitems", "kodiitems"]
    if table_name not in valid_tables:
        raise ValueError(f"Invalid table name: {table_name}")

    cursor.execute(f"DELETE FROM {table_name}")
    deleted_count = cursor.rowcount
    conn.commit()
    return deleted_count


def get_all_items(table_name: str = "jellyitems") -> List[Dict[str, Any]]:
    """
    Read all items from specified table
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()

    valid_tables = ["jellyitems", "kodiitems"]
    if table_name not in valid_tables:
        raise ValueError(f"Invalid table name: {table_name}")

    cursor.execute(f"SELECT item_json FROM {table_name}")
    return [json.loads(row[0]) for row in cursor.fetchall()]


if __name__ == "__main__":
    # Example usage
    import os
    from .utils import load_dotenvs, config_logger
    from pathlib import Path

    load_dotenvs()
    logdir = Path(os.getenv("LOG_DIR", "./logs"))
    logfile = os.getenv("LOG_FILE", "sqlite_util.log")
    log_file_path = logdir / logfile
    logdir.mkdir(parents=True, exist_ok=True)
    config_logger(logfile, logdir)

    # Test connection
    conn = get_sqlite_connection()
    logger.info(f"SQLite connection established at: {os.getenv('SQLITE_DB_PATH', './data/jellykodi.db')}")

    # Test inserting a Jellyfin item
    test_item = {
        "Id": "test123",
        "UserId": "user123",
        "UserName": "testuser",
        "unified_root": "RIP",
        "unified_file": "test/movie.mkv",
        "UserData": {"PlayCount": 1, "PlaybackPositionTicks": 0}
    }

    matched, inserted, modified = upsert_jelly_items([test_item])
    logger.info(f"Upsert result: matched={matched}, inserted={inserted}, modified={modified}")

    # Test retrieving watched items
    watched = get_watched_jelly_items("testuser")
    logger.info(f"Found {len(watched)} watched items")
