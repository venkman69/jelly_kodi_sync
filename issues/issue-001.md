# Refresh from Jellyfin should show progress

At minimum a spinner to indicate running process.
Best is to show a progress bar that shows processing state.

**Status:** Spinner implemented. The "Refresh from Jellyfin" button now shows an
animated HTMX `hx-indicator` spinner ("Refreshing…") while the `/refresh` request
is in flight, and hides it when the table swaps back in. Verified end-to-end with
Playwright (`tests/verify_refresh_spinner.py`).

A full progress bar remains a follow-up: `jelly_pull()` is a single blocking call
with no incremental progress, so it would need SSE/polling plus per-step progress
reporting inside the pull.


# SRT file handling.
SRT files should be renamed when the movie file is renamed.

For example for 'Top Secret'
```
ls -altr /mnt/movies/TRANSCODED| grep -i thud
-rwxrwxrwx 1 root root  4079933003 Jun 15  2025 Thudarum_(2025).mp4
-rwxrwxrwx 1 root root      163978 Jun 15  2025 thudarum (2025).eng.srt
```
So, when renaming 'Thudarum' it should map the srt file's name to the new name of Thudarum. Logic is simple:
* Keep the .eng.srt part, sometimes it is numbered or named with a source as well such as `.eng.<source>.srt`.
* Change the rest of the name to match and move the file in the same manner as the movie

**Status:** Resolved by sidecar-aware rename (commit 7a60d77). A rename now carries
along any sidecar sharing the video's stem (subtitles, `.nfo`, artwork), preserving
the `.eng.srt` / `.eng.<source>.srt` suffix.

The specific `Thudarum` mismatch above (video stem `Thudarum_(2025)` vs srt stem
`thudarum (2025)` — differing case + space/underscore) is a stale artifact of an
earlier rename that predated sidecar handling and left the srt orphaned. It is
intentionally not special-cased; going forward the video + srt are renamed together.
