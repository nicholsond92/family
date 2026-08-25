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
from html.parser import HTMLParser
from urllib.parse import urljoin

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


def is_fastdirect(url: str) -> bool:
    """FastDirect (fastdir.com) school sites publish lunch menus as a
    server-rendered CGI calendar page (…/cgi/NNNN/Lunch.pl), not a JSON API."""
    return "fastdir.com" in (url or "").lower()


def valid_menu_url(url: str) -> bool:
    return bool(parse_menu_url(url)) or is_fastdirect(url)


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

    if is_fastdirect(url):
        return fetch_fastdirect_month(url, year, month)

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


# ---- FastDirect (fastdir.com) calendar pages ------------------------------

_FD_MONTHS = {name: i for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_FD_MONTH_RE = re.compile(
    r"\b(" + "|".join(m[:3] + r"(?:" + m[3:] + r")?" for m in _FD_MONTHS)
    + r")\b\W{0,3}(\d{4})", re.I)
_FD_DAY_RE = re.compile(r"^(\d{1,2})(?!\d)")


class _FastDirParser(HTMLParser):
    """Collects table-cell texts (with <br>/<p>/<div> as line breaks), the
    page's plain text, links back to the Lunch.pl script, and the hidden
    fields of the page's own month-nav forms (for replaying their POST).

    Menu day cells hold a nested table (item rows with a price column), so
    cells are tracked as a stack keyed by table depth: a <td> at a deeper
    table nests instead of terminating the day cell, and its text folds back
    into the parent when it closes. A <td>/<tr> at the same depth closes the
    open cell — the site's old HTML omits many closing tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []
        self.hidden: dict[str, str] = {}
        self._stack: list[tuple[int, list[str]]] = []
        self._tdepth = 0

    def _close_top(self):
        _, buf = self._stack.pop()
        text = "".join(buf)
        self.cells.append(text)
        if self._stack:
            self._stack[-1][1].append("\n" + text + "\n")

    def _close_at_depth(self):
        while self._stack and self._stack[-1][0] >= self._tdepth:
            self._close_top()

    def close_all(self):
        while self._stack:
            self._close_top()

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._tdepth += 1
        elif tag in ("td", "th"):
            self._close_at_depth()
            self._stack.append((self._tdepth, []))
        elif tag == "tr":
            self._close_at_depth()
        elif tag in ("br", "p", "div", "li") and self._stack:
            self._stack[-1][1].append("\n")
        elif tag == "a":
            href = next((v for k, v in attrs if k == "href"), "") or ""
            if "lunch" in href.lower():
                self.links.append(href)
        elif tag == "input":
            a = dict(attrs)
            if (a.get("type") or "").lower() == "hidden" and a.get("name"):
                self.hidden[a["name"]] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "table":
            self._close_at_depth()
            self._tdepth = max(0, self._tdepth - 1)
        elif tag in ("td", "th", "tr"):
            self._close_at_depth()
        elif tag in ("p", "div", "li") and self._stack:
            self._stack[-1][1].append("\n")

    def handle_data(self, data):
        self.text.append(data)
        if self._stack:
            self._stack[-1][1].append(data)


def parse_fastdirect(page: str):
    """(days, (year, month) or None, month_nav_links, hidden_form_fields)
    from a FastDirect lunch calendar page. days is {day_of_month: [item, …]}
    — a cell counts as a menu day when its text starts with a day number
    followed by lines of item text. Fewer than 3 such cells means this
    wasn't a calendar."""
    parser = _FastDirParser()
    parser.feed(page)
    parser.close_all()
    m = _FD_MONTH_RE.search(" ".join(parser.text))
    year_month = None
    if m:
        for name, num in _FD_MONTHS.items():
            if name.startswith(m.group(1).lower()):
                year_month = (int(m.group(2)), num)
                break
    days: dict[int, list[str]] = {}
    for cell in parser.cells:
        lines = [ln.strip() for ln in cell.split("\n") if ln.strip()]
        if not lines:
            continue
        dm = _FD_DAY_RE.match(lines[0])
        if not dm or not 1 <= int(dm.group(1)) <= 31:
            continue
        day = int(dm.group(1))
        rest = lines[0][dm.end():].strip(" .:-–—")
        items = []
        for line in [rest] + lines[1:]:
            line = re.sub(r"\s+", " ", line).strip()
            # Drop a trailing price ("Chicken Sandwich 2.00" / price-only
            # lines from the menu's price column).
            line = re.sub(r"[\s,]*\$?\d+\.\d{2}$", "", line).strip()
            # A day's foods come crammed into one comma-run line; split so
            # each food is its own item (and the Hide-items filter matches
            # one food, not the whole line).
            for item in line.split(","):
                item = item.strip()
                if any(c.isalpha() for c in item) and len(item) < 80:
                    items.append(item)
        if items and day not in days:
            days[day] = items
    if len(days) < 3:
        days = {}
    return days, year_month, list(dict.fromkeys(parser.links)), parser.hidden


def _fastdir_get(url: str, attempts: list, post_data: dict | None = None):
    import requests

    try:
        headers = {"User-Agent": "FamilyHub/1.0 (+lunch menu display)"}
        if post_data is None:
            resp = requests.get(url, timeout=8, headers=headers)
        else:
            resp = requests.post(url, data=post_data, timeout=8,
                                 headers=headers)
        body = resp.text
        # Excerpt from the calendar table, not the boilerplate head.
        start = max(body.lower().find("<table"), 0)
        attempts.append({"endpoint": url + (f" POST {post_data}" if post_data
                                            else ""),
                         "status": resp.status_code,
                         "excerpt": body[start:start + 900]})
        return body if resp.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001 — record and move on
        attempts.append({"endpoint": url, "status": "error",
                         "excerpt": repr(exc)[:300]})
        return None


def fetch_fastdirect_month(url: str, year: int, month: int):
    """(lunches_by_date, attempts). The page shows one month at a time; its
    prev/next controls are forms submitting ReqYR/ReqMO back to Lunch.pl, so
    other months are requested with those parameters (the CGI reads them from
    GET or POST). Any Lunch.pl hrefs on the page are kept as a fallback."""
    attempts: list = []
    found_links: list[str] = []
    form_fields: dict[str, str] = {}

    def try_page(page):
        if page is None:
            return None
        days, year_month, links, hidden = parse_fastdirect(page)
        found_links.extend(links)
        for key, value in hidden.items():
            form_fields.setdefault(key, value)
        # Say what came back so the test page is diagnosable without
        # digging through raw HTML excerpts.
        shows = (date(year_month[0], year_month[1], 1).strftime("%B %Y")
                 if year_month else "no recognizable month")
        attempts[-1]["note"] = f"page shows {shows}, {len(days)} menu day(s)"
        # A page with no readable month header is trusted only for the
        # month the site defaults to — the current one.
        trusted = (year_month == (year, month) or (
            year_month is None
            and (year, month) == (date.today().year, date.today().month)))
        if not days or not trusted:
            return None
        attempts[-1]["parsed_days"] = len(days)
        out = {}
        for day, items in days.items():
            try:
                out[date(year, month, day).isoformat()] = items
            except ValueError:
                continue
        return out

    out = try_page(_fastdir_get(url, attempts))
    if out is not None:
        return out, attempts
    sep = "&" if "?" in url else "?"
    out = try_page(_fastdir_get(f"{url}{sep}ReqYR={year}&ReqMO={month}",
                                attempts))
    if out is not None:
        return out, attempts
    # Replay the page's own month-nav form: all its hidden fields, with the
    # month we want. Fall back to the bare minimum when none were found.
    post = {**form_fields, "ReqYR": str(year), "ReqMO": str(month)}
    post.setdefault("PassActiveTest", "0")
    out = try_page(_fastdir_get(url, attempts, post_data=post))
    if out is not None:
        return out, attempts
    seen = {url}
    for link in found_links[:5]:
        full = urljoin(url, link)
        if full in seen:
            continue
        seen.add(full)
        out = try_page(_fastdir_get(full, attempts))
        if out is not None:
            return out, attempts
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


def _is_no_school(item: str) -> bool:
    """Calendar placeholders like "No School" / "No School - Labor Day" are
    kept when parsing (so the test page reports what a month really holds)
    but never shown as a lunch. Always filtered, independent of the
    user-editable ignore list."""
    return item.strip().lower().startswith("no school")


def prettify_item(item: str) -> str:
    """Title-case shouting menu text; leave mixed-case text alone. Short
    all-caps tokens (BBQ, PBJ) are preserved. Cramped punctuation from
    school menus ("Macaroni&Cheese,Green Beans") gets friendly spacing."""
    item = re.sub(r",(?=\S)", ", ", item)
    item = re.sub(r"\s*&\s*", " & ", item)
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
                if not _is_no_school(item)
                and not any(term in item.lower() for term in terms)
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
