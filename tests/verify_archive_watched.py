"""Playwright verification for the archive-library sync buttons.

Verifies the Sync tab's "Scan Archive" and "Mark Archive Watched" buttons produce
green-tick feedback in #manual-result. Run the web server with DRY_RUN=true first so
"Mark Archive Watched" makes no real writes to Jellyfin:

    DRY_RUN=true uv run sync-jelly-kodi web --port 5001

Then point this at the running instance (URL_PREFIX defaults to what the server uses):

    uv run tests/verify_archive_watched.py --base-url http://127.0.0.1:5001/jellykodisync

Requires the playwright browsers: `uv run playwright install chromium`.
"""
from __future__ import annotations

import sys

import typer
from playwright.sync_api import expect, sync_playwright

app = typer.Typer(add_completion=False)


def _check_button(page, label: str, expect_text: str, timeout_ms: int) -> None:
    page.get_by_role("button", name=label).click()
    result = page.locator("#manual-result")
    expect(result).to_contain_text("✓", timeout=timeout_ms)
    expect(result).to_contain_text(expect_text, timeout=timeout_ms)
    print(f"  ✓ '{label}' -> {result.inner_text().strip()}")


@app.command()
def main(
    base_url: str = "http://127.0.0.1:5001/jellykodisync",
    headed: bool = False,
) -> None:
    """Drive the Sync tab and assert the archive buttons report success."""
    sync_url = f"{base_url.rstrip('/')}/sync"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()
        page.goto(sync_url)

        # Both new buttons must be present.
        for label in ("Scan Transcoded", "Scan Archive", "Mark Archive Watched"):
            expect(page.get_by_role("button", name=label)).to_be_visible()

        # Archive scan is a quick 204 trigger; mark-watched enumerates every user's
        # archive movies so it needs a longer timeout.
        _check_button(page, "Scan Archive", "library scan triggered", timeout_ms=30_000)
        _check_button(page, "Mark Archive Watched", "archive movie(s) played", timeout_ms=180_000)

        browser.close()
    print("PASS: archive buttons report success feedback")


if __name__ == "__main__":
    try:
        app()
    except AssertionError as e:  # pragma: no cover - surfaced as non-zero exit
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
