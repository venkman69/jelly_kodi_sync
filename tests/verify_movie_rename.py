"""Fixture + server launcher for verifying the TRANSCODED movie-renamer UI.

Run this, then drive the UI with the Playwright MCP (see the module docstring steps).
It stands up an isolated temp SQLite DB + temp TRANSCODED folder so the real data is
never touched, seeds one misnamed movie, and serves the FastHTML app.

Usage:
    uv run python tests/verify_movie_rename.py [--port 5099]

It prints a JSON line with the fixture paths and URL, then serves until Ctrl-C.

Manual/Playwright-MCP verification steps:
  1. Open http://127.0.0.1:<port>/
  2. Assert the row for "seg_mainfeature_t01.mkv" is listed with proposed
     "The_Matrix_(1999).mkv".
  3. Click its "Rename" button; assert the status cell shows "✓ Renamed to ...".
  4. Assert the file on disk under <transcoded_dir> is now "The_Matrix_(1999).mkv".
"""
import argparse
import json
import os
import tempfile


def setup_fixtures() -> dict:
    tmp_root = tempfile.mkdtemp(prefix="verify_movie_rename_")
    transcoded_dir = os.path.join(tmp_root, "TRANSCODED")
    os.makedirs(transcoded_dir, exist_ok=True)
    db_path = os.path.join(tmp_root, "test.db")
    misnamed = "seg_mainfeature_t01.mkv"

    # Create the misnamed movie file on disk.
    open(os.path.join(transcoded_dir, misnamed), "w").close()

    # Point the app at the isolated DB + folder BEFORE importing app modules,
    # so load_dotenv (override=False) can't clobber these.
    os.environ["SQLITE_DB_PATH"] = db_path
    os.environ["TRANSCODED_LOCAL_PATH"] = transcoded_dir

    from sync_jelly_kodi.sqlite_util import upsert_jelly_items

    # Seed one TRANSCODED Movie whose file is misnamed. Jellyfin knows it as
    # "The Matrix" (1999), so the proposed canonical name is The_Matrix_(1999).mkv.
    item = {
        "Id": "movie1",
        "UserId": "user1",
        "UserName": "venkman",
        "Name": "The Matrix",
        "ProductionYear": 1999,
        "Type": "Movie",
        "unified_root": "TRANSCODED",
        "unified_file": misnamed,
        "UserData": {"PlayCount": 0, "PlaybackPositionTicks": 0},
    }
    upsert_jelly_items([item])

    return {
        "tmp_root": tmp_root,
        "transcoded_dir": transcoded_dir,
        "db_path": db_path,
        "misnamed": misnamed,
        "expected": "The_Matrix_(1999).mkv",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5099)
    args = parser.parse_args()

    fixtures = setup_fixtures()
    from sync_jelly_kodi.web import serve

    print(json.dumps({**fixtures, "url": f"http://127.0.0.1:{args.port}/"}), flush=True)
    serve("127.0.0.1", args.port)


if __name__ == "__main__":
    main()
