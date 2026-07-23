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
    Li,
    Meta,
    Ol,
    P,
    Span,
    Strong,
    Style,
    Textarea,
    Title,
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
    jelly_transcoded_refresh_step,
    jelly_archive_refresh_step,
    mark_archive_watched_step,
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

# Only what FrankenUI/Tailwind can't handle: HTMX indicator + spinner.
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
    #staleness button.htmx-request svg {
        animation: spin 0.6s linear infinite;
        transform-origin: center;
    }
    /* Brighter red for error/warning text — the default --destructive is too dark,
       especially in dark mode where hsl(0 62.8% 30.6%) is nearly invisible. */
    .text-destructive { color: hsl(0 80% 50%) !important; }
    .dark .text-destructive { color: hsl(0 85% 58%) !important; }
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


def _card_field(label: str, value, cls: str = "") -> Div:
    """A labeled field inside a movie card: small uppercase label above the value.

    ``break-words`` lets long filenames wrap within the card instead of overflowing,
    which is the whole reason we moved off the width-constrained table columns.
    """
    return Div(
        Span(label, cls="block text-[0.7rem] font-bold uppercase tracking-wide text-muted-foreground"),
        Div(value, cls="text-sm break-words"),
        cls=cls,
    )


def movie_card(m: dict, status: str = "", ok: bool | None = None) -> Div:
    """Render one card for a misnamed movie (also used as the rename response).

    Card (not table row) so each movie gets the full container width: the current
    filename and the proposed-name textarea can use the whole card instead of a
    cramped column, and the rename/delete actions sit centered below the name.
    """
    rid = _row_id(m["current_file"])

    flags = []
    if not m["has_metadata"]:
        flags.append(Span("⚠ no Jellyfin year", cls="text-destructive text-sm"))
    if not m["exists_on_disk"]:
        flags.append(Span("⚠ not found on disk", cls="text-destructive text-sm"))
    if m.get("collision"):
        flags.append(Span("⚠ name conflict (duplicate target)", cls="text-destructive text-sm"))

    rename_fid = f"frename-{rid}"
    delete_fid = f"fdelete-{rid}"
    escaped = m["current_file"].replace("'", "\\'")

    # width:fit-content beats pico.css's default `button { width: 100% }`, which would
    # otherwise stretch each button to fill and defeat the centering.
    _action_style = (
        "background:none;border:1px solid hsl(var(--border));border-radius:0.375rem;"
        "padding:5px 22px;cursor:pointer;display:inline-flex;align-items:center;"
        "justify-content:center;line-height:1.2;width:fit-content"
    )
    proposed_block = Div(
        Span("Proposed name", cls="block text-[0.7rem] font-bold uppercase tracking-wide text-muted-foreground mb-1"),
        # A textarea (not a single-line Input) so a long proposed filename wraps and is
        # fully visible instead of clipping and forcing horizontal scroll.
        Textarea(
            m["proposed"],
            name="proposed",
            placeholder="Title_(YEAR).ext",
            form=rename_fid,
            rows=2,
            cls="uk-textarea w-full min-w-0 resize-y",
        ),
        Div(
            HtmlButton(
                "🧹",
                type="submit", form=rename_fid, title="Rename",
                style=_action_style + ";font-size:1rem",
                onclick=f"var p=this.form&&this.form.elements['proposed'];return confirm('Rename {escaped} → ' + (p?p.value:'the proposed name') + '?')",
            ),
            HtmlButton(
                UkIcon("trash-2", cls="h-4 w-4"),
                type="submit", form=delete_fid,
                title="Delete file and sidecars",
                style=_action_style + ";color:#f87171",
                onclick=f"return confirm('Delete {escaped} and all its sidecars?')",
            ),
            cls="flex justify-center gap-4 mt-2",
        ),
        *([Div(*flags, cls="flex flex-col gap-0.5 mt-2")] if flags else []),
    )

    body = [
        _card_field("Current filename", m["current_file"]),
        Div(
            _card_field("Jellyfin title", m["title"] or "—"),
            _card_field("Year", str(m["year"]) if m["year"] else "—"),
            cls="grid grid-cols-2 gap-3",
        ),
        proposed_block,
    ]
    if status:
        icon, scls = (
            ("✓ ", "text-success") if ok
            else ("✗ ", "text-destructive") if ok is False
            else ("", "")
        )
        body.append(Div(Span(f"{icon}{status}", cls=f"text-sm {scls}".strip())))
    body.append(Form(Hidden(name="current_file", value=m["current_file"]),
                     id=rename_fid, hx_post="/rename",
                     hx_target=f"#{rid}", hx_swap="outerHTML"))
    body.append(Form(Hidden(name="current_file", value=m["current_file"]),
                     id=delete_fid, hx_post="/movie-delete",
                     hx_target=f"#{rid}", hx_swap="outerHTML"))

    return Div(*body, id=rid, cls="border border-border rounded-lg p-4 flex flex-col gap-3")


def movies_list(oob: bool = False) -> Div:
    """Misnamed-movie cards. Data comes from the Jellyfin pull, so the header
    "Pull from Jellyfin" icon refreshes this via an out-of-band swap (``oob``).

    A responsive card grid (1 column on phones, 2 on large screens) rather than a
    table, so each movie's filename/proposed-name gets real width."""
    movies = get_transcoded_movies()
    header = Div(
        Span(f"{len(movies)} misnamed movie(s) in TRANSCODED"),
        cls="flex flex-wrap items-center gap-2 mb-4",
    )
    cards = Div(
        *[movie_card(m) for m in movies],
        cls="grid grid-cols-1 lg:grid-cols-2 gap-4",
    )
    extra = {"hx_swap_oob": "true"} if oob else {}
    return Div(header, cards, id="movies", **extra)


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
        # Desktop only; on mobile the tabs live in a fixed bottom bar (see
        # mobile_tab_nav) so they never scroll off-screen.
        cls="hidden md:flex gap-1 border-b border-border mb-6",
    )


# Shared tab definitions: (key, desktop label, mobile label, href, lucide icon).
_TABS = [
    ("renamer", "Movie Renamer", "Renamer", "/", "film"),
    ("sync", "Jelly-Kodi Sync", "Sync", "/sync", "arrow-left-right"),
    ("archive", "Archiver", "Archiver", "/archive", "archive"),
    ("audit", "Audit Log", "Audit", "/audit", "history"),
]


def mobile_tab_nav(active: str) -> Div:
    """Bottom-anchored tab bar for mobile (hidden on md+).

    Fixed to the bottom of the viewport so tabs stay reachable and never scroll
    off. Mirrors the scrapescore bottom-nav pattern: icon over a tiny label, the
    active tab tinted with the primary color. Desktop uses ``tab_nav`` instead.
    """

    def item(key: str, mobile_label: str, href: str, icon: str) -> A:
        state = (
            "text-primary font-semibold" if active == key else "text-muted-foreground"
        )
        return A(
            UkIcon(icon, cls="h-5 w-5"),
            Span(mobile_label, cls="text-[10px] mt-0.5"),
            href=f"{URL_PREFIX}{href}",
            cls=f"flex-1 flex flex-col items-center py-2 {state}",
        )

    return Div(
        *[item(key, mobile_label, href, icon) for key, _, mobile_label, href, icon in _TABS],
        cls=(
            "md:hidden fixed bottom-0 left-0 right-0 bg-background "
            "border-t border-border flex z-50"
        ),
    )


def page(active: str, *content):
    return (
        Title("Jelly-Kodi Sync"),
        # pb-24 on mobile keeps content clear of the fixed bottom tab bar; md:pb-4
        # restores normal spacing on desktop where the bottom bar is hidden.
        Container(
            staleness_panel(), tab_nav(active), *content, cls="pt-4 pb-24 md:pb-4"
        ),
        mobile_tab_nav(active),
    )


@rt("/")
def index():
    return page("renamer", movies_list())


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


def _pull_btn(label: str, route: str, color: str = "") -> HtmlButton:
    """Tiny inline refresh icon button for the staleness bar."""
    color_style = f"color:{color};" if color else ""
    return HtmlButton(
        UkIcon("refresh-cw", cls="h-3 w-3"),
        hx_post=route,
        hx_target="#staleness",
        hx_swap="outerHTML",
        hx_disabled_elt="this",
        title=f"Pull from {label}",
        style=(
            f"background:none;border:none;padding:2px 3px;cursor:pointer;"
            f"line-height:1;vertical-align:middle;opacity:0.7;{color_style}"
        ),
    )


def _scan_btn(label: str, route: str, color: str = "") -> HtmlButton:
    """Tiny labeled library-scan button for the staleness bar."""
    color_style = f"color:{color};" if color else ""
    return HtmlButton(
        UkIcon("refresh-cw", cls="h-3 w-3"),
        Span(label, cls="ml-0.5 font-bold"),
        hx_post=route,
        hx_target="#header-scan-result",
        hx_swap="innerHTML",
        hx_disabled_elt="this",
        title=f"Scan {label} library",
        style=(
            f"background:none;border:none;padding:2px 4px;cursor:pointer;"
            f"line-height:1;vertical-align:middle;opacity:0.7;"
            f"display:inline-flex;align-items:center;gap:2px;{color_style}"
        ),
    )


def staleness_panel(oob: bool = False, failure_msg: str = "") -> Div:
    """Pull-freshness bar shown on every tab; updated out-of-band after syncs."""
    times = get_last_pull_times()
    kodi_age, kodi_cls = _fmt_age(times["kodi"])
    jelly_age, jelly_cls = _fmt_age(times["jelly"])
    extra = {"hx_swap_oob": "true"} if oob else {}
    error = [Span(f"✗ {failure_msg}", cls="text-destructive ml-2")] if failure_msg else []
    return Div(
        Span("Kodi: ", style="color:#1BBBE9;font-weight:bold"),
        Span(kodi_age, cls=kodi_cls),
        _pull_btn("Kodi", "/pull-kodi", color="#1BBBE9"),
        Span("Jellyfin: ", cls="ml-3", style="color:#AA5CC3;font-weight:bold"),
        Span(jelly_age, cls=jelly_cls),
        _pull_btn("Jellyfin", "/pull-jelly", color="#AA5CC3"),
        *error,
        Span("|", cls="text-muted-foreground mx-1 select-none opacity-30"),
        _scan_btn("Kodi", "/sync/refresh-kodi-library", color="#1BBBE9"),
        _scan_btn("Trans", "/sync/refresh-jelly-transcoded", color="#AA5CC3"),
        _scan_btn("Arch", "/sync/refresh-jelly-archive", color="#AA5CC3"),
        Span(id="header-scan-result"),
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
            "Use the pull and scan buttons in the header to refresh data, then push in "
            "the direction you want. Pushes compare whatever is currently in the database.",
            cls="mb-3",
        ),
        Div(
            _btn(
                UkIcon("shuffle", cls="mr-1 h-4 w-4"),
                Span("to Jellyfin", style="color:#AA5CC3;font-weight:bold"),
                cls=ButtonT.secondary,
                hx_post="/sync/push-jelly",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            _btn(
                UkIcon("shuffle", cls="mr-1 h-4 w-4"),
                Span("to Kodi", style="color:#1BBBE9;font-weight:bold"),
                cls=ButtonT.secondary,
                hx_post="/sync/push-kodi",
                hx_target="#manual-result",
                hx_swap="innerHTML",
                hx_indicator="#manual-spinner",
            ),
            cls="flex flex-wrap gap-2 mb-4",
        ),
        P("Archive maintenance:", cls="text-sm font-semibold mb-2"),
        Div(
            _btn(
                UkIcon("check", cls="mr-1 h-4 w-4"), "Mark Archive Watched",
                cls=ButtonT.secondary,
                hx_post="/sync/mark-archive-watched",
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
    return _tick(ok, "from Kodi", msg), staleness_panel(oob=True, failure_msg="" if ok else msg)


@rt("/sync/pull-jelly")
def sync_pull_jelly():
    ok, msg = pull_jelly_step()
    return _tick(ok, "from Jellyfin", msg), staleness_panel(oob=True, failure_msg="" if ok else msg)


@rt("/sync/refresh-kodi-library")
def sync_refresh_kodi_library():
    ok, msg = kodi_library_scan_step()
    cls = "text-success ml-1" if ok else "text-destructive ml-1"
    return Span(("✓" if ok else "✗") + f" Kodi: {msg}", cls=cls)


@rt("/sync/refresh-jelly-transcoded")
def sync_refresh_jelly_transcoded():
    ok, msg = jelly_transcoded_refresh_step()
    cls = "text-success ml-1" if ok else "text-destructive ml-1"
    return Span(("✓" if ok else "✗") + f" Trans: {msg}", cls=cls)


@rt("/sync/refresh-jelly-archive")
def sync_refresh_jelly_archive():
    ok, msg = jelly_archive_refresh_step()
    cls = "text-success ml-1" if ok else "text-destructive ml-1"
    return Span(("✓" if ok else "✗") + f" Arch: {msg}", cls=cls)


@rt("/sync/mark-archive-watched")
def sync_mark_archive_watched():
    ok, msg = mark_archive_watched_step()
    return _tick(ok, "Mark Archive Watched", msg)


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
    ok, msg = pull_kodi_step()
    return staleness_panel(failure_msg="" if ok else msg)


@rt("/pull-jelly")
def pull_jelly_header():
    ok, msg = pull_jelly_step()
    # Also refresh the renamer table (Jellyfin data drives it) via OOB swap; the
    # swap is dropped harmlessly on tabs where #movies isn't in the DOM.
    return staleness_panel(failure_msg="" if ok else msg), movies_list(oob=True)


@rt("/rename")
def rename(current_file: str, proposed: str):
    # The proposed name now comes from a <textarea>, which can carry stray newlines
    # (wrapping/paste/Enter); a filename must stay single-line, so flatten and trim.
    proposed = " ".join(proposed.split())
    logger.debug("/rename: request received current_file='%s' proposed='%s'", current_file, proposed)
    steps = rename_movie_steps(current_file, proposed)
    op_id = uuid.uuid4().hex[:12]
    log_audit_steps(op_id, "rename", current_file, steps)
    rid = _row_id(current_file)
    return _steps_result_card(
        rid, steps,
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
        return Div(id=rid, style="display:none")
    m = next(
        (x for x in get_transcoded_movies() if x["current_file"] == current_file),
        {"current_file": current_file, "title": "", "year": None, "ext": "",
         "proposed": "", "has_metadata": False, "exists_on_disk": True, "collision": False},
    )
    return movie_card(m, status=message, ok=False)


# --- Archiver tab -----------------------------------------------------------------


def _archive_row_id(current_file: str) -> str:
    return "archrow-" + hashlib.md5(current_file.encode("utf-8")).hexdigest()[:12]


def archive_card(m: dict) -> Div:
    """Render one card for a fully-watched movie eligible for archiving."""
    rid = _archive_row_id(m["current_file"])

    watch_badges = Div(
        *(
            [Span("Jellyfin", cls="text-xs font-bold px-1.5 py-0.5 rounded",
                  style="background:rgba(170,92,195,0.2);color:#AA5CC3")]
            if m["jelly_watched"] else []
        ),
        *(
            [Span("Kodi", cls="text-xs font-bold px-1.5 py-0.5 rounded",
                  style="background:rgba(27,187,233,0.2);color:#1BBBE9")]
            if m["kodi_watched"] else []
        ),
        cls="flex gap-1",
    )

    if m["needs_rename"]:
        action = Span("⚠ Rename first", cls="text-yellow-400 text-sm",
                      title="Go to Movie Renamer tab to rename this file")
    elif not m["exists_on_disk"]:
        action = Span("⚠ Not found on disk", cls="text-destructive text-sm")
    else:
        escaped = m["current_file"].replace("'", "\\'")
        archive_fid = f"farchive-{rid}"
        action = Div(
            HtmlButton(
                UkIcon("archive", cls="h-4 w-4 mr-1"), "Archive",
                type="submit", form=archive_fid,
                title="Archive movie and sidecars",
                # width:fit-content beats pico's `button { width: 100% }` (see movie_card).
                style="background:none;border:1px solid hsl(var(--border));border-radius:0.375rem;"
                      "padding:5px 22px;cursor:pointer;color:#60a5fa;display:inline-flex;"
                      "align-items:center;line-height:1.2;width:fit-content",
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

    return Div(
        _card_field("Filename", m["current_file"]),
        Div(
            _card_field("Title", m["title"] or "—"),
            _card_field("Year", str(m["year"]) if m["year"] else "—"),
            cls="grid grid-cols-2 gap-3",
        ),
        _card_field("Watched by", watch_badges),
        Div(action, cls="flex justify-center mt-1"),
        id=rid,
        cls="border border-border rounded-lg p-4 flex flex-col gap-3",
    )


def archive_list() -> Div:
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

    cards = Div(
        *[archive_card(m) for m in movies],
        cls="grid grid-cols-1 lg:grid-cols-2 gap-4",
    )
    note = P(
        "After archiving, run a Kodi library scan to remove the old entry from Kodi's library.",
        cls="text-muted-foreground text-sm mt-4",
    )
    return Div(header, cards, note, id="archive-movies")


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


def _steps_result_card(rid: str, steps: list, success_msg: str, fail_msg: str) -> Div:
    """Replace a movie card (id=``rid``) with a step-by-step result card.

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
    return Div(*rows, id=rid, cls="border border-border rounded-lg p-4")


@rt("/archive")
def archive_page():
    return page("archive", archive_list())


@rt("/archive/do")
def archive_do(current_file: str):
    logger.debug("/archive/do: current_file='%s'", current_file)
    steps = archive_movie(current_file)
    op_id = uuid.uuid4().hex[:12]
    log_audit_steps(op_id, "archive", current_file, steps)
    rid = _archive_row_id(current_file)
    return _steps_result_card(
        rid, steps,
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
