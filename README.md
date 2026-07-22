# sync_jelly_kodi

A command-line tool that synchronizes **watch status** — playcount (watched / unwatched) and
resume position — between a [Jellyfin](https://jellyfin.org/) server and a
[Kodi](https://kodi.tv/) media player.

Items are matched across the two systems by their normalized file path (`unified_file`), so a movie
or episode you finish in Kodi is marked watched in Jellyfin, and vice versa.

## Overview

The tool is a [Typer](https://typer.tiangolo.com/) CLI. Its entry point is
`sync-jelly-kodi = sync_jelly_kodi.main:app` (see `pyproject.toml`). Data pulled from each system is
cached in a local SQLite database, which is then used to reconcile watch state in both directions.

`bin/sync_jelly_kodi` is a thin bash wrapper around the CLI: it sources `.env` and `.credentials`,
activates the virtualenv, and forwards its arguments to `sync-jelly-kodi`.

## Installation

```bash
uv sync
```

The `bin/sync_jelly_kodi` wrapper additionally requires the `common_infra` project linked as a
subfolder (it sources `common_infra/run_functions` for process/pidfile management). If you invoke
the CLI directly with `uv run`, `common_infra` is not needed.

## Configuration

Configuration is read from two dotenv files, loaded in order by `utils.load_dotenvs()`:

1. `.env` — non-secret settings.
2. `.credentials` — secrets. Copy `.credentials.template` to `.credentials` and fill it in:

```bash
cp .credentials.template .credentials
```

### Environment variables

| Variable | File | Purpose |
|----------|------|---------|
| `JELLYFIN_URL` | `.env` | Base URL of the Jellyfin server (required for `sync` / `pull-jelly`). |
| `JELLYFIN_API_KEY` | `.credentials` | Jellyfin API key (required for `sync` / `pull-jelly`). |
| `JELLYFIN_SYNC_USER` | `.env` | Jellyfin user whose watch state is synced. |
| `JELLY_MOUNT_PAT` | `.env` | Regex (3 capture groups) that normalizes Jellyfin file paths — see below. |
| `KODIHOST` | `.env` | Kodi host / address. |
| `KODIPORT` | `.env` | Kodi JSON-RPC port. |
| `KODIUSER` | `.credentials` | Kodi username. |
| `KODIPASS` | `.credentials` | Kodi password. |
| `KODI_MOUNT_PAT` | `.env` | Regex (3 capture groups) that normalizes Kodi file paths — see below. |

### Path normalization: `JELLY_MOUNT_PAT` / `KODI_MOUNT_PAT`

Jellyfin and Kodi each report media paths under their own mount root, but the shared library lives
under a common `movies` directory with `RIP` / `TRANSCODED` / `EPISODIC` subfolders. Jellyfin (here,
running in a Podman container on Ubuntu) sees it mounted at `/mnt/movies/...`; Kodi mounts the same
share at its own location. To match the same file across both systems, each side's path is run
through a regex that strips the mount prefix and captures a normalized path.

`JELLY_MOUNT_PAT` is applied in `get_root_file_path()` (`jelly_util.py`) to each item's `Path`, and
`KODI_MOUNT_PAT` is applied equivalently on the Kodi side (`kodi_util.py`). The default pattern is
`^(.*/movies/)([^/]+)(.*)`, and each pattern **must produce exactly 3 capture groups**. For the
example path `/mnt/movies/TRANSCODED/Akira_(1988).mkv`:

- **Group 0** — the mount prefix up to and including `movies/` (`/mnt/movies/`), discarded.
- **Group 1** — becomes `unified_root`: the category folder (`TRANSCODED`).
- **Group 2** — becomes `unified_file`: everything after it (`/Akira_(1988).mkv`). Note the greedy
  `(.*)` keeps the leading `/` separator, so `unified_file` values start with `/`. Any Windows `\`
  is also converted to Unix `/` (a no-op on Linux, retained for portability).

The resulting `unified_file` is the key used to match Jellyfin items against Kodi items during
`sync`. If a path fails to match, or does not yield exactly 3 groups, the item is logged as an error
and skipped (its root/file resolve to `None`), so it won't be synced.

> Consumers that need the bare filename (e.g. the `web` movie renamer) should `os.path.basename()`
> `unified_file` to drop that leading `/`.
| `SQLITE_DB_PATH` | `.env` | Path to the local SQLite database. |
| `LOG_DIR` | `.env` | Directory for log files (default `./logs`). |
| `LOG_FILE` | `.env` | Log file name (default `jelly_kodi_sync.log`). |
| `LOG_LEVEL` | `.env` | Log level (default `INFO`). |
| `DRY_RUN` | `.env` | When set, run without writing changes back. |
| `TRANSCODED_LOCAL_PATH` | `.env` | Local filesystem path to the mounted TRANSCODED share, used by the `web` movie-renamer UI to rename files (e.g. `/mnt/movies/TRANSCODED`). |

## Usage

Run any command directly through `uv`:

```bash
uv run sync-jelly-kodi <command>
```

…or through the bash wrapper (which loads the dotenv files and venv for you):

```bash
./bin/sync_jelly_kodi <command>
```

### Commands

| Command | Description |
|---------|-------------|
| `pull-jelly` | Pull data from Jellyfin into the local database. |
| `pull-kodi` | Verify Kodi is reachable, then pull Kodi data into the local database. |
| `sync` | Bidirectional watch-status sync between Jellyfin and Kodi (see below). |
| `web` | Launch the FastHTML movie-renamer UI (see below). |

## Movie renamer UI (`web`)

```bash
uv run sync-jelly-kodi web            # serves at http://127.0.0.1:5001
uv run sync-jelly-kodi web --port 8080

# or via the bin wrapper (sources .env / .credentials, runs in foreground):
./bin/sync_jelly_kodi_web             # serves at http://127.0.0.1:5001
./bin/sync_jelly_kodi_web --port 8080
```

The server always auto-reloads on source changes (uvicorn `reload=True` watching the package
source), so edits take effect without a manual restart.

Lists **movies** (not series/episodes) under the `TRANSCODED` root whose filenames don't follow the
Kodi naming convention `Title_With_Underscores_(YEAR).ext` (as used by the sibling `kodidash`
project). For each, it derives the canonical name from Jellyfin's title/year (read from the local
SQLite `jellyitems` table) and offers a **Rename** button that renames the file in place on the
`TRANSCODED_LOCAL_PATH` share. A **Refresh from Jellyfin** button re-runs `jelly_pull()`.

Because `TRANSCODED_LOCAL_PATH` is typically a case-insensitive CIFS mount, renames that only change
case are performed via an intermediate temp filename. Set `TRANSCODED_LOCAL_PATH` in `.env` before
using this command.

## How `sync` works

`sync` first runs a **preflight check** that Kodi is reachable; if Kodi is down it logs the error
and exits with status `1`. It then runs an 8-step pipeline, timing and logging each step:

| Step | Action |
|------|--------|
| 1 | Pull latest data from Jellyfin into the local DB. |
| 2 | Read watched items from Jellyfin (from the DB). |
| 3 | **Push Jellyfin → Kodi** watch status. |
| 4 | Pull latest data from Kodi into the local DB. |
| 5 | Read watched items from Kodi (from the DB). |
| 6 | **Push Kodi → Jellyfin** watch status. |
| 7 | Re-pull Jellyfin to refresh local state after the changes. |
| 8 | Re-pull Kodi to refresh local state after the changes. |

### Matching and direction

- **Jellyfin → Kodi** (step 3): for each watched Jellyfin item, find the matching Kodi item by
  file path. On a single unique match the Kodi item's watch status is updated. Multiple matches are
  logged as a warning and skipped; no match is simply logged.

- **Kodi → Jellyfin** (step 6): a Jellyfin session is opened, then for each watched Kodi item all
  matching Jellyfin items are updated. One file can appear in multiple Jellyfin users' libraries, so
  every match is synced.

Note the Jellyfin → Kodi direction runs first, and Kodi data is re-pulled (step 4) *before*
computing the reverse direction — so the Kodi → Jellyfin sync sees Kodi's freshly updated state.
