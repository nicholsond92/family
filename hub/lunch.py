"""School lunch menus from Health-e Pro / My School Menus.

The published menu site (menus.healthepro.com, formerly myschoolmenus.com) is
a single-page app backed by an undocumented JSON API. There's no official
integration, so this module:

- accepts the menu page URL a parent copies from their browser
  (e.g. https://menus.healthepro.com/organizations/99/sites/760/menus/104901),
- tries the known API endpoint shapes on both hostnames,
- parses defensively: the response historically carries a
  ``menu_month_calendar`` list (sometimes JSON-encoded as a string) of day
  entries whose display items live under varying keys.

Results are cached in the settings table for a few hours. Everything fails
soft — a broken menu source never breaks the wall display. The Settings page
links a live test endpoint that shows exactly what the API returned, so the
parser can be adjusted if Health-e Pro changes shapes.
"""

import json
import re
from datetime import date, datetime

from . import db

URL_RE = re.compile(
    r"/organizations/(?P<org>\d+)(?:/sites/(?P<site>\d+))?(?:/menus/(?P<menu>\d+))?"
)

CACHE_TTL_SECONDS = 6 * 60 * 60
_NAME_KEYS = ("name", "recipe_name", "item_name", "text", "title")
_DATE_KEYS = ("day", "date", "menu_date", "serve_date")
_ITEM_LIST_KEYS = (
    "current_display", "overwritten_display", "items", "recipes", "menu_items",
)


def parse_menu_url(url: str):
    """(org, site, menu) ids from a pasted menu page URL, or None."""
    m = URL_RE.search(url or "")
    if not m or not m.group("org"):
        return None
    return m.group("org"), m.group("site"), m.group("menu")


def candidate_endpoints(url: str, year: int, month: int) -> list[str]:
    """Month-data candidates first; the plain menu endpoint (which returns
    only metadata, as observed live) last as a shape probe."""
    parsed = parse_menu_url(url)
    if not parsed:
        return []
    org, site, menu = parsed
    if not menu:
        return []
    first = f"{year}-{month:02d}-01"
    hosts = ["https://menus.healthepro.com", "https://myschoolmenus.com"]
    out = []
    for host in hosts:
        base = f"{host}/api/organizations/{org}/menus/{menu}"
        out.append(f"{base}/year/{year}/month/{month}/date_overwrites")
        out.append(f"{base}/year/{year}/month/{month}")
        out.append(f"{base}/months/{first}")
        out.append(f"{base}/month/{first}")
        out.append(f"{base}/calendar?year={year}&month={month}")
        out.append(f"{base}?year={year}&month={month}")
        out.append(base)
    return out


def _maybe_json(value):
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in ("[", "{"):
            try:
                return json.loads(s)
            except ValueError:
                return None
    return value


def _find_calendar(node):
    """Depth-first search for a menu_month_calendar-like list of day entries."""
    node = _maybe_json(node)
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "menu_month_calendar":
                cal = _maybe_json(value)
                if isinstance(cal, list):
                    return cal
            found = _find_calendar(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        # A list of dicts that carry a date key is itself a calendar.
        if node and all(
            isinstance(e, dict) and any(k in e for k in _DATE_KEYS) for e in node
        ):
            return node
        for value in node:
            found = _find_calendar(value)
            if found is not None:
                return found
    return None


def _entry_date(entry) -> str | None:
    for key in _DATE_KEYS:
        raw = entry.get(key)
        if isinstance(raw, str) and raw:
            try:
                return date.fromisoformat(raw[:10]).isoformat()
            except ValueError:
                continue
    return None


def _collect_names(node, out: list[str]):
    node = _maybe_json(node)
    if isinstance(node, dict):
        kind = node.get("type")
        if kind in ("recipe", "text", None):
            for key in _NAME_KEYS:
                value = node.get(key)
                if isinstance(value, str) and value.strip() and kind is not None:
                    out.append(value.strip())
                    break
        for key in _ITEM_LIST_KEYS + ("setting",):
            if key in node:
                _collect_names(node[key], out)
    elif isinstance(node, list):
        for value in node:
            _collect_names(value, out)


def parse_month(payload) -> dict[str, list[str]]:
    """{iso_date: [item, ...]} from an API response payload."""
    calendar = _find_calendar(payload)
    if not calendar:
        return {}
    out: dict[str, list[str]] = {}
    for entry in calendar:
        if not isinstance(entry, dict):
            continue
        iso = _entry_date(entry)
        if not iso:
            continue
        names: list[str] = []
        _collect_names(entry, names)
        deduped = list(dict.fromkeys(n for n in names if len(n) < 80))
        if deduped:
            out[iso] = deduped
    return out


def fetch_month(url: str, year: int, month: int):
    """(lunches_by_date, attempts) — attempts logged for the debug page."""
    import requests

    attempts = []
    for endpoint in candidate_endpoints(url, year, month):
        try:
            resp = requests.get(
                endpoint, timeout=6,
                headers={"Accept": "application/json",
                         "User-Agent": "FamilyHub/1.0 (+lunch menu display)"},
            )
            body = resp.text
            attempts.append({
                "endpoint": endpoint, "status": resp.status_code,
                "excerpt": body[:600],
            })
            if resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue
            lunches = parse_month(payload)
            if lunches:
                attempts[-1]["parsed_days"] = len(lunches)
                return lunches, attempts
        except Exception as exc:  # noqa: BLE001 — record and move on
            attempts.append({"endpoint": endpoint, "status": "error",
                             "excerpt": repr(exc)[:300]})
    return {}, attempts


_API_ROUTE_RE = re.compile(
    r"[\"'`]((?:[A-Za-z0-9_\-/${}.:]*)?api/[A-Za-z0-9_\-/${}?&=.:]{3,160})[\"'`]"
)
_SCRIPT_SRC_RE = re.compile(r"src=[\"']([^\"']+\.js[^\"']*)[\"']")


def extract_api_routes(text: str) -> list[str]:
    """Distinct api/... route strings referenced by a JS bundle, filtered to
    menu-related ones. Template literals keep their ${var} segments so the
    real path shape is visible."""
    found = {m for m in _API_ROUTE_RE.findall(text)}
    interesting = sorted(
        r for r in found
        if any(k in r.lower() for k in ("menu", "organization", "site", "calendar"))
    )
    return interesting[:80]


def discover_api_routes(url: str):
    """Fetch the menu page and its JS bundles; report every API route the
    site's own code references. Turns the live app into an API prober when
    Health-e Pro changes their undocumented endpoints."""
    import requests

    notes: list[str] = []
    routes: set[str] = set()
    headers = {"User-Agent": "FamilyHub/1.0 (+lunch menu display)"}
    try:
        page = requests.get(url, timeout=8, headers=headers)
        notes.append(f"page: {page.status_code}, {len(page.text)} bytes")
        srcs = _SCRIPT_SRC_RE.findall(page.text)
        origin = url.split("/organizations/")[0].rstrip("/")
        for src in srcs[:6]:
            full = src if src.startswith("http") else f"{origin}/{src.lstrip('/')}"
            try:
                js = requests.get(full, timeout=10, headers=headers)
                notes.append(f"{full}: {js.status_code}, {len(js.text)} bytes")
                if js.status_code == 200:
                    routes.update(extract_api_routes(js.text))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{full}: {exc!r}")
        routes.update(extract_api_routes(page.text))
    except Exception as exc:  # noqa: BLE001
        notes.append(repr(exc))
    return sorted(routes)[:80], notes


# Items filtered off the wall display by default: daily staples that appear
# on every menu and drown out what's actually different each day. Editable
# in Settings (comma-separated, substring match, case-insensitive).
DEFAULT_IGNORE = "milk, variety, ketchup & mustard, condiment"


def ignored_terms(conn) -> list[str]:
    raw = db.get_setting(conn, "lunch_ignore")
    if raw is None:
        raw = DEFAULT_IGNORE
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def prettify_item(item: str) -> str:
    """Title-case shouting menu text; leave mixed-case text alone. Short
    all-caps tokens (BBQ, PBJ) are preserved."""
    letters = [c for c in item if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.8:
        return item

    def fix(match):
        word = match.group(0)
        # Vowel-less short tokens are initialisms (BBQ, PBJ, BLT, W) — keep.
        if len(word) <= 3 and word.isupper() and not set(word) & set("AEIOU"):
            return word
        return word.capitalize()

    return re.sub(r"[A-Za-z]+", fix, item)


def _day_items(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):  # pre-list cache format
        return [s.strip() for s in value.split(",") if s.strip()]
    return []


def _join_labels(labels: list[str]) -> str:
    named = [l for l in labels if l]
    if not named:
        return ""
    if len(named) == 1:
        return named[0]
    return ", ".join(named[:-1]) + " & " + named[-1]


def _merge_identical(entries: list[dict], total_menus: int) -> list[dict]:
    """Collapse menus serving the same lunch into one entry. Schools in the
    same district often share a menu; the board shouldn't repeat itself.
    Comparison ignores item order and case."""
    merged: list[dict] = []
    for entry in entries:
        key = tuple(sorted(p.strip().lower() for p in entry["text"].split(",")))
        match = next((m for m in merged if m["key"] == key), None)
        if match is None:
            merged.append({"key": key, "labels": [entry["label"]],
                           "text": entry["text"]})
        else:
            match["labels"].append(entry["label"])
    every_menu_matches = len(merged) == 1 and len(entries) == total_menus
    return [
        {
            "label": ("All schools" if every_menu_matches
                      else _join_labels(m["labels"])),
            "text": m["text"],
        }
        for m in merged
    ]


def get_menus(conn) -> list[dict]:
    raw = db.get_setting(conn, "lunch_menus", "") or "[]"
    try:
        menus = json.loads(raw)
        return [m for m in menus if isinstance(m, dict) and m.get("url")]
    except ValueError:
        return []


def set_menus(conn, menus: list[dict]) -> None:
    db.set_setting(conn, "lunch_menus", json.dumps(menus))


def lunches_for(conn, dates: list[date]) -> dict[str, list[dict]]:
    """{iso_date: [{label, text}, ...]} for the configured menus, cached."""
    menus = get_menus(conn)
    if not menus or not dates:
        return {}
    months = {(d.year, d.month) for d in dates}
    out: dict[str, list[dict]] = {}
    for i, menu in enumerate(menus):
        label = menu.get("label") or ""
        by_date: dict[str, str] = {}
        for (year, month) in months:
            cache_key = f"lunch_cache:{i}:{year}-{month:02d}"
            cached = None
            raw = db.get_setting(conn, cache_key)
            if raw:
                try:
                    cached = json.loads(raw)
                except ValueError:
                    cached = None
            fresh = (
                cached is not None
                and (datetime.utcnow().timestamp() - cached.get("at", 0))
                < CACHE_TTL_SECONDS
            )
            if not fresh:
                lunches, _ = fetch_month(menu["url"], year, month)
                if lunches or cached is None:
                    cached = {"at": datetime.utcnow().timestamp(), "days": lunches}
                    db.set_setting(conn, cache_key, json.dumps(cached))
                else:
                    # Keep stale data rather than blanking the board.
                    cached["at"] = datetime.utcnow().timestamp()
                    db.set_setting(conn, cache_key, json.dumps(cached))
            by_date.update(cached.get("days", {}))
        terms = ignored_terms(conn)
        for d in dates:
            iso = d.isoformat()
            if iso not in by_date:
                continue
            items = [
                prettify_item(item) for item in _day_items(by_date[iso])
                if not any(term in item.lower() for term in terms)
            ]
            if items:
                out.setdefault(iso, []).append(
                    {"label": label, "text": ", ".join(items)}
                )
    return {
        iso: (_merge_identical(entries, len(menus)) if len(entries) > 1
              else entries)
        for iso, entries in out.items()
    }
