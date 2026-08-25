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
    assert lunches == {"2026-08-25": "Cheese Pizza, Garden Salad, Milk"}


def test_parse_month_plain_lists_and_alt_keys():
    payload = {
        "menu_month_calendar": [
            {"date": "2026-09-01", "items": [
                {"type": "recipe", "recipe_name": "Tacos"},
                {"type": "recipe", "name": "Refried Beans"},
            ]},
        ]
    }
    assert lunch.parse_month(payload) == {"2026-09-01": "Tacos, Refried Beans"}


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
        return {"2026-08-25": "Cheese Pizza"}, []

    monkeypatch.setattr(lunch, "fetch_month", fake_fetch)
    days = [date(2026, 8, 24), date(2026, 8, 25)]
    out = lunch.lunches_for(conn, days)
    assert out == {"2026-08-25": [{"label": "Elementary", "text": "Cheese Pizza"}]}
    # Second call within the TTL hits the cache, not the network.
    out2 = lunch.lunches_for(conn, days)
    assert out2 == out
    assert len(calls) == 1
    conn.close()
