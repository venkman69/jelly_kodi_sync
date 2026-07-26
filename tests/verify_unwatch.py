"""Playwright verification for the archive Unwatch button.

Sets up an isolated SQLite DB with one watched TRANSCODED movie, starts the
FastHTML server, then exercises the Unwatch flow:
  1. Card appears in the archive proposals
  2. Clicking Unwatch shows the step-by-step result card with an OK button
  3. Clicking OK removes the card and refreshes the archive list

Usage:
    uv run python tests/verify_unwatch.py [--headed] [--port 5098]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import time
import urllib.request


def setup_fixtures(db_path: str, transcoded_dir: str, archive_dir: str) -> str:
    """Seed env vars and DB; returns the movie filename."""
    movie_file = "The_Matrix_(1999).mkv"

    os.makedirs(transcoded_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)
    open(os.path.join(transcoded_dir, movie_file), "w").close()

    # Must be set BEFORE any app module is imported so the singleton picks up the test path.
    os.environ["SQLITE_DB_PATH"] = db_path
    os.environ["TRANSCODED_LOCAL_PATH"] = transcoded_dir
    os.environ["ARCHIVE"] = archive_dir
    os.environ["JELLYFIN_SYNC_USER"] = "venkman"
    # Prevent real Jellyfin/Kodi calls from crashing the route.
    os.environ.setdefault("JELLYHOST", "http://127.0.0.1:0")
    os.environ.setdefault("JELLYTOKEN", "dummy")
    os.environ.setdefault("KODIHOST", "127.0.0.1")
    os.environ["URL_PREFIX"] = ""  # routes at root paths for the test

    from sync_jelly_kodi.sqlite_util import upsert_jelly_items

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
    return movie_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5098)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    tmp_root = tempfile.mkdtemp(prefix="verify_unwatch_")
    db_path = os.path.join(tmp_root, "test.db")
    transcoded_dir = os.path.join(tmp_root, "TRANSCODED")
    archive_dir = os.path.join(tmp_root, "ARCHIVE")

    movie_file = setup_fixtures(db_path, transcoded_dir, archive_dir)
    print(f"Fixture: {movie_file}  db={db_path}", flush=True)

    # Import AFTER env vars are set.
    import uvicorn
    from sync_jelly_kodi.web import app

    def _serve():
        uvicorn.run(app, host="127.0.0.1", port=args.port, reload=False, log_level="warning")

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{args.port}"
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{base_url}/archive", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        print("FAIL: server did not start", file=sys.stderr)
        sys.exit(1)

    from playwright.sync_api import expect, sync_playwright

    failed = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        page.goto(f"{base_url}/archive")

        # 1. The Matrix card must appear.
        card = page.locator("text=The Matrix").first
        try:
            expect(card).to_be_visible(timeout=10_000)
            print("  ✓ Archive card visible")
        except AssertionError as e:
            print(f"  ✗ Archive card not visible: {e}", file=sys.stderr)
            print(f"  Page content: {page.locator('body').inner_text()[:500]}", file=sys.stderr)
            failed = True

        if not failed:
            # 2. Click Unwatch — accept the confirm dialog automatically.
            page.on("dialog", lambda d: d.accept())
            page.get_by_role("button", name="Unwatch").click()

            # 3. Steps result card should appear with an OK button.
            ok_btn = page.get_by_role("button", name="OK")
            try:
                expect(ok_btn).to_be_visible(timeout=15_000)
                print("  ✓ OK button visible — step-by-step result card appeared")
            except AssertionError as e:
                print(f"  ✗ OK button never appeared: {e}", file=sys.stderr)
                print(f"  Page content: {page.locator('body').inner_text()[:800]}", file=sys.stderr)
                failed = True

        if not failed:
            # Print result card text for inspection.
            try:
                card_text = page.locator("[id^=archrow-]").first.inner_text()
                print(f"  Result card:\n    {card_text.strip()}")
            except Exception:
                pass

            # 4. Click OK — card + OK button should disappear.
            ok_btn.click()
            try:
                expect(ok_btn).not_to_be_visible(timeout=8_000)
                print("  ✓ OK dismissed — card gone")
            except AssertionError as e:
                print(f"  ✗ OK button still visible after click: {e}", file=sys.stderr)
                failed = True

        browser.close()

    if failed:
        print("FAIL", file=sys.stderr)
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
