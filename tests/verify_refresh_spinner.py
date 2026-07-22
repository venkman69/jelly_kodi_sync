"""Fixture + server launcher for verifying the "Refresh from Jellyfin" spinner.

Stands up the FastHTML renamer UI against an isolated temp SQLite DB + TRANSCODED
folder (real data untouched) and monkeypatches ``jelly_pull`` to sleep briefly so
the HTMX ``hx-indicator`` spinner is observable during the request.

Usage:
    uv run python tests/verify_refresh_spinner.py [--port 5098] [--delay 2.0]

Prints a JSON line with the URL, then serves until Ctrl-C.

Playwright-MCP verification steps:
  1. Open http://127.0.0.1:<port>/
  2. Assert #refresh-spinner exists and is hidden (display:none) at rest.
  3. Click "Refresh from Jellyfin".
  4. While the request is in flight, assert #refresh-spinner is visible
     (the .htmx-request class is applied and the spinner animates).
  5. After the response swaps in, assert #refresh-spinner is hidden again.
"""
import argparse
import json
import os
import tempfile
import time


def setup_fixtures(delay: float) -> dict:
    tmp_root = tempfile.mkdtemp(prefix="verify_refresh_spinner_")
    transcoded_dir = os.path.join(tmp_root, "TRANSCODED")
    os.makedirs(transcoded_dir, exist_ok=True)
    db_path = os.path.join(tmp_root, "test.db")

    # Point the app at the isolated DB + folder BEFORE importing app modules.
    os.environ["SQLITE_DB_PATH"] = db_path
    os.environ["TRANSCODED_LOCAL_PATH"] = transcoded_dir

    from sync_jelly_kodi.sqlite_util import upsert_jelly_items

    # Seed one misnamed movie so the table renders a row.
    upsert_jelly_items(
        [
            {
                "Id": "movie1",
                "UserId": "user1",
                "UserName": "venkman",
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "Type": "Movie",
                "unified_root": "TRANSCODED",
                "unified_file": "seg_mainfeature_t01.mkv",
                "UserData": {"PlayCount": 0, "PlaybackPositionTicks": 0},
            }
        ]
    )
    open(os.path.join(transcoded_dir, "seg_mainfeature_t01.mkv"), "w").close()

    return {"tmp_root": tmp_root, "transcoded_dir": transcoded_dir, "db_path": db_path}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5098)
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()

    fixtures = setup_fixtures(args.delay)

    # Make refresh slow (and side-effect free) so the spinner is observable.
    from sync_jelly_kodi import jelly_util

    def _slow_pull() -> bool:
        time.sleep(args.delay)
        return True

    jelly_util.jelly_pull = _slow_pull

    # Run the app object directly (no uvicorn reload): reload re-imports the app in a
    # subprocess worker, which would bypass the in-process _slow_pull monkeypatch.
    import uvicorn

    from sync_jelly_kodi.web import app

    print(json.dumps({**fixtures, "url": f"http://127.0.0.1:{args.port}/"}), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
