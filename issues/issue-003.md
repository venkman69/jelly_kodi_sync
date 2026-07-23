# Movie Archiving Function
Add a 3rd tab to identify watched movies in TRANSCODED. These movies should be moved to the archive directory. 
Use the safe pattern (and improve if logic is not sufficiently safe) from ../kodidash to archive.

Make sure logging and error handling are good. If a failure occurs during a move the information should show what the current state of the file/folder is so that it can be corrected manually. This should be displayed in the ui as well.

So it would be best show each step in the UI as a process and a green tick if it succeeds.
and if it fails it would get a red tick and process would stop.

---

## Decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | What defines "watched"? | `PlayCount > 0` only — fully watched. Do not archive partially-watched (resume position only). |
| 2 | Non-kodi-named files | Skip — show a warning row directing user to Renamer tab first. |
| 3 | Kodi cleanup after archive | Show a UI note recommending a manual Kodi library scan. |
| 4 | Is ARCHIVE indexed by Jellyfin? | Yes — Jellyfin will pick up the moved file on its next scan. |
| 5 | Confirmation dialog | Required — show confirm dialog with source → destination before archiving. |
| 6 | Sidecars | Must be moved to the target archive folder alongside the movie file. |

---

## Edge Cases and Implementation Notes

### Watch detection

**1. Partially vs fully watched (DECISION NEEDED)**
`PlayCount > 0` means fully watched; `PlaybackPositionTicks > 0` means started but not finished.
Archiving a partially-watched movie is destructive. Recommendation: only archive when `PlayCount > 0`.

**2. Stale DB (DECISION NEEDED)**
If `kodi_pull` or `jelly_pull` hasn't run recently, watch status in the DB may not reflect reality.
Consider showing the staleness panel (same as sync tab) so the user knows how fresh the data is before archiving.

**3. Multi-user watch deduplication**
`jellyitems` has one row per `(id, user_id)`. Deduplication by `unified_file` must check if ANY user has
watched the file — not just the first row seen. Need OR logic across all users for the same file.

**4. Path format consistency**
`unified_file` for TRANSCODED movies has a leading `/` (e.g. `/Aliens_T091.mkv`). When cross-referencing
with `kodiitems`, the path format must match exactly — `find_kodi_items_by_file` uses exact string match.

**5. Movie not in kodiitems**
If a movie was never pulled from Kodi (Kodi doesn't have it), only the Jellyfin side exists.
Watch-status detection must check both sources independently (OR logic), not require both to match.

---

### Naming and metadata

**6. Non-kodi-named source files (DECISION NEEDED)**
TRANSCODED contains both renamed (kodi-named) and not-yet-renamed files. Two options:
- (a) Require rename first — skip non-kodi-named files, show a warning directing user to the Renamer tab.
- (b) Archive anyway — use Jellyfin metadata (title + year from jellyitems) to compute the archive path,
  regardless of the current filename on disk.

**7. Archive directory name collision**
Two source files could compute the same `Title_(Year)` target directory (e.g. two editions of the same
film). Need collision detection — same `Counter(proposed)` pattern used in the renamer.

**8. Windows-illegal characters in title**
If ARCHIVE is a CIFS mount, titles with `:`, `*`, `?` etc. will fail `os.mkdir`. Apply the same
`_normalize_illegal()` treatment from `movie_rename.py` when constructing the target path.

---

### Filesystem safety

**9. Cross-filesystem move**
`os.rename` raises `OSError: [Errno 18] Invalid cross-device link` when TRANSCODED and ARCHIVE are on
different mounts (very likely in this setup). Must use `shutil.move` instead of `os.rename`.

**10. Partially-created target directory**
If a previous archive attempt failed after `os.mkdir` but before the file move, the directory exists but
is empty. The guard should check whether the target *file* exists, not just the directory — an empty
pre-existing directory should be reusable.

**11. Archive root not mounted**
If `ARCHIVE` env var points to a CIFS mount that isn't currently mounted, `os.makedirs` will silently
create the path locally in the wrong place. Must verify the ARCHIVE root exists before starting any step.

**12. Sidecar partial failure**
Sidecars (subtitles, .nfo, artwork etc.) must be moved to the target archive folder alongside the video —
they are not optional. If the video moves to ARCHIVE but a sidecar move fails, files end up split across
two locations. The failure `current_state` output must explicitly list what moved and what didn't for
manual recovery. Unlike the renamer (where sidecar failure is best-effort), archive sidecar failures
should be clearly flagged as requiring attention.

**13. Non-atomic operation**
The archive is `mkdir` + `move` + sidecar moves. A crash between steps leaves an intermediate state.
Each step's error output must describe exactly what was and wasn't completed.

**14. `os.mkdir` not `os.makedirs`**
Using `makedirs(exist_ok=True)` would silently succeed if the dir already exists, masking collisions.
Use plain `os.mkdir` so an existing directory raises an error.

---

### Post-archive state

**15. jellyitems DB cleanup**
After archiving, the `jellyitems` row still points to the old TRANSCODED path. On next refresh the movie
reappears with `exists_on_disk=False`. Must call `delete_jelly_items_by_file()` (already in
`sqlite_util.py`) after a successful archive — same as `delete_movie()` does.

**16. Kodi library stale entry (DECISION NEEDED)**
Kodi's library will retain the old TRANSCODED entry until a library scan. Options:
- (a) Show a note in the UI recommending a Kodi library scan after archiving.
- (b) Automatically trigger `kodi_library_scan()` after each successful archive.

**17. Jellyfin stale entry (DECISION NEEDED)**
Jellyfin will still list the file under TRANSCODED until its next scan, and watch history is tied to the
old ItemId. If ARCHIVE is not a Jellyfin library path, watch data for the archived file is orphaned.
Clarification needed: is the ARCHIVE directory also indexed by Jellyfin?

---

### UI

**18. Confirmation dialog (DECISION NEEDED)**
The Archive button fires immediately. Archiving is harder to undo than a rename.
Recommendation: show a `confirm()` dialog (like the delete button) showing source → destination.

**19. Double-submit prevention**
If the user clicks Archive twice before the HTMX response arrives, two operations fire — the second
will fail (file already moved) but is confusing. Disable the button on first click using
`hx-disabled-elt="this"` or equivalent.

**20. ARCHIVE env var not configured**
If `ARCHIVE` is unset, the tab must show a clear configuration warning rather than an empty table or
a traceback.

**21. No pagination**
If there are many watched movies the table could be long. Not a blocker for initial implementation.
