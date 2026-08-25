import json
from datetime import date

from hub import lunch


def test_parse_menu_url_variants():
    assert lunch.parse_menu_url(
        "https://menus.healthepro.com/organizations/99/sites/760/menus/104901"
    ) == ("99", "760", "104901")
    assert lunch.parse_menu_url(
        "https://menus.healthepro.com/organizations/1094"
    ) == ("1094", None, None)
    assert lunch.parse_menu_url("https://example.com/nope") is None


def test_candidate_endpoints_cover_both_hosts():
    urls = lunch.candidate_endpoints(
        "https://menus.healthepro.com/organizations/99/sites/760/menus/104901",
        2026, 8,
    )
    assert any("menus.healthepro.com/api/organizations/99/menus/104901" in u for u in urls)
    assert any("myschoolmenus.com/api/organizations/99/menus/104901" in u for u in urls)
    assert any("year=2026&month=8" in u for u in urls)
    # Month-path candidates come before the metadata-only base endpoint.
    assert "year/2026/month/8" in urls[0] or "year/2026/month/8" in urls[1]
    assert urls.index(
        "https://menus.healthepro.com/api/organizations/99/menus/104901"
    ) > urls.index(
        "https://menus.healthepro.com/api/organizations/99/menus/104901/year/2026/month/8"
    )


def test_extract_api_routes_from_bundle():
    bundle = """
    const a = fetch(`api/organizations/${org}/menus/${menu}/year/${y}/month/${m}`);
    const b = "api/organizations/"+o+"/menus";
    const c = 'api/auth/login';
    const d = `api/menu_month/${id}/calendar`;
    """
    routes = lunch.extract_api_routes(bundle)
    assert "api/organizations/${org}/menus/${menu}/year/${y}/month/${m}" in routes
    assert "api/menu_month/${id}/calendar" in routes
    assert "api/auth/login" not in routes  # not menu-related


def _sample_payload():
    # The historically observed shape: menu_month_calendar is a JSON-encoded
    # string; each day's items hide inside a JSON-encoded "setting".
    calendar = [
        {
            "day": "2026-08-25",
            "setting": json.dumps({
                "current_display": [
                    {"type": "category", "name": "Entree"},
                    {"type": "recipe", "name": "Cheese Pizza"},
                    {"type": "recipe", "name": "Garden Salad"},
                    {"type": "text", "name": "Milk"},
                ]
            }),
        },
        {"day": "2026-08-26", "setting": json.dumps({"current_display": []})},
        {"day": None},
    ]
    return {"data": {"id": 104901, "menu_month_calendar": json.dumps(calendar)}}


def test_parse_month_known_shape():
    lunches = lunch.parse_month(_sample_payload())
    assert lunches == {"2026-08-25": ["Cheese Pizza", "Garden Salad", "Milk"]}


def test_parse_month_plain_lists_and_alt_keys():
    payload = {
        "menu_month_calendar": [
            {"date": "2026-09-01", "items": [
                {"type": "recipe", "recipe_name": "Tacos"},
                {"type": "recipe", "name": "Refried Beans"},
            ]},
        ]
    }
    assert lunch.parse_month(payload) == {"2026-09-01": ["Tacos", "Refried Beans"]}


def test_prettify_item():
    assert lunch.prettify_item("MINI CORN DOGS") == "Mini Corn Dogs"
    assert lunch.prettify_item("TACO MEAT W/BEAN/CHIPS") == "Taco Meat W/Bean/Chips"
    assert lunch.prettify_item("BBQ RIB SANDWICH") == "BBQ Rib Sandwich"
    # Mixed-case input is left alone.
    assert lunch.prettify_item("Cheese Pizza") == "Cheese Pizza"
    # Cramped school-menu punctuation gets friendly spacing.
    assert lunch.prettify_item("Macaroni&Cheese") == "Macaroni & Cheese"
    assert (lunch.prettify_item("Pancakes,Sausage Patty,Hashbrowns,fruit")
            == "Pancakes, Sausage Patty, Hashbrowns, fruit")
    assert lunch.prettify_item("Ketchup & Mustard") == "Ketchup & Mustard"


def test_parse_month_garbage_is_empty():
    assert lunch.parse_month({"data": {"whatever": 1}}) == {}
    assert lunch.parse_month(None) == {}
    assert lunch.parse_month("not json at all") == {}


def test_lunches_for_uses_cache(tmp_path, monkeypatch):
    from hub import db
    conn = db.connect(str(tmp_path / "t.db"))
    lunch.set_menus(conn, [{
        "url": "https://menus.healthepro.com/organizations/99/menus/104901",
        "label": "Elementary",
    }])

    calls = []

    def fake_fetch(url, year, month):
        calls.append((year, month))
        return {"2026-08-25": ["MINI CORN DOGS", "TOSSED SIDE SALAD",
                               "MILK", "VARIETY", "KETCHUP & MUSTARD"]}, []

    monkeypatch.setattr(lunch, "fetch_month", fake_fetch)
    days = [date(2026, 8, 24), date(2026, 8, 25)]
    out = lunch.lunches_for(conn, days)
    # Staples filtered by the default ignore list; shouting title-cased.
    assert out == {"2026-08-25": [
        {"label": "Elementary", "text": "Mini Corn Dogs, Tossed Side Salad",
         "logos": []}
    ]}
    # Second call within the TTL hits the cache, not the network.
    out2 = lunch.lunches_for(conn, days)
    assert out2 == out
    assert len(calls) == 1

    # A custom ignore list applies without refetching.
    db.set_setting(conn, "lunch_ignore", "salad")
    out3 = lunch.lunches_for(conn, days)
    assert out3["2026-08-25"][0]["text"] == (
        "Mini Corn Dogs, Milk, Variety, Ketchup & Mustard"
    )
    conn.close()


def test_lunches_for_reads_old_string_cache(tmp_path):
    from hub import db
    conn = db.connect(str(tmp_path / "t.db"))
    lunch.set_menus(conn, [{"url": "https://menus.healthepro.com/organizations/9/menus/1"}])
    import json as _json
    conn_key = "lunch_cache:0:2026-08"
    db.set_setting(conn, conn_key, _json.dumps({
        "at": 9999999999, "days": {"2026-08-25": "CHEESE PIZZA, MILK"},
    }))
    out = lunch.lunches_for(conn, [date(2026, 8, 25)])
    assert out["2026-08-25"][0]["text"] == "Cheese Pizza"
    conn.close()


def test_lunches_for_merges_schools_with_same_lunch(tmp_path, monkeypatch):
    from hub import db
    conn = db.connect(str(tmp_path / "t.db"))
    lunch.set_menus(conn, [
        {"url": "https://menus.healthepro.com/organizations/1606/menus/1", "label": "Washington"},
        {"url": "https://menus.healthepro.com/organizations/1606/menus/2", "label": "Franklin"},
        {"url": "https://menus.healthepro.com/organizations/1606/menus/3", "label": "Truman"},
    ])

    def fake_fetch(url, year, month):
        if url.endswith("/3"):
            return {
                "2026-08-25": ["MINI CORN DOGS", "MILK"],
                "2026-08-26": ["WALKING TACO"],
            }, []
        # Washington and Franklin share a menu; item order differs on the
        # 26th but it's still the same lunch.
        return {
            "2026-08-25": ["MINI CORN DOGS", "MILK"],
            "2026-08-26": (["CHEESE PIZZA", "CARROTS"] if url.endswith("/1")
                           else ["CARROTS", "CHEESE PIZZA"]),
        }, []

    monkeypatch.setattr(lunch, "fetch_month", fake_fetch)
    out = lunch.lunches_for(conn, [date(2026, 8, 25), date(2026, 8, 26)])

    # All three schools serve the same lunch on the 25th -> one combined line.
    assert out["2026-08-25"] == [
        {"label": "All schools", "text": "Mini Corn Dogs", "logos": []}
    ]
    # On the 26th only Washington & Franklin match; Truman stays separate.
    assert out["2026-08-26"] == [
        {"label": "Washington & Franklin", "text": "Cheese Pizza, Carrots",
         "logos": []},
        {"label": "Truman", "text": "Walking Taco", "logos": []},
    ]
    conn.close()


def test_join_labels():
    assert lunch._join_labels([]) == ""
    assert lunch._join_labels(["", ""]) == ""
    assert lunch._join_labels(["Truman"]) == "Truman"
    assert lunch._join_labels(["A", "B"]) == "A & B"
    assert lunch._join_labels(["A", "", "B", "C"]) == "A, B & C"


FASTDIR_SEPT = """
<html><head><title>FastDirect Communications</title></head><body>
<center><font size=4><b>Hot Lunch Menu</b></font><br>
<b>September, 2026</b>
<FORM ACTION=https://ssl.fastdir.com/~fastdir/cgi/0124/Lunch.pl METHOD=POST><INPUT TYPE=hidden NAME=WOcode VALUE=>
<INPUT TYPE=hidden NAME=LunchStatYear VALUE=2027><INPUT TYPE=hidden NAME=PassActiveTest VALUE=0>
<INPUT TYPE=hidden NAME=ReqYR VALUE=2026><INPUT TYPE=hidden NAME=ReqMO VALUE=8><input type=image src="arrowl.gif"></FORM>
<a href="/~fastdir/cgi/0124/Lunch.pl?Month=8&Year=2026">&lt;&lt; Prev</a>
<a href="/~fastdir/cgi/0124/Lunch.pl?Month=10&Year=2026">Next &gt;&gt;</a>
<table border=1>
<tr><th>Monday</th><th>Tuesday</th><th>Wednesday</th></tr>
<tr>
 <td><b>1</b><br>CHEESE PIZZA<br>Green Beans<br>Milk</td>
 <td><b>2</b><br>Chicken Nuggets<br>Dinner Roll</td>
 <td><b>3</b><br>Walking Taco</td>
</tr>
<tr><td>8<br>No School</td><td>&nbsp;</td><td>10<br>Hot Dog<br>Chips</td></tr>
</table></center></body></html>
"""

FASTDIR_OCT = FASTDIR_SEPT.replace("September, 2026", "October, 2026").replace(
    "CHEESE PIZZA", "PANCAKES &amp; SAUSAGE")


def test_parse_fastdirect_calendar():
    days, year_month, links, hidden = lunch.parse_fastdirect(FASTDIR_SEPT)
    assert year_month == (2026, 9)
    assert days[1] == ["CHEESE PIZZA", "Green Beans", "Milk"]
    assert days[2] == ["Chicken Nuggets", "Dinner Roll"]
    assert days[8] == ["No School"]
    assert days[10] == ["Hot Dog", "Chips"]
    # Weekday headers and empty cells aren't menu days.
    assert set(days) == {1, 2, 3, 8, 10}
    assert any("Month=10" in link for link in links)
    # The month-nav form's hidden fields are captured for POST replay.
    assert hidden["LunchStatYear"] == "2027"
    assert hidden["WOcode"] == ""


def test_parse_fastdirect_rejects_non_calendar():
    days, _, _, _ = lunch.parse_fastdirect(
        "<table><tr><td>1<br>Only</td><td>2<br>Two days</td></tr></table>")
    assert days == {}


def test_fetch_fastdirect_other_months_via_reqyr_reqmo(monkeypatch):
    base = "https://ssl.fastdir.com/~fastdir/cgi/0124/Lunch.pl"
    calls = []

    def fake_get(url, attempts, post_data=None):
        calls.append((url, post_data))
        body = None
        if url == base and post_data is None:
            body = FASTDIR_SEPT
        elif url == f"{base}?ReqYR=2026&ReqMO=10" and post_data is None:
            body = FASTDIR_OCT
        attempts.append({"endpoint": url, "status": 200 if body else 404,
                         "excerpt": ""})
        return body

    monkeypatch.setattr(lunch, "_fastdir_get", fake_get)

    out, _ = lunch.fetch_month(base, 2026, 9)
    assert out["2026-09-01"] == ["CHEESE PIZZA", "Green Beans", "Milk"]

    # October isn't the page shown — the fetcher asks the CGI for it the way
    # the page's own prev/next forms do.
    out, _ = lunch.fetch_month(base, 2026, 10)
    assert out["2026-10-01"] == ["PANCAKES & SAUSAGE", "Green Beans", "Milk"]

    # A month the site won't serve comes back empty, not wrong; the POST and
    # href fallbacks were tried before giving up.
    calls.clear()
    out, _ = lunch.fetch_month(base, 2027, 2)
    assert out == {}
    assert any(post is not None for _, post in calls)
    assert any("Month=10" in url for url, _ in calls)


def test_fetch_fastdirect_post_fallback(monkeypatch):
    base = "https://ssl.fastdir.com/~fastdir/cgi/0124/Lunch.pl"

    posts = []

    def fake_get(url, attempts, post_data=None):
        body = None
        if url == base and post_data is None:
            body = FASTDIR_SEPT
        elif (url == base and post_data
              and post_data.get("ReqMO") == "10"):
            posts.append(post_data)
            body = FASTDIR_OCT
        attempts.append({"endpoint": url, "status": 200 if body else 404,
                         "excerpt": ""})
        return body

    monkeypatch.setattr(lunch, "_fastdir_get", fake_get)
    out, _ = lunch.fetch_month(base, 2026, 10)
    assert out["2026-10-01"] == ["PANCAKES & SAUSAGE", "Green Beans", "Milk"]
    # The POST replays the page's own hidden form fields, ReqYR/ReqMO swapped.
    assert posts and posts[0]["LunchStatYear"] == "2027"
    assert posts[0]["ReqMO"] == "10" and posts[0]["ReqYR"] == "2026"


def test_valid_menu_url():
    assert lunch.valid_menu_url(
        "https://menus.healthepro.com/organizations/1606/menus/122977")
    assert lunch.valid_menu_url("https://ssl.fastdir.com/~fastdir/cgi/0124/Lunch.pl")
    assert not lunch.valid_menu_url("https://example.com/lunch")
    assert not lunch.valid_menu_url("")


def test_no_school_days_never_shown_as_lunch(tmp_path):
    from hub import db
    conn = db.connect(str(tmp_path / "t.db"))
    lunch.set_menus(conn, [{"url": "https://ssl.fastdir.com/~fastdir/cgi/0124/Lunch.pl",
                            "label": "St. Paul"}])
    import json as _json
    conn_key = f"lunch_cache:0:2026-08"
    db.set_setting(conn, conn_key, _json.dumps({
        "at": 9999999999, "days": {
            "2026-08-25": ["No School"],
            "2026-08-26": ["NO SCHOOL - Teacher Inservice"],
            "2026-08-27": ["Cheese Pizza", "Green Beans"],
        },
    }))
    out = lunch.lunches_for(conn, [date(2026, 8, 25), date(2026, 8, 26),
                                   date(2026, 8, 27)])
    # Placeholder-only days produce no lunch line at all.
    assert "2026-08-25" not in out
    assert "2026-08-26" not in out
    assert out["2026-08-27"][0]["text"] == "Cheese Pizza, Green Beans"
    conn.close()


# Mirrors the real St. Paul page structure: menu day cells hold a nested
# table of item rows with a price column, many closing tags omitted.
FASTDIR_NESTED = """
<html><body>
<table width=100% class="ShowPersCalTable1" cellpadding=0 cellspacing=0 border=1 bgcolor=white><tr><td colspan=7 align=center><table class="ShowPersCalTable2" border=0><tr valign=top><td align=center nowrap><FORM ACTION=https://ssl.fastdir.com/~fastdir/cgi/0124/Lunch.pl METHOD=POST><INPUT TYPE=hidden NAME=WOcode VALUE=><INPUT TYPE=hidden NAME=LunchStatYear VALUE=2027><INPUT TYPE=hidden NAME=PassActiveTest VALUE=0><INPUT TYPE=hidden NAME=ReqYR VALUE=2026><INPUT TYPE=hidden NAME=ReqMO VALUE=7><input type=image src="arrowl.gif"></form><font size=6><B>August 2026</B></font></td></tr></table></td></tr>
<tr><td>Sunday<td>Monday<td>Tuesday</tr>
<tr><td>2<td>3 <i>No School</i><td>4 <i>No School</i></tr>
<tr><td>23
<td>24
<table width=100%><tr valign=top><td>Biscuit&amp;Gravy,Sausage,Hashbrown,Fruit,Drink<td align=right>3.75</tr><tr><td>PBJ Tray/Grilled Cheese Tray<td align=right>3.75</tr><tr><td>Chicken Sandwich<td align=right>2.00</tr></table>
<td>25
<table width=100%><tr valign=top><td>Macaroni&amp;Cheese,Green Beans,Roll,Fruit,Drink<td align=right>3.75</tr><tr><td>Chicken Sandwich Tray<td align=right>4.00</tr></table>
</tr>
</table>
<font size=2>**************Large Side $1.25 // Small Side $0.75 // Water or Milk $0.50</font>
</body></html>
"""


def test_parse_fastdirect_nested_menu_tables():
    days, year_month, _, hidden = lunch.parse_fastdirect(FASTDIR_NESTED)
    assert year_month == (2026, 8)
    assert days[3] == ["No School"]
    # Nested item tables fold into the day cell; price column dropped;
    # comma-run lines split into one item per food.
    assert days[24] == ["Biscuit&Gravy", "Sausage", "Hashbrown", "Fruit",
                        "Drink", "PBJ Tray/Grilled Cheese Tray",
                        "Chicken Sandwich"]
    assert days[25] == ["Macaroni&Cheese", "Green Beans", "Roll", "Fruit",
                        "Drink", "Chicken Sandwich Tray"]
    assert set(days) == {3, 4, 24, 25}
    assert hidden["LunchStatYear"] == "2027"


def test_school_badge():
    from hub.app import school_badge
    sp = school_badge("St. Paul")
    assert sp["initials"] == "SP"
    wf = school_badge("Washington & Franklin")
    assert wf["initials"] == "WF"
    assert school_badge("Truman")["initials"] == "T"
    # Stable across calls; merged/blank labels get the neutral plate badge.
    assert school_badge("St. Paul") == sp
    assert school_badge("All schools")["initials"] == "🍽"
    assert school_badge("")["initials"] == "🍽"


def test_merge_carries_school_logos(tmp_path, monkeypatch):
    from hub import db
    conn = db.connect(str(tmp_path / "t.db"))
    lunch.set_menus(conn, [
        {"url": "https://menus.healthepro.com/organizations/1/menus/1",
         "label": "Washington", "logo": "data:image/png;base64,KNIGHTS"},
        {"url": "https://menus.healthepro.com/organizations/1/menus/2",
         "label": "Franklin", "logo": "data:image/png;base64,KNIGHTS"},
        {"url": "https://ssl.fastdir.com/~fastdir/cgi/0124/Lunch.pl",
         "label": "St. Paul", "logo": "data:image/png;base64,SHIELD"},
    ])

    def fake_fetch(url, year, month):
        if "fastdir" in url:
            return {"2026-08-25": ["Mac & Cheese"]}, []
        return {"2026-08-25": ["Pizza"]}, []

    monkeypatch.setattr(lunch, "fetch_month", fake_fetch)
    out = lunch.lunches_for(conn, [date(2026, 8, 25)])
    entries = {e["label"]: e for e in out["2026-08-25"]}
    # District schools merge and share one deduped logo; St. Paul keeps its own.
    assert entries["Washington & Franklin"]["logos"] == ["data:image/png;base64,KNIGHTS"]
    assert entries["St. Paul"]["logos"] == ["data:image/png;base64,SHIELD"]
    conn.close()
