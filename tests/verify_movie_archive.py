"""Fixture + server launcher for verifying the movie Archiver UI.

Sets up an isolated SQLite DB, TRANSCODED folder, and ARCHIVE folder, seeds one
watched kodi-named movie (+ sidecar), and serves the FastHTML app so the Archive
tab can be exercised with Playwright.

Usage:
    uv run python tests/verify_movie_archive.py [--port 5099]

Prints a JSON line with fixture paths and URL, then serves until Ctrl-C.

Manual / Playwright-MCP verification steps:
  1. Open http://127.0.0.1:<port>/archive
  2. Assert the row for "The_Matrix_(1999).mkv" is listed with watch badges.
  3. Click the archive (📦) icon; confirm the dialog.
  4. Assert step ticks appear and all show ✓.
  5. Assert the target file exists at <archive_dir>/The_Matrix_(1999)/The_Matrix_(1999).mkv
  6. Assert the sidecar is at <archive_dir>/The_Matrix_(1999)/The_Matrix_(1999).srt
"""
import argparse
import json
import os
import tempfile


def setup_fixtures() -> dict:
    tmp_root = tempfile.mkdtemp(prefix="verify_movie_archive_")
    transcoded_dir = os.path.join(tmp_root, "TRANSCODED")
    archive_dir = os.path.join(tmp_root, "ARCHIVE")
    os.makedirs(transcoded_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)
    db_path = os.path.join(tmp_root, "test.db")

    movie_file = "The_Matrix_(1999).mkv"
    sidecar_file = "The_Matrix_(1999).srt"

    # Create on-disk files.
    open(os.path.join(transcoded_dir, movie_file), "w").close()
    open(os.path.join(transcoded_dir, sidecar_file), "w").close()

    # Set env vars BEFORE importing app modules.
    os.environ["SQLITE_DB_PATH"] = db_path
    os.environ["TRANSCODED_LOCAL_PATH"] = transcoded_dir
    os.environ["ARCHIVE"] = archive_dir

    from sync_jelly_kodi.sqlite_util import upsert_jelly_items

    # Seed a watched TRANSCODED Movie (PlayCount=1).
    item = {
        "Id": "matrix1",
        "UserId": "user1",
        "UserName": "venkman",
        "Name": "The Matrix",
        "ProductionYear": 1999,
        "Type": "Movie",
        "unified_root": "TRANSCODED",
        "unified_file": f"/{movie_file}",
        "UserData": {"PlayCount": 1, "PlaybackPositionTicks": 0},
    }
    upsert_jelly_items([item])

    return {
        "tmp_root": tmp_root,
        "transcoded_dir": transcoded_dir,
        "archive_dir": archive_dir,
        "db_path": db_path,
        "movie_file": movie_file,
        "sidecar_file": sidecar_file,
        "expected_video": os.path.join(archive_dir, "The_Matrix_(1999)", movie_file),
        "expected_sidecar": os.path.join(archive_dir, "The_Matrix_(1999)", sidecar_file),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5099)
    args = parser.parse_args()

    fixtures = setup_fixtures()
    from sync_jelly_kodi.web import serve

    print(json.dumps({**fixtures, "url": f"http://127.0.0.1:{args.port}/archive"}), flush=True)
    serve("127.0.0.1", args.port)


if __name__ == "__main__":
    main()
