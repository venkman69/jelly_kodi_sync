"""FastHTML UI to rename misnamed TRANSCODED movies using Jellyfin metadata.

Lists movies under the TRANSCODED root whose filenames don't follow the kodidash
``Title_(YEAR).ext`` convention, shows the canonical name derived from Jellyfin's
title/year, and lets the user rename the file in place.

Run via the CLI: ``uv run sync-jelly-kodi web``.
"""
import hashlib
import logging
import os
from pathlib import Path

from fasthtml.common import (
    Button,
    Div,
    Form,
    H1,
    Hidden,
    Input,
    P,
    Span,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Titled,
    Tr,
    fast_app,
)

from . import jelly_util, utils
from .movie_rename import get_transcoded_movies, rename_movie

utils.load_dotenvs()
utils.config_logger(
    os.getenv("LOG_FILE", "jelly_kodi_sync.log"),
    Path(os.getenv("LOG_DIR", "./logs")),
)
logger = logging.getLogger(__name__)

app, rt = fast_app()


def _row_id(current_file: str) -> str:
    """Stable DOM id for a movie row, derived from its filename."""
    return "row-" + hashlib.md5(current_file.encode("utf-8")).hexdigest()[:12]


def movie_row(m: dict, status: str = "", ok: bool | None = None) -> Tr:
    """Render one table row for a misnamed movie (also used as the rename response)."""
    rid = _row_id(m["current_file"])

    flags = []
    if not m["has_metadata"]:
        flags.append(Span(" ⚠ no Jellyfin year", style="color:#b00"))
    if not m["exists_on_disk"]:
        flags.append(Span(" ⚠ not found on disk", style="color:#b00"))

    if ok is True:
        status_cell = Span(f"✓ {status}", style="color:#080")
    elif ok is False:
        status_cell = Span(f"✗ {status}", style="color:#b00")
    else:
        status_cell = Span(status)

    form = Form(
        Hidden(name="current_file", value=m["current_file"]),
        Input(
            name="proposed",
            value=m["proposed"],
            style="width:22rem",
            placeholder="Title_(YEAR).ext",
        ),
        Button("Rename", type="submit"),
        hx_post="/rename",
        hx_target=f"#{rid}",
        hx_swap="outerHTML",
    )

    return Tr(
        Td(m["current_file"]),
        Td(m["title"] or "—"),
        Td(str(m["year"]) if m["year"] else "—"),
        Td(form, *flags),
        Td(status_cell),
        id=rid,
    )


def movies_table() -> Div:
    movies = get_transcoded_movies()
    header = Div(
        Span(f"{len(movies)} misnamed movie(s) in TRANSCODED"),
        Button(
            "Refresh from Jellyfin",
            hx_post="/refresh",
            hx_target="#movies",
            hx_swap="outerHTML",
            style="margin-left:1rem",
        ),
        style="margin-bottom:1rem",
    )
    table = Table(
        Thead(
            Tr(
                Th("Current filename"),
                Th("Jellyfin title"),
                Th("Year"),
                Th("Proposed name"),
                Th("Status"),
            )
        ),
        Tbody(*[movie_row(m) for m in movies]),
    )
    return Div(header, table, id="movies")


@rt("/")
def index():
    return Titled("TRANSCODED movie renamer", movies_table())


@rt("/rename")
def rename(current_file: str, proposed: str):
    ok, message = rename_movie(current_file, proposed)
    # Rebuild the row so its state (new filename / on-disk flag) reflects the result.
    if ok:
        m = {
            "current_file": proposed,
            "title": "",
            "year": None,
            "ext": "",
            "proposed": proposed,
            "has_metadata": True,
            "exists_on_disk": True,
        }
    else:
        m = next(
            (x for x in get_transcoded_movies() if x["current_file"] == current_file),
            {
                "current_file": current_file,
                "title": "",
                "year": None,
                "ext": "",
                "proposed": proposed,
                "has_metadata": True,
                "exists_on_disk": True,
            },
        )
    return movie_row(m, status=message, ok=ok)


@rt("/refresh")
def refresh():
    logger.info("Refreshing Jellyfin data via jelly_pull()")
    try:
        jelly_util.jelly_pull()
    except Exception as e:  # noqa: BLE001 - surface any pull failure in the UI
        logger.error("jelly_pull failed: %s", e)
        return Div(
            P(f"Refresh failed: {e}", style="color:#b00"),
            movies_table(),
            id="movies",
        )
    return movies_table()


def serve(host: str = "127.0.0.1", port: int = 5001):
    import uvicorn

    logger.info("Starting movie-renamer UI at http://%s:%s", host, port)
    # Always reload on source change. reload needs an import string (not the app
    # object) so the worker can re-import; watch this package's source dir.
    uvicorn.run(
        "sync_jelly_kodi.web:app",
        host=host,
        port=port,
        reload=True,
        reload_dirs=[os.path.dirname(__file__)],
    )
