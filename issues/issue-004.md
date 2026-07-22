# Preserve Jellyfin watch progress across a movie rename

When a movie is renamed via the web UI (`/rename` → `movie_rename.rename_movie`), only
the on-disk files are moved (video + sidecars). Jellyfin is never told about the change.

Jellyfin keys a user's watch data (`UserData`) by **ItemId**, and the ItemId is derived
from the file path. So on the next Jellyfin library scan the renamed file looks like a
*brand new* item and gets a *new* ItemId, while the old item — carrying the resume
position, play count, and played flag — is dropped as stale. The result: renaming a
movie silently wipes its watch progress.

Note that watch data is **per user**: the `jellyitems` table is keyed on
`(id, user_id)`, so every user who had progress on that title must be handled, not just
one.

## Desired behaviour

As part of the rename, copy the existing Jellyfin watch status onto the newly-created
item so progress survives the rename. This should happen **automatically** as a single
step — the user renames, and the watch status carries over with no extra action.

For now, preserve **watch progress and played state only**:

* `PlaybackPositionTicks` (resume position)
* `PlayCount`
* `Played`

Favorites / last-played (`IsFavorite`, `LastPlayedDate`, `PlayedPercentage`) are out of
scope.

## Proposed approach

Reuse the existing building blocks in `src/sync_jelly_kodi/jelly_util.py`; the machinery
to both read and write `UserData` already exists.

1. **Capture (before rename).** For the current file, read each user's `UserData`
   (`PlaybackPositionTicks`, `PlayCount`, `Played`). This can come from the cached
   `jellyitems.userdata_json` already populated by `jelly_pull`, or be fetched fresh via
   a `JellySession`. `find_jelly_items_by_file()` in `sqlite_util.py` returns all
   per-user rows for a given file.
2. **Rename on disk.** Existing `rename_movie()` flow, unchanged.
3. **Refresh Jellyfin** so it indexes the renamed file and mints the new ItemId. Today
   the code only does a full `jelly_pull()` after a manual "Refresh" click. Because the
   Jellyfin scan is asynchronous, the new ItemId will not exist immediately after the
   file move — a targeted library refresh plus a poll-with-timeout will be needed.
4. **Locate the new item** by its renamed path/filename, using the same `unified_file`
   normalization as `get_root_file_path()` / `find_jelly_items_by_file()`.
5. **Reapply watch status** per user with the existing
   `update_playback_position(session, user_id, item_id, position_ticks, play_count)` —
   it already sends exactly `PlaybackPositionTicks` / `PlayCount` / `Played`.

## UX

The transfer is part of the `/rename` action (`web.py`), so it happens in one click.
Surface the outcome in the row status, e.g. `Renamed to X (+watch status restored for N
users)`, consistent with how sidecar rename results are reported today.

## Open questions (for implementation)

* **Waiting for the async scan** to create the new item is the main risk — decide
  between polling `jelly_pull` with a timeout vs. a targeted `Items/{id}/Refresh` call,
  and how long to wait before giving up (and how to report a partial failure so watch
  data isn't silently lost).
* **`DRY_RUN`** should be respected, as `sync_watch_status_from_kodi_to_jelly` already
  does.

**Status:** Observed in testing (2026-07-22) that Jellyfin is smart enough to recognise
the renamed file as the same movie (likely via embedded TMDB/IMDB ID in its library
database) and automatically carries over watch progress without any intervention from
this tool. No implementation needed unless this behaviour is found to be unreliable.
