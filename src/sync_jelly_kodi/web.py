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
    A,
    Div,
    Form,
    H2,
    Hidden,
    Hr,
    Input,
    Li,
    Meta,
    Ol,
    P,
    Span,
    Strong,
    Style,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Title,
    Tr,
    fast_app,
)
from monsterui.all import Button, ButtonT, Container, Theme, UkIcon

from . import jelly_util, utils
from .movie_rename import delete_movie, get_transcoded_movies, rename_movie
from .sqlite_util import get_last_pull_times
from .sync_ops import (
    AUTO_STEPS,
    jelly_library_refresh_step,
    kodi_library_scan_step,
    pull_jelly_step,
    pull_kodi_step,
    push_jelly_to_kodi_step,
    push_kodi_to_jelly_step,
)

utils.load_dotenvs()
utils.config_logger(
    os.getenv("LOG_FILE", "jelly_kodi_sync.log"),
    Path(os.getenv("LOG_DIR", "./logs")),
)
logger = logging.getLogger(__name__)

# Only what FrankenUI/Tailwind can't handle: HTMX indicator + mobile table cards.
_htmx_css = Style(
    """
    .htmx-indicator { display: none; }
    .htmx-request .htmx-indicator,
    .htmx-request.htmx-indicator { display: inline-flex; align-items: center; }
    .spinner {
        display: inline-block;
        width: 1rem; height: 1rem;
        border: 2px solid hsl(var(--border));
        border-top-color: hsl(var(--foreground));
        border-radius: 50%;
        animation: spin 0.6s linear infinite;
        flex-shrink: 0;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    @media (max-width: 640px) {
        .table-scroll table,
        .table-scroll thead,
        .table-scroll tbody,
        .table-scroll tr,
        .table-scroll td { display: block; width: 100%; }
        .table-scroll thead { display: none; }
        .table-scroll tr {
            border: 1px solid hsl(var(--border));
            border-radius: 0.5rem;
            margin-bottom: 0.75rem;
            padding: 0.5rem 0.75rem;
        }
        .table-scroll td { padding: 0.25rem 0; }
        .table-scroll td::before {
            content: attr(data-label);
            display: block;
            font-size: 0.7rem;
            font-weight: bold;
            text-transform: uppercase;
            color: hsl(var(--muted-foreground));
            margin-bottom: 0.1rem;
        }
    }
    """
)

app, rt = fast_app(
    hdrs=(
        Meta(name="viewport", content="width=device-width, initial-scale=1"),
        *Theme.slate.headers(),
        _htmx_css,
    ),
    bodycls="bg-background text-foreground",
)


def _row_id(current_file: str) -> str:
    """Stable DOM id for a movie row, derived from its filename."""
    return "row-" + hashlib.md5(current_file.encode("utf-8")).hexdigest()[:12]


def movie_row(m: dict, status: str = "", ok: bool | None = None) -> Tr:
    """Render one table row for a misnamed movie (also used as the rename response)."""
    rid = _row_id(m["current_file"])

    flags = []
    if not m["has_metadata"]:
        flags.append(Span(" ⚠ no Jellyfin year", cls="text-destructive text-sm"))
    if not m["exists_on_disk"]:
        flags.append(Span(" ⚠ not found on disk", cls="text-destructive text-sm"))
    if m.get("collision"):
        flags.append(Span(" ⚠ name conflict (duplicate target)", cls="text-destructive text-sm"))

    if ok is True:
        status_cell = Span(f"✓ {status}", cls="text-success text-sm")
    elif ok is False:
        status_cell = Span(f"✗ {status}", cls="text-destructive text-sm")
    else:
        status_cell = Span(status, cls="text-sm")

    rename_fid = f"frename-{rid}"
    delete_fid = f"fdelete-{rid}"
    escaped = m["current_file"].replace("'", "\\'")

    proposed_cell = Div(
        Div(
            Input(
                name="proposed",
                value=m["proposed"],
                placeholder="Title_(YEAR).ext",
                form=rename_fid,
                cls="uk-input flex-1 min-w-0",
            ),
            Div(
                Button(
                    UkIcon("tag", cls="h-4 w-4"),
                    type="submit", form=rename_fid, title="Rename",
                    cls="p-1 rounded hover:bg-accent text-muted-foreground hover:text-foreground",
                ),
                Button(
                    UkIcon("trash-2", cls="h-4 w-4"),
                    type="submit", form=delete_fid,
                    title="Delete file and sidecars",
                    cls="p-1 rounded hover:bg-accent text-destructive",
                    onclick=f"return confirm('Delete {escaped} and all its sidecars?')",
                ),
                cls="flex flex-col gap-0.5 flex-shrink-0",
            ),
            cls="flex items-center gap-2",
        ),
        *flags,
        Form(Hidden(name="current_file", value=m["current_file"]),
             id=rename_fid, hx_post="/rename",
             hx_target=f"#{rid}", hx_swap="outerHTML"),
        Form(Hidden(name="current_file", value=m["current_file"]),
             id=delete_fid, hx_post="/movie-delete",
             hx_target=f"#{rid}", hx_swap="outerHTML"),
    )

    return Tr(
        Td(m["current_file"], data_label="Current filename"),
        Td(m["title"] or "—", data_label="Jellyfin title"),
        Td(str(m["year"]) if m["year"] else "—", data_label="Year"),
        Td(proposed_cell, data_label="Proposed name"),
        Td(status_cell, data_label="Status"),
        id=rid,
    )


def movies_table() -> Div:
    movies = get_transcoded_movies()
    header = Div(
        Span(f"{len(movies)} misnamed movie(s) in TRANSCODED"),
        _btn(
            UkIcon("refresh-cw", cls="mr-2 h-4 w-4"),
            "Refresh from Jellyfin",
            cls=ButtonT.primary,
            hx_post="/refresh",
            hx_target="#movies",
            hx_swap="outerHTML",
            hx_indicator="#refresh-spinner",
        ),
        Span(
            Span(cls="spinner"),
            Span(" Refreshing…", cls="ml-2 text-sm text-muted-foreground"),
            id="refresh-spinner",
            cls="htmx-indicator",
        ),
        cls="flex flex-wrap items-center gap-2 mb-4",
    )
    table = Div(
        Table(
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
            cls="uk-table uk-table-divider uk-table-hover uk-table-small",
        ),
        cls="table-scroll",
    )
    return Div(header, table, id="movies")


def tab_nav(active: str) -> Div:
    """Top navigation shared by every tab; highlights the active one."""

    def tab(label: str, href: str, key: str) -> A:
        cls = (
            "px-4 py-2 text-sm font-semibold border-b-2 border-primary text-primary whitespace-nowrap"
            if active == key
            else "px-4 py-2 text-sm text-muted-foreground hover:text-foreground whitespace-nowrap"
        )
        return A(label, href=href, cls=cls)

    return Div(
        tab("Movie Renamer", "/", "renamer"),
        tab("Jelly-Kodi Sync", "/sync", "sync"),
        cls="flex gap-1 border-b border-border mb-6",
    )


def page(active: str, *content):
    return (
        Title("Jelly-Kodi Sync"),
        Container(tab_nav(active), *content, cls="py-4"),
    )


@rt("/")
def index():
    return page("renamer", movies_table())


# --- Jelly-Kodi Sync tab ----------------------------------------------------------


def _fmt_ts(ts: str | None) -> str:
    return ts if ts else "never"


def staleness_panel(oob: bool = False) -> Div:
    """Show when each side's data was last pulled.

    When ``oob`` is set, the panel replaces the existing ``#staleness`` element via
    an out-of-band swap after a pull refreshes the data.
    """
    times = get_last_pull_times()
    extra = {"hx_swap_oob": "true"} if oob else {}
    return Div(
        Span(Span("Kodi last pulled: "), Strong(_fmt_ts(times["kodi"])),
             cls="whitespace-nowrap"),
        Span(Span("Jellyfin last pulled: "), Strong(_fmt_ts(times["jelly"])),
             cls="whitespace-nowrap"),
        Span("(UTC)", cls="text-muted-foreground text-xs whitespace-nowrap"),
        id="staleness",
        cls="flex flex-wrap gap-x-4 gap-y-1 items-center p-3 bg-muted rounded-lg mb-6",
        **extra,
    )


def _tick(ok: bool, label: str, msg: str) -> P:
    if ok:
        return P(
            Span("✓ ", cls="text-success font-bold"),
            Strong(label),
            f" — {msg}",
            cls="my-1",
        )
    return P(
        Span("✗ ", cls="text-destructive font-bold"),
        Strong(label),
        f" — {msg}",
        cls="my-1 text-destructive",
    )


def _pending(idx: int) -> Div:
    """A self-firing placeholder that runs auto-step ``idx`` once inserted.

    ``hx_trigger=load`` posts to the step endpoint the moment this lands in the DOM,
    and the response replaces this element (outerHTML) with the step's tick + the
    next pending placeholder — chaining the steps sequentially without SSE.
    """
    label = AUTO_STEPS[idx][0]
    return Div(
        Span(cls="spinner"),
        Span(f" {label}…", cls="ml-2 text-sm text-muted-foreground"),
        id=f"auto-pending-{idx}",
        hx_post=f"/sync/auto/{idx}",
        hx_trigger="load",
        hx_target=f"#auto-pending-{idx}",
        hx_swap="outerHTML",
        cls="my-1 flex items-center",
    )


def _btn(*args, cls="", **kwargs):
    """Sync-tab button: inline-flex via style attr beats UIkit's dynamically-injected
    display:flex!important from core.iife.js, keeping buttons content-wide in flex rows."""
    return Button(*args, cls=cls, style="display: inline-flex; width: fit-content", **kwargs)


def sync_tab() -> Div:
    auto_section = Div(
        H2("Auto Sync", cls="text-xl font-semibold mt-2 mb-2"),
        P("Runs the full sync in order; each step starts only after the previous one succeeds:",
          cls="mb-2"),
        Ol(*[Li(label) for label, _ in AUTO_STEPS],
           cls="list-decimal pl-6 mb-4 space-y-0.5 text-sm"),
        _btn(
            UkIcon("arrow-left-right", cls="mr-2 h-4 w-4"),
            "Sync",
            cls=ButtonT.primary,
            hx_post="/sync/auto/0",
            hx_target="#auto-results",
            hx_swap="innerHTML",
        ),
        Div(id="auto-results", cls="mt-4"),
    )
    manual_section = Div(
        H2("Manual Controls", cls="text-xl font-semibold mt-2 mb-2"),
        P(
            "Pull each side first (check the freshness above), then push in the "
            "direction you want. Pushes compare whatever is currently in the database.",
            cls="mb-3",
        ),
        Div(
            _btn(
                UkIcon("download", cls="mr-1 h-4 w-4"), "from Kodi",
                cls=ButtonT.secondary,
                hx_post="/sync/pull-kodi",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            _btn(
                UkIcon("download", cls="mr-1 h-4 w-4"), "from Jellyfin",
                cls=ButtonT.secondary,
                hx_post="/sync/pull-jelly",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            _btn(
                UkIcon("shuffle", cls="mr-1 h-4 w-4"), "to Jellyfin",
                cls=ButtonT.secondary,
                hx_post="/sync/push-jelly",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            _btn(
                UkIcon("shuffle", cls="mr-1 h-4 w-4"), "to Kodi",
                cls=ButtonT.secondary,
                hx_post="/sync/push-kodi",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            cls="flex flex-wrap gap-2 mb-2",
        ),
        Div(
            _btn(
                UkIcon("refresh-cw", cls="mr-1 h-4 w-4"), "Kodi library",
                cls=ButtonT.ghost,
                hx_post="/sync/refresh-kodi-library",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            _btn(
                UkIcon("refresh-cw", cls="mr-1 h-4 w-4"), "Jellyfin library",
                cls=ButtonT.ghost,
                hx_post="/sync/refresh-jelly-library",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            cls="flex flex-wrap gap-2",
        ),
        Span(
            Span(cls="spinner"),
            Span(" Working…", cls="ml-2 text-sm text-muted-foreground"),
            id="manual-spinner",
            cls="htmx-indicator",
        ),
        Div(id="manual-result", cls="mt-4"),
    )
    return Div(staleness_panel(), auto_section, Hr(), manual_section)


@rt("/sync")
def sync_page():
    return page("sync", sync_tab())


@rt("/sync/auto/{idx}")
def sync_auto(idx: int):
    label, func = AUTO_STEPS[idx]
    ok, msg = func()
    parts = [_tick(ok, label, msg)]
    if ok and idx + 1 < len(AUTO_STEPS):
        parts.append(_pending(idx + 1))
    elif ok:
        parts.append(P("✓ Auto-sync complete.", cls="text-success font-bold mt-2"))
    else:
        parts.append(P("✗ Sync halted.", cls="text-destructive font-bold mt-2"))
    parts.append(staleness_panel(oob=True))
    return tuple(parts)


@rt("/sync/pull-kodi")
def sync_pull_kodi():
    ok, msg = pull_kodi_step()
    return _tick(ok, "from Kodi", msg), staleness_panel(oob=True)


@rt("/sync/pull-jelly")
def sync_pull_jelly():
    ok, msg = pull_jelly_step()
    return _tick(ok, "from Jellyfin", msg), staleness_panel(oob=True)


@rt("/sync/refresh-kodi-library")
def sync_refresh_kodi_library():
    ok, msg = kodi_library_scan_step()
    return _tick(ok, "Kodi library", msg)


@rt("/sync/refresh-jelly-library")
def sync_refresh_jelly_library():
    ok, msg = jelly_library_refresh_step()
    return _tick(ok, "Jellyfin library", msg)


@rt("/sync/push-jelly")
def sync_push_jelly():
    ok, msg = push_kodi_to_jelly_step()
    return _tick(ok, "to Jellyfin", msg)


@rt("/sync/push-kodi")
def sync_push_kodi():
    ok, msg = push_jelly_to_kodi_step()
    return _tick(ok, "to Kodi", msg)


@rt("/rename")
def rename(current_file: str, proposed: str):
    logger.debug("/rename: request received current_file='%s' proposed='%s'", current_file, proposed)
    ok, message = rename_movie(current_file, proposed)
    logger.debug("/rename: result ok=%s message='%s'", ok, message)
    if ok:
        m = {
            "current_file": proposed,
            "title": "",
            "year": None,
            "ext": "",
            "proposed": proposed,
            "has_metadata": True,
            "exists_on_disk": True,
            "collision": False,
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
                "collision": False,
            },
        )
    return movie_row(m, status=message, ok=ok)


@rt("/movie-delete")
def movie_delete(current_file: str):
    logger.debug("/movie-delete: request received current_file='%s'", current_file)
    ok, message = delete_movie(current_file)
    logger.debug("/movie-delete: result ok=%s message='%s'", ok, message)
    if ok:
        rid = _row_id(current_file)
        return Tr(id=rid, style="display:none")
    m = next(
        (x for x in get_transcoded_movies() if x["current_file"] == current_file),
        {"current_file": current_file, "title": "", "year": None, "ext": "",
         "proposed": "", "has_metadata": False, "exists_on_disk": True, "collision": False},
    )
    return movie_row(m, status=message, ok=False)


@rt("/refresh")
def refresh():
    logger.info("Refreshing Jellyfin data via jelly_pull()")
    try:
        jelly_util.jelly_pull()
    except Exception as e:  # noqa: BLE001
        logger.error("jelly_pull failed: %s", e)
        return Div(
            P(f"Refresh failed: {e}", cls="text-destructive"),
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
