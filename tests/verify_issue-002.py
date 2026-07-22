"""Fixture + server launcher for verifying the "Jelly-Kodi Sync" tab (issue-002).

Stands up the FastHTML UI against an isolated temp SQLite DB (real data untouched)
and replaces the sync step functions with fast, side-effect-free stubs so the
chained auto-sync and the manual buttons can be exercised without a live Kodi or
Jellyfin server. The stub "pull" steps upsert a row so the staleness panel visibly
changes from "never" to a timestamp.

Usage:
    uv run python tests/verify_issue-002.py [--port 5097] [--delay 0.4]

Prints a JSON line with the URL, then serves until Ctrl-C.

Playwright-MCP verification steps:
  1. Open http://127.0.0.1:<port>/sync
  2. Assert the tab bar shows "Movie Renamer" and "Jelly-Kodi Sync"; the latter is active.
  3. Assert #staleness shows "Kodi last pulled: never" at rest.
  4. Click "Pull from Kodi"; assert #manual-result shows a green "✓ Pull from Kodi"
     tick and #staleness "Kodi last pulled:" is no longer "never".
  5. Click "Auto Sync"; wait for "✓ Auto-sync complete." to appear in #auto-results,
     and assert all 7 step labels rendered a ✓ tick (no ✗).
"""
import argparse
import json
import os
import tempfile
import time


def setup_fixtures() -> dict:
    tmp_root = tempfile.mkdtemp(prefix="verify_issue002_")
    db_path = os.path.join(tmp_root, "test.db")

    # Point the app at the isolated DB BEFORE importing app modules.
    os.environ["SQLITE_DB_PATH"] = db_path
    os.environ["TRANSCODED_LOCAL_PATH"] = os.path.join(tmp_root, "TRANSCODED")
    os.makedirs(os.environ["TRANSCODED_LOCAL_PATH"], exist_ok=True)

    from sync_jelly_kodi.sqlite_util import upsert_jelly_items

    # Seed one Jellyfin item so "Jellyfin last pulled" shows a timestamp; Kodi stays
    # "never" until the Pull-from-Kodi stub inserts a row.
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
                "unified_file": "/The_Matrix_(1999).mkv",
                "UserData": {"PlayCount": 1, "PlaybackPositionTicks": 0},
            }
        ]
    )
    return {"tmp_root": tmp_root, "db_path": db_path}


def install_stubs(delay: float) -> None:
    """Replace the real sync steps with fast, offline stubs (in-process)."""
    from sync_jelly_kodi import sync_ops, web
    from sync_jelly_kodi.sqlite_util import upsert_jelly_items, upsert_kodi_items

    def stub_preflight():
        time.sleep(delay)
        return True, "Kodi is reachable (stub)"

    def stub_pull_jelly():
        time.sleep(delay)
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
                    "unified_file": "/The_Matrix_(1999).mkv",
                    "UserData": {"PlayCount": 1, "PlaybackPositionTicks": 0},
                }
            ]
        )
        return True, "Pulled items from Jellyfin (stub)"

    def stub_pull_kodi():
        time.sleep(delay)
        upsert_kodi_items(
            [
                {
                    "uniqueid": "k1",
                    "unified_root": "TRANSCODED",
                    "unified_file": "/The_Matrix_(1999).mkv",
                    "playcount": 1,
                    "resume": {"position": 0.0},
                }
            ]
        )
        return True, "Pulled items from Kodi (stub)"

    def stub_push():
        time.sleep(delay)
        return True, "Pushed watch status (stub) (1/1 matched)"

    # Rebuild the shared auto-sequence list in place so web.AUTO_STEPS (same object)
    # sees the stubs, preserving the original labels.
    new_steps = []
    for label, _ in sync_ops.AUTO_STEPS:
        low = label.lower()
        if "preflight" in low:
            fn = stub_preflight
        elif "jellyfin" in low and "pull" in low:
            fn = stub_pull_jelly
        elif "kodi" in low and "pull" in low:
            fn = stub_pull_kodi
        else:
            fn = stub_push
        new_steps.append((label, fn))
    sync_ops.AUTO_STEPS[:] = new_steps

    # Manual endpoints hold their own name-bound references; patch those too.
    web.pull_jelly_step = stub_pull_jelly
    web.pull_kodi_step = stub_pull_kodi
    web.push_kodi_to_jelly_step = stub_push
    web.push_jelly_to_kodi_step = stub_push


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5097)
    parser.add_argument("--delay", type=float, default=0.4)
    args = parser.parse_args()

    fixtures = setup_fixtures()
    install_stubs(args.delay)

    # Run the app object directly (no uvicorn reload): reload re-imports the app in a
    # subprocess worker, bypassing the in-process stubs.
    import uvicorn

    from sync_jelly_kodi.web import app

    print(json.dumps({**fixtures, "url": f"http://127.0.0.1:{args.port}/sync"}), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
