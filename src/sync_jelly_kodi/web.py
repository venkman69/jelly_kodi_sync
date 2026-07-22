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
    Button,
    Div,
    Form,
    H2,
    Hidden,
    Hr,
    Input,
    Li,
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
    Titled,
    Tr,
    fast_app,
)

from . import jelly_util, utils
from .movie_rename import get_transcoded_movies, rename_movie
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

# Spinner shown while a "Refresh from Jellyfin" request is in flight. HTMX hides
# elements with class ``htmx-indicator`` by default and reveals them for the
# duration of the request that names them via ``hx-indicator``.
_spinner_css = Style(
    """
    .tab-link {
        padding: 0.5rem 1.2rem;
        text-decoration: none;
        font-size: 1rem;
        font-weight: 500;
        border-bottom: 3px solid transparent;
        color: #555;
    }
    .tab-link.active {
        font-weight: bold;
        border-bottom-color: currentColor;
        color: #111;
    }
    @media (prefers-color-scheme: dark) {
        .tab-link        { color: #aaa; }
        .tab-link.active { color: #fff; }
    }
    .htmx-indicator {
        display: none;
        margin-left: 0.5rem;
        vertical-align: middle;
    }
    .htmx-request .htmx-indicator,
    .htmx-request.htmx-indicator { display: inline-flex; }
    .spinner {
        display: inline-block;
        width: 1rem;
        height: 1rem;
        border: 2px solid #ccc;
        border-top-color: #333;
        border-radius: 50%;
        animation: spin 0.6s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    """
)

app, rt = fast_app(hdrs=[_spinner_css])


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
    if m.get("collision"):
        flags.append(
            Span(" ⚠ name conflict (duplicate target)", style="color:#b00")
        )

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
            hx_indicator="#refresh-spinner",
            style="margin-left:1rem",
        ),
        Span(
            Span(cls="spinner"),
            Span(" Refreshing…", style="margin-left:0.4rem"),
            id="refresh-spinner",
            cls="htmx-indicator",
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


def tab_nav(active: str) -> Div:
    """Top navigation shared by every tab; highlights the active one."""

    def tab(label: str, href: str, key: str) -> A:
        cls = "tab-link active" if active == key else "tab-link"
        return A(label, href=href, cls=cls)

    return Div(
        tab("Movie Renamer", "/", "renamer"),
        tab("Jelly-Kodi Sync", "/sync", "sync"),
        style="display:flex; gap:0.5rem; border-bottom:1px solid #ccc; margin-bottom:1.5rem",
    )


def page(active: str, *content) -> Titled:
    return Titled("Jelly-Kodi Sync", tab_nav(active), *content)


@rt("/")
def index():
    return page("renamer", movies_table())


# --- Jelly-Kodi Sync tab ----------------------------------------------------------


def _fmt_ts(ts: str | None) -> str:
    return ts if ts else "never"


def staleness_panel(oob: bool = False) -> Div:
    """Show when each side's data was last pulled, so the user can judge staleness.

    When ``oob`` is set, the panel replaces the existing ``#staleness`` element via
    an out-of-band swap after a pull refreshes the data.
    """
    times = get_last_pull_times()
    extra = {"hx_swap_oob": "true"} if oob else {}
    return Div(
        Strong("Data freshness — "),
        Span("Kodi last pulled: "),
        Strong(_fmt_ts(times["kodi"])),
        Span("     Jellyfin last pulled: ", style="margin-left:1rem"),
        Strong(_fmt_ts(times["jelly"])),
        Span("   (UTC)", style="color:#888"),
        id="staleness",
        style="padding:0.6rem; background:#f4f4f4; border-radius:4px; margin-bottom:1.5rem",
        **extra,
    )


def _tick(ok: bool, label: str, msg: str) -> P:
    mark, color = ("✓", "#080") if ok else ("✗", "#b00")
    return P(
        Span(f"{mark} ", style=f"color:{color}"),
        Strong(label),
        f" — {msg}",
        style=f"margin:0.2rem 0;{'' if ok else ' color:#b00;'}",
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
        Span(f" {label}…", style="margin-left:0.4rem"),
        id=f"auto-pending-{idx}",
        hx_post=f"/sync/auto/{idx}",
        hx_trigger="load",
        hx_target=f"#auto-pending-{idx}",
        hx_swap="outerHTML",
        style="margin:0.3rem 0",
    )


def sync_tab() -> Div:
    auto_section = Div(
        H2("Auto Sync"),
        P("Runs the full sync in order; each step starts only after the previous one succeeds:"),
        Ol(*[Li(label) for label, _ in AUTO_STEPS]),
        Button(
            "Auto Sync",
            hx_post="/sync/auto/0",
            hx_target="#auto-results",
            hx_swap="innerHTML",
        ),
        Div(id="auto-results", style="margin-top:1rem"),
    )
    manual_section = Div(
        H2("Manual Controls"),
        P(
            "Pull each side first (check the freshness above), then push in the "
            "direction you want. Pushes compare whatever is currently in the database."
        ),
        Div(
            Button(
                "Pull from Kodi",
                hx_post="/sync/pull-kodi",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            Button(
                "Pull from Jellyfin",
                hx_post="/sync/pull-jelly",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            Button(
                "Compare & push to Jellyfin",
                hx_post="/sync/push-jelly",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            Button(
                "Compare & push to Kodi",
                hx_post="/sync/push-kodi",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            style="display:flex; flex-wrap:wrap; gap:0.5rem; margin-bottom:0.5rem",
        ),
        Div(
            Button(
                "Refresh Kodi library",
                hx_post="/sync/refresh-kodi-library",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            Button(
                "Refresh Jellyfin library",
                hx_post="/sync/refresh-jelly-library",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            style="display:flex; flex-wrap:wrap; gap:0.5rem",
        ),
        Span(
            Span(cls="spinner"),
            Span(" Working…", style="margin-left:0.4rem"),
            id="manual-spinner",
            cls="htmx-indicator",
        ),
        Div(id="manual-result", style="margin-top:1rem"),
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
        parts.append(
            P("✓ Auto-sync complete.", style="color:#080; font-weight:bold; margin-top:0.5rem")
        )
    else:
        parts.append(
            P("✗ Sync halted.", style="color:#b00; font-weight:bold; margin-top:0.5rem")
        )
    # A pull step changes freshness; refresh the staleness panel out-of-band.
    parts.append(staleness_panel(oob=True))
    return tuple(parts)


@rt("/sync/pull-kodi")
def sync_pull_kodi():
    ok, msg = pull_kodi_step()
    return _tick(ok, "Pull from Kodi", msg), staleness_panel(oob=True)


@rt("/sync/pull-jelly")
def sync_pull_jelly():
    ok, msg = pull_jelly_step()
    return _tick(ok, "Pull from Jellyfin", msg), staleness_panel(oob=True)


@rt("/sync/refresh-kodi-library")
def sync_refresh_kodi_library():
    ok, msg = kodi_library_scan_step()
    return _tick(ok, "Refresh Kodi library", msg)


@rt("/sync/refresh-jelly-library")
def sync_refresh_jelly_library():
    ok, msg = jelly_library_refresh_step()
    return _tick(ok, "Refresh Jellyfin library", msg)


@rt("/sync/push-jelly")
def sync_push_jelly():
    # "push to Jellyfin" = write Kodi's watch status into Jellyfin.
    ok, msg = push_kodi_to_jelly_step()
    return _tick(ok, "Compare & push to Jellyfin", msg)


@rt("/sync/push-kodi")
def sync_push_kodi():
    # "push to Kodi" = write Jellyfin's watch status into Kodi.
    ok, msg = push_jelly_to_kodi_step()
    return _tick(ok, "Compare & push to Kodi", msg)


@rt("/rename")
def rename(current_file: str, proposed: str):
    logger.debug("/rename: request received current_file='%s' proposed='%s'", current_file, proposed)
    ok, message = rename_movie(current_file, proposed)
    logger.debug("/rename: result ok=%s message='%s'", ok, message)
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
