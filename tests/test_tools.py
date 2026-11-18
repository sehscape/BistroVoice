"""Stage 2: the tools as plain functions, no LLM involved.

The clock is fixed at Wednesday 18 Nov 2026 15:00 (see conftest.py).
Friday 20 Nov dinner is deliberately busy in data/reservations.csv.
"""

import json
from datetime import datetime

from bistrovoice.tools import (
    check_table_availability,
    create_reservation,
    get_allergen_info,
    resolve_dish,
    run_tool,
    search_menu,
)
from conftest import LONDON

# ---------------------------------------------------------------- availability


def test_example_scenario_table_is_free_tonight(ctx):
    r = run_tool(ctx, "check_table_availability", {"date": "2026-11-18", "time": "19:30", "party_size": 4})
    assert r["ok"] and r["available"]


def test_busy_friday_offers_nearest_times(ctx):
    r = run_tool(ctx, "check_table_availability", {"date": "2026-11-20", "time": "19:30", "party_size": 4})
    assert r["ok"] and not r["available"]
    assert r["alternatives"] == ["21:00", "21:15", "17:30"]
    # every alternative really is bookable
    for t in r["alternatives"]:
        assert check_table_availability(ctx, "2026-11-20", t, 4)["available"]


def test_past_date_is_rejected(ctx):
    r = run_tool(ctx, "check_table_availability", {"date": "2026-11-17", "time": "19:30", "party_size": 2})
    assert r == {"ok": False, "error": "past_date", "message": "Tuesday 17 November has already passed."}


def test_closed_day_suggests_next_open_day(ctx):
    r = run_tool(ctx, "check_table_availability", {"date": "2026-11-23", "time": "19:00", "party_size": 2})
    assert r["error"] == "closed"
    assert r["alternatives"]  # Tuesday 24th, near 7pm


def test_morning_time_suggests_evening(ctx):
    r = run_tool(ctx, "check_table_availability", {"date": "2026-11-19", "time": "07:30", "party_size": 2})
    assert r["error"] == "outside_hours"
    assert "Did the customer mean 7:30pm?" in r["message"]


def test_time_after_last_seating(ctx):
    r = run_tool(ctx, "check_table_availability", {"date": "2026-11-19", "time": "21:30", "party_size": 2})
    assert r["error"] == "outside_hours"
    assert "21:00" in r["alternatives"]


def test_off_slot_time(ctx):
    r = run_tool(ctx, "check_table_availability", {"date": "2026-11-19", "time": "19:20", "party_size": 2})
    assert r["error"] == "off_slot" and r["nearest"] == ["19:15", "19:30"]


def test_too_little_notice(conn):
    from bistrovoice.db import load_rules
    from bistrovoice.tools import ToolContext

    late = ToolContext(conn, load_rules(conn), lambda: datetime(2026, 11, 18, 17, 20, tzinfo=LONDON))
    r = run_tool(late, "check_table_availability", {"date": "2026-11-18", "time": "17:30", "party_size": 2})
    assert r["error"] == "too_soon"


def test_party_size_limits(ctx):
    big = run_tool(ctx, "check_table_availability", {"date": "2026-11-19", "time": "19:00", "party_size": 12})
    assert big["error"] == "large_party"
    zero = run_tool(ctx, "check_table_availability", {"date": "2026-11-19", "time": "19:00", "party_size": 0})
    assert zero["error"] == "invalid_party_size"
    words = run_tool(ctx, "check_table_availability", {"date": "2026-11-19", "time": "19:00", "party_size": "four"})
    assert words["error"] == "invalid_party_size"


def test_two_people_never_get_the_eight_seat_table(ctx):
    # Friday 20:45 only T11 (8 seats) is free; a couple shouldn't be put there.
    r = check_table_availability(ctx, "2026-11-20", "20:45", 2)
    assert not r["available"]


# ---------------------------------------------------------------- reservations


def test_booking_writes_a_row(ctx):
    r = run_tool(ctx, "create_reservation", {"name": "Sam Lee", "phone": "07700 900 555",
                                             "party_size": 4, "date": "2026-11-18", "time": "19:30"})
    assert r["status"] == "booked" and r["reference"] == "1028"
    row = ctx.conn.execute("SELECT * FROM reservations WHERE reservation_id = 1028").fetchone()
    assert (row["name"], row["phone"], row["party_size"], row["time"]) == ("Sam Lee", "07700900555", 4, "19:30")


def test_retrying_the_same_booking_does_not_double_book(ctx):
    args = {"name": "Sam Lee", "phone": "07700900555", "party_size": 2, "date": "2026-11-19", "time": "19:00"}
    first = run_tool(ctx, "create_reservation", args)
    again = run_tool(ctx, "create_reservation", args)
    assert again["reference"] == first["reference"] and again["already_existed"]
    assert ctx.conn.execute("SELECT COUNT(*) FROM reservations WHERE phone='07700900555'").fetchone()[0] == 1


def test_booking_fills_up_then_refuses(ctx):
    # Wednesday 19:30, party of 4: T5 and T9 are taken, so T6, T7, T8 and T10 are free.
    refs = []
    for i in range(4):
        r = create_reservation(ctx, f"Guest {'ABCD'[i]}", f"0770090060{i}", 4, "2026-11-18", "19:30")
        refs.append(r["reference"])
    assert len(set(refs)) == 4
    r = run_tool(ctx, "create_reservation", {"name": "Guest E", "phone": "07700900609",
                                             "party_size": 4, "date": "2026-11-18", "time": "19:30"})
    assert r["error"] == "unavailable" and r["alternatives"]
    tables = ctx.conn.execute("SELECT table_id FROM reservations WHERE name LIKE 'Guest %'").fetchall()
    assert sorted(t[0] for t in tables) == ["T10", "T6", "T7", "T8"]


def test_booking_validates_name_and_phone(ctx):
    bad_phone = run_tool(ctx, "create_reservation", {"name": "Sam", "phone": "12345", "party_size": 2,
                                                     "date": "2026-11-19", "time": "19:00"})
    assert bad_phone["error"] == "invalid_phone"
    bad_name = run_tool(ctx, "create_reservation", {"name": "4", "phone": "07700900555", "party_size": 2,
                                                    "date": "2026-11-19", "time": "19:00"})
    assert bad_name["error"] == "invalid_name"


def test_booking_in_the_past_is_refused(ctx):
    r = run_tool(ctx, "create_reservation", {"name": "Sam Lee", "phone": "07700900555", "party_size": 2,
                                             "date": "2026-11-18", "time": "12:30"})
    assert r["error"] == "past_time"


# ---------------------------------------------------------------- allergens


def test_pesto_nut_question_answered_from_verified_field(ctx):
    r = get_allergen_info(ctx, "pesto", "nuts")
    assert r["status"] == "verified"
    assert r["say"].startswith("Yes, the Trofie al Pesto contains tree nuts.")
    assert "cashews" in r["say"]


def test_absent_allergen_mentions_kitchen_traces(ctx):
    r = get_allergen_info(ctx, "burrata", "nuts")
    assert r["say"].startswith("No, the Burrata and Heirloom Tomatoes doesn't contain tree nuts or peanuts.")
    assert "traces" in r["say"]


def test_shellfish_covers_molluscs(ctx):
    assert get_allergen_info(ctx, "calamari", "shellfish")["say"].startswith(
        "Yes, the Calamari Fritti contains molluscs.")


def test_allergen_free_dish(ctx):
    assert "none of the 14 major allergens" in get_allergen_info(ctx, "lemon sorbet")["say"]


def test_unverified_dishes_are_never_answered(ctx):
    for dish, reason in [("sticky toffee pudding", "unclear"), ("lamb tagine", "unclear"),
                         ("soup of the day", "missing")]:
        r = get_allergen_info(ctx, dish, "nuts")
        assert r["status"] == "unconfirmed" and r["reason"] == reason and r["offer_handoff"]
        assert "can't confirm" in r["say"]
        # nothing from the unverified row reaches the model
        dumped = json.dumps(r).lower()
        for leak in ("sesame", "peanut", "supplier", "gluten", "contains"):
            assert leak not in dumped, (dish, leak)


def test_not_one_of_the_14(ctx):
    r = get_allergen_info(ctx, "pesto", "garlic")
    assert r["say"].startswith("I can only confirm the 14 major allergens")


def test_ambiguous_and_unknown_dishes(ctx):
    amb = run_tool(ctx, "get_allergen_info", {"dish": "salad"})
    assert amb["error"] == "ambiguous_dish" and len(amb["options"]) == 2
    assert run_tool(ctx, "get_allergen_info", {"dish": "pizza"})["error"] == "unknown_dish"


def test_dish_names_tolerate_transcription_slips(ctx):
    assert resolve_dish(ctx, "the tiramisoo")[0]["dish"] == "Tiramisu"
    assert resolve_dish(ctx, "truffle fries")[0]["dish"] == "Truffle Fries"
    assert resolve_dish(ctx, "squid")[0]["dish"] == "Calamari Fritti"


# ---------------------------------------------------------------- menu search


def test_search_menu(ctx):
    assert search_menu(ctx, "pesto")["results"][0]["dish"] == "Trofie al Pesto"
    desserts = search_menu(ctx, "what desserts do you have")["results"]
    assert {d["category"] for d in desserts} == {"dessert"} and len(desserts) == 4
    assert search_menu(ctx, "squid")["results"][0]["dish"] == "Calamari Fritti"
    assert len(search_menu(ctx, "menu")["results"]) == 20
    assert search_menu(ctx, "pizza")["results"] == []


def test_prices_come_from_the_database(ctx):
    r = search_menu(ctx, "steak frites")["results"][0]
    assert (r["dish"], r["price"]) == ("Ember Steak Frites", "£26.00")


# ---------------------------------------------------------------- failures


def test_simulated_outage_is_reported_not_hidden(ctx):
    ctx.faults.add("create_reservation")
    r = run_tool(ctx, "create_reservation", {"name": "Sam Lee", "phone": "07700900555", "party_size": 2,
                                             "date": "2026-11-19", "time": "19:00"})
    assert r["ok"] is False and r["error"] == "system_unavailable"
    assert ctx.conn.execute("SELECT COUNT(*) FROM reservations WHERE phone='07700900555'").fetchone()[0] == 0


def test_real_database_error_is_caught(ctx):
    ctx.conn.execute("DROP TABLE reservations")
    r = run_tool(ctx, "check_table_availability", {"date": "2026-11-19", "time": "19:00", "party_size": 2})
    assert r["error"] == "system_unavailable"


def test_missing_and_unknown_arguments(ctx):
    r = run_tool(ctx, "create_reservation", {"party_size": 2, "date": "2026-11-19", "time": "19:00"})
    assert r["error"] == "missing_arguments" and r["missing"] == ["name", "phone"]
    assert run_tool(ctx, "delete_everything", {})["error"] == "unknown_tool"


def test_handoff_is_logged(ctx):
    r = run_tool(ctx, "handoff_to_staff", {"reason": "allergen info unconfirmed for Lamb Tagine"})
    assert r["status"] == "handed_off"
    assert ctx.conn.execute("SELECT reason FROM handoffs").fetchone()[0].startswith("allergen")
