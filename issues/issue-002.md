# Jelly Kodi Sync UI
* Add a tab to perform sync between Kodi and Jellyfin.
* I would like an 'auto sync' which follows the current process, and the process should be displayed above the auto-sync.
* Then the user should be given manual control for each leg of the sync as well as the direction of sync. Something like:
    * These steps would be needed each time:
        * Pull from Kodi
        * Pull from Jellyfin
    * These steps could be the manual control:
        * Compare Kodi and Jellyfin, and push changes to Jellyfin.
        * Compare Kodi and Jellyfin, and push changes to Kodi.

**Status:** Implemented. The web UI now has a tab bar ("Movie Renamer" + "Jelly-Kodi
Sync"). The Sync tab shows a *Data freshness* panel (last-pulled time per side, so
staleness is visible before pushing), an **Auto Sync** section that lists the ordered
process and runs it as sequential per-step ticks (each step is a chained HTMX request
that only fires after the previous succeeds — green ✓ / red ✗, halts on failure, no
SSE), and **Manual Controls** (Pull from Kodi, Pull from Jellyfin, Compare & push to
Jellyfin, Compare & push to Kodi).

The shared sync legs were extracted from `main.py`'s `sync` command into
`sync_ops.py` (`set_watch_*` plus `*_step` wrappers returning `(ok, message)`), so the
CLI and UI run identical logic. Verified end-to-end with Playwright
(`tests/verify_issue-002.py`): tab nav, staleness "never" → timestamp after a pull, and
all 7 auto-sync steps completing with green ticks.
