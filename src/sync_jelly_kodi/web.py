"""FastHTML UI to rename misnamed TRANSCODED movies using Jellyfin metadata.

Lists movies under the TRANSCODED root whose filenames don't follow the kodidash
``Title_(YEAR).ext`` convention, shows the canonical name derived from Jellyfin's
title/year, and lets the user rename the file in place.

Run via the CLI: ``uv run sync-jelly-kodi web``.
"""
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fasthtml.common import (
    A,
    Button as HtmlButton,
    Div,
    Script,
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

from . import utils
from .movie_archive import archive_movie, get_watched_transcoded_movies
from .movie_rename import delete_movie, get_transcoded_movies, rename_movie_steps
from .sqlite_util import (
    get_audit_operations,
    get_last_pull_times,
    log_audit_step,
    log_audit_steps,
)
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

URL_PREFIX = os.getenv("URL_PREFIX", "").rstrip("/")

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

def _prefix_script():
    if not URL_PREFIX:
        return ()
    return (Script(f"""
        document.addEventListener("htmx:configRequest", function(evt) {{
            if (evt.detail.path.startsWith("/")) {{
                evt.detail.path = "{URL_PREFIX}" + evt.detail.path;
            }}
        }});
    """),)


# Light/dark theme toggle (ported from scrapescore). Persists to FrankenUI's
# ``__FRANKEN__`` localStorage key that ``Theme.slate.headers()`` reads on load, and
# swaps the sun/moon glyphs so only one shows. Re-synced on htmx:afterSettle because
# the toggle lives in the staleness header, which is OOB-swapped after pulls.
_theme_toggle_script = Script("""
function _syncTheme() {
    const isDark = document.documentElement.classList.contains('dark');
    // FrankenUI themes via the .dark class, but the CDN-loaded pico.css / daisyUI
    // auto-dark via @media (prefers-color-scheme: dark) unless data-theme is set.
    // Mirror the .dark class onto data-theme so an OS-dark user forced to light
    // doesn't get light-on-white headings and unreadable table text.
    document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
    document.querySelectorAll('.theme-sun').forEach(el => el.classList.toggle('hidden', isDark));
    document.querySelectorAll('.theme-moon').forEach(el => el.classList.toggle('hidden', !isDark));
}
function toggleTheme() {
    const html = document.documentElement;
    const f = JSON.parse(localStorage.getItem('__FRANKEN__') || '{}');
    const nowDark = html.classList.toggle('dark');
    f.mode = nowDark ? 'dark' : 'light';
    localStorage.setItem('__FRANKEN__', JSON.stringify(f));
    _syncTheme();
}
_syncTheme();  // run immediately (head) to set data-theme before first paint
document.addEventListener('DOMContentLoaded', _syncTheme);
document.addEventListener('htmx:afterSettle', _syncTheme);
""")


app, rt = fast_app(
    hdrs=(
        Meta(name="viewport", content="width=device-width, initial-scale=1"),
        *Theme.slate.headers(),
        _htmx_css,
        _theme_toggle_script,
        *_prefix_script(),
    ),
    bodycls="bg-background text-foreground",
)

if URL_PREFIX:
    # Wrap rt so every @rt("/foo") registers at URL_PREFIX + "/foo".
    # Routes live at the prefixed paths; no stripping needed anywhere.
    _rt_orig = rt

    def rt(path="", *args, **kwargs):  # noqa: F811
        return _rt_orig(URL_PREFIX + path, *args, **kwargs)


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
                HtmlButton(
                    "🧹",
                    type="submit", form=rename_fid, title="Rename",
                    style="background:none;border:none;padding:3px;cursor:pointer;font-size:0.85rem;line-height:1",
                    onclick=f"var p=this.form&&this.form.elements['proposed'];return confirm('Rename {escaped} → ' + (p?p.value:'the proposed name') + '?')",
                ),
                HtmlButton(
                    UkIcon("trash-2", cls="h-3 w-3"),
                    type="submit", form=delete_fid,
                    title="Delete file and sidecars",
                    style="background:none;border:none;padding:3px;cursor:pointer;color:#f87171;line-height:1",
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


def movies_table(oob: bool = False) -> Div:
    """Misnamed-movie table. Data comes from the Jellyfin pull, so the header
    "Pull from Jellyfin" icon refreshes this via an out-of-band swap (``oob``)."""
    movies = get_transcoded_movies()
    header = Div(
        Span(f"{len(movies)} misnamed movie(s) in TRANSCODED"),
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
    extra = {"hx_swap_oob": "true"} if oob else {}
    return Div(header, table, id="movies", **extra)


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
        tab("Movie Renamer", f"{URL_PREFIX}/", "renamer"),
        tab("Jelly-Kodi Sync", f"{URL_PREFIX}/sync", "sync"),
        tab("Archiver", f"{URL_PREFIX}/archive", "archive"),
        tab("Audit Log", f"{URL_PREFIX}/audit", "audit"),
        cls="flex gap-1 border-b border-border mb-6",
    )


def page(active: str, *content):
    return (
        Title("Jelly-Kodi Sync"),
        Container(staleness_panel(), tab_nav(active), *content, cls="py-4"),
    )


@rt("/")
def index():
    return page("renamer", movies_table())


# --- Jelly-Kodi Sync tab ----------------------------------------------------------


def _fmt_age(ts: str | None) -> tuple[str, str]:
    """Return (age_text, css_class) for a UTC pull timestamp string.

    Colors: green-ish default (<1 h), amber warning (1–24 h), red stale (>24 h).
    """
    if ts is None:
        return "never", "text-destructive font-semibold"
    try:
        pulled = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except ValueError:
        return ts, "text-muted-foreground"
    secs = max(0, int((datetime.now(timezone.utc) - pulled).total_seconds()))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    text = " ".join(parts) + " ago"
    if secs > 86400:
        cls = "text-destructive font-semibold"
    elif secs > 3600:
        cls = "text-amber-400 font-semibold"
    else:
        cls = "text-muted-foreground"
    return text, cls


def _pull_btn(label: str, route: str) -> HtmlButton:
    """Tiny inline refresh icon button for the staleness bar."""
    return HtmlButton(
        UkIcon("refresh-cw", cls="h-3 w-3"),
        hx_post=route,
        hx_target="#staleness",
        hx_swap="outerHTML",
        hx_disabled_elt="this",
        title=f"Pull from {label}",
        style=(
            "background:none;border:none;padding:2px 3px;cursor:pointer;"
            "line-height:1;vertical-align:middle;opacity:0.6"
        ),
    )


def staleness_panel(oob: bool = False) -> Div:
    """Pull-freshness bar shown on every tab; updated out-of-band after syncs."""
    times = get_last_pull_times()
    kodi_age, kodi_cls = _fmt_age(times["kodi"])
    jelly_age, jelly_cls = _fmt_age(times["jelly"])
    extra = {"hx_swap_oob": "true"} if oob else {}
    return Div(
        Span("Kodi: ", cls="text-muted-foreground"),
        Span(kodi_age, cls=kodi_cls),
        _pull_btn("Kodi", "/pull-kodi"),
        Span("Jellyfin: ", cls="text-muted-foreground ml-3"),
        Span(jelly_age, cls=jelly_cls),
        _pull_btn("Jellyfin", "/pull-jelly"),
        A(
            Span(UkIcon("sun", cls="h-4 w-4"), cls="theme-sun"),
            Span(UkIcon("moon", cls="h-4 w-4"), cls="theme-moon"),
            href="#",
            onclick="toggleTheme(); return false;",
            title="Toggle theme",
            cls="ml-auto flex items-center opacity-60 hover:opacity-100",
        ),
        id="staleness",
        cls="flex flex-wrap items-center gap-x-1 gap-y-0.5 px-3 py-1.5 bg-muted rounded-lg text-xs mb-3",
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


def _pending(idx: int, op_id: str) -> Div:
    """A self-firing placeholder that runs auto-step ``idx`` once inserted.

    ``hx_trigger=load`` posts to the step endpoint the moment this lands in the DOM,
    and the response replaces this element (outerHTML) with the step's tick + the
    next pending placeholder — chaining the steps sequentially without SSE. ``op_id``
    is carried through the chain so every step is audited under one operation.
    """
    label = AUTO_STEPS[idx][0]
    return Div(
        Span(cls="spinner"),
        Span(f" {label}…", cls="ml-2 text-sm text-muted-foreground"),
        id=f"auto-pending-{idx}",
        hx_post=f"/sync/auto/{idx}?op_id={op_id}",
        hx_trigger="load",
        hx_target=f"#auto-pending-{idx}",
        hx_swap="outerHTML",
        cls="my-1 flex items-center",
    )


def _btn(*args, cls="", **kwargs):
    """Compact button used across all tabs. ``uk-btn-sm`` shrinks the padding/font;
    inline-flex via style attr beats UIkit's dynamically-injected display:flex!important
    from core.iife.js, keeping buttons content-wide in flex rows."""
    cls = f"{cls} {ButtonT.sm}".strip()
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
            cls=ButtonT.secondary,
            hx_post="/sync/auto/0",
            hx_target="#auto-results",
            hx_swap="innerHTML",
        ),
        Div(id="auto-results", cls="mt-4"),
    )
    manual_section = Div(
        H2("Manual Controls", cls="text-xl font-semibold mt-2 mb-2"),
        P(
            "Use the pull buttons in the header to refresh data, then push in the "
            "direction you want. Pushes compare whatever is currently in the database.",
            cls="mb-3",
        ),
        Div(
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
            cls="flex flex-wrap gap-2 mb-4",
        ),
        P("Initiate Library Scans:", cls="text-sm font-semibold mb-2"),
        Div(
            _btn(
                UkIcon("refresh-cw", cls="mr-1 h-4 w-4"), "Kodi library",
                cls=ButtonT.secondary,
                hx_post="/sync/refresh-kodi-library",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            _btn(
                UkIcon("refresh-cw", cls="mr-1 h-4 w-4"), "Jellyfin library",
                cls=ButtonT.secondary,
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
    return Div(auto_section, Hr(), manual_section)


@rt("/sync")
def sync_page():
    return page("sync", sync_tab())


@rt("/sync/auto/{idx}")
def sync_auto(idx: int, op_id: str = ""):
    # A fresh run starts at idx 0 with no op_id; mint one and carry it through the chain.
    if not op_id:
        op_id = uuid.uuid4().hex[:12]
    label, func = AUTO_STEPS[idx]
    ok, msg = func()
    log_audit_step(op_id, "sync", "auto-sync", idx, label, ok, msg)
    parts = [_tick(ok, label, msg)]
    if ok and idx + 1 < len(AUTO_STEPS):
        parts.append(_pending(idx + 1, op_id))
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


@rt("/pull-kodi")
def pull_kodi_header():
    pull_kodi_step()
    return staleness_panel()


@rt("/pull-jelly")
def pull_jelly_header():
    pull_jelly_step()
    # Also refresh the renamer table (Jellyfin data drives it) via OOB swap; the
    # swap is dropped harmlessly on tabs where #movies isn't in the DOM.
    return staleness_panel(), movies_table(oob=True)


@rt("/rename")
def rename(current_file: str, proposed: str):
    logger.debug("/rename: request received current_file='%s' proposed='%s'", current_file, proposed)
    steps = rename_movie_steps(current_file, proposed)
    op_id = uuid.uuid4().hex[:12]
    log_audit_steps(op_id, "rename", current_file, steps)
    rid = _row_id(current_file)
    return _steps_result_row(
        rid, steps, ncols=5,
        success_msg=f"Renamed to {proposed}.",
        fail_msg="Rename incomplete — see the state notes above for manual recovery.",
    )


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


# --- Archiver tab -----------------------------------------------------------------


def _archive_row_id(current_file: str) -> str:
    return "archrow-" + hashlib.md5(current_file.encode("utf-8")).hexdigest()[:12]


def archive_row(m: dict) -> Tr:
    rid = _archive_row_id(m["current_file"])

    watch_badges = Div(
        *(
            [Span("Jelly", cls="text-xs bg-blue-900 text-blue-200 px-1 rounded")]
            if m["jelly_watched"] else []
        ),
        *(
            [Span("Kodi", cls="text-xs bg-green-900 text-green-200 px-1 rounded")]
            if m["kodi_watched"] else []
        ),
        cls="flex gap-1",
    )

    if m["needs_rename"]:
        action_cell = Span("⚠ Rename first", cls="text-yellow-400 text-sm",
                           title="Go to Movie Renamer tab to rename this file")
    elif not m["exists_on_disk"]:
        action_cell = Span("⚠ Not found on disk", cls="text-destructive text-sm")
    else:
        escaped = m["current_file"].replace("'", "\\'")
        archive_fid = f"farchive-{rid}"
        action_cell = Div(
            HtmlButton(
                UkIcon("archive", cls="h-3 w-3"),
                type="submit", form=archive_fid,
                title="Archive movie and sidecars",
                style="background:none;border:none;padding:3px;cursor:pointer;color:#60a5fa;line-height:1",
                onclick=f"return confirm('Archive {escaped} to ARCHIVE directory?')",
            ),
            Form(
                Hidden(name="current_file", value=m["current_file"]),
                id=archive_fid,
                hx_post="/archive/do",
                hx_target=f"#{rid}",
                hx_swap="outerHTML",
                hx_disabled_elt="find button",
            ),
        )

    return Tr(
        Td(m["current_file"], data_label="Filename"),
        Td(m["title"] or "—", data_label="Title"),
        Td(str(m["year"]) if m["year"] else "—", data_label="Year"),
        Td(watch_badges, data_label="Watched by"),
        Td(action_cell, data_label="Action"),
        id=rid,
    )


def archive_table() -> Div:
    archive_root = os.getenv("ARCHIVE", "")
    if not archive_root:
        return Div(
            P(
                "⚠ ARCHIVE environment variable is not configured. "
                "Set it to the local path of your archive directory.",
                cls="text-destructive",
            ),
            id="archive-movies",
        )

    movies = get_watched_transcoded_movies()
    header = Div(
        Span(f"{len(movies)} fully-watched movie(s) in TRANSCODED"),
        cls="flex flex-wrap items-center gap-2 mb-4",
    )
    if not movies:
        return Div(
            header,
            P("No fully-watched movies found in TRANSCODED.", cls="text-muted-foreground"),
            id="archive-movies",
        )

    table = Div(
        Table(
            Thead(Tr(
                Th("Filename"), Th("Title"), Th("Year"), Th("Watched by"), Th("Action"),
            )),
            Tbody(*[archive_row(m) for m in movies]),
            cls="uk-table uk-table-divider uk-table-hover uk-table-small",
        ),
        cls="table-scroll",
    )
    note = P(
        "After archiving, run a Kodi library scan to remove the old entry from Kodi's library.",
        cls="text-muted-foreground text-sm mt-4",
    )
    return Div(header, table, note, id="archive-movies")


def _step_row(s: dict) -> P:
    """Render one step. Accepts either a backend step dict (``label``) or an audit
    row (``step_label``)."""
    label = s.get("label", s.get("step_label", ""))
    detail = s.get("detail", "")
    state = s.get("current_state", "")
    icon = "✓" if s["ok"] else "✗"
    cls = "text-success" if s["ok"] else "text-destructive"
    parts = [Span(f"{icon} ", cls=f"{cls} font-bold"), Strong(label)]
    if detail:
        parts.append(f" — {detail}")
    if not s["ok"] and state:
        parts.append(Span(f" [{state}]", cls="text-xs text-muted-foreground ml-1"))
    return P(*parts, cls="my-0.5 text-sm")


def _steps_result_row(rid: str, steps: list, ncols: int, success_msg: str, fail_msg: str) -> Tr:
    """Replace a table row (id=``rid``) with a full-width step-by-step result.

    Shared by the rename and archive actions so both show identical, transparent
    step lists ending in a success or failure summary.
    """
    all_ok = all(s["ok"] for s in steps)
    rows = [_step_row(s) for s in steps]
    if all_ok:
        rows.append(P(Span("✓ ", cls="text-success font-bold"), success_msg,
                      cls="text-success text-sm mt-1 font-semibold"))
    else:
        rows.append(P(Span("✗ ", cls="text-destructive font-bold"), fail_msg,
                      cls="text-destructive text-sm mt-1 font-semibold"))
    return Tr(Td(*rows, colspan=str(ncols)), id=rid)


@rt("/archive")
def archive_page():
    return page("archive", archive_table())


@rt("/archive/do")
def archive_do(current_file: str):
    logger.debug("/archive/do: current_file='%s'", current_file)
    steps = archive_movie(current_file)
    op_id = uuid.uuid4().hex[:12]
    log_audit_steps(op_id, "archive", current_file, steps)
    rid = _archive_row_id(current_file)
    return _steps_result_row(
        rid, steps, ncols=5,
        success_msg="Archive complete. Run a Kodi library scan to clean up the old entry.",
        fail_msg="Archive incomplete — see the state notes above for manual recovery.",
    )


# --- Audit Log tab ----------------------------------------------------------------


def _audit_op_card(op: dict):
    failed = next((s for s in op["steps"] if not s["ok"]), None)
    if op["ok"]:
        summary = Span(f"✓ OK ({len(op['steps'])} steps)",
                       cls="text-success font-semibold text-sm")
    else:
        summary = Span(f"✗ FAILED at '{failed['step_label']}'",
                       cls="text-destructive font-semibold text-sm")
    head = Div(
        Span(op["timestamp"] or "", cls="text-muted-foreground text-xs whitespace-nowrap"),
        Span(op["action"], cls="uk-badge"),
        Span(op["target"] or "", cls="text-sm font-mono"),
        summary,
        cls="flex flex-wrap items-center gap-x-3 gap-y-1 mb-1",
    )
    body = Div(*[_step_row(s) for s in op["steps"]], cls="ml-2 pl-3 border-l border-border")
    return Div(head, body, cls="py-3 border-b border-border")


def audit_tab() -> Div:
    ops = get_audit_operations(limit=50)
    header = Div(
        Span(f"{len(ops)} recent operation(s)"),
        Span("(UTC)", cls="text-muted-foreground text-xs ml-2"),
        cls="flex items-center gap-2 mb-4",
    )
    if not ops:
        return Div(header,
                   P("No rename, archive, or sync operations recorded yet.",
                     cls="text-muted-foreground"),
                   id="audit")
    return Div(header, *[_audit_op_card(op) for op in ops], id="audit")


@rt("/audit")
def audit_page():
    return page("audit", audit_tab())


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
