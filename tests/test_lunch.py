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
        {"label": "Elementary", "text": "Mini Corn Dogs, Tossed Side Salad"}
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
        {"label": "All schools", "text": "Mini Corn Dogs"}
    ]
    # On the 26th only Washington & Franklin match; Truman stays separate.
    assert out["2026-08-26"] == [
        {"label": "Washington & Franklin", "text": "Cheese Pizza, Carrots"},
        {"label": "Truman", "text": "Walking Taco"},
    ]
    conn.close()


def test_join_labels():
    assert lunch._join_labels([]) == ""
    assert lunch._join_labels(["", ""]) == ""
    assert lunch._join_labels(["Truman"]) == "Truman"
    assert lunch._join_labels(["A", "B"]) == "A & B"
    assert lunch._join_labels(["A", "", "B", "C"]) == "A, B & C"
