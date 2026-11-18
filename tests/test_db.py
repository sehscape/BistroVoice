from datetime import date

import pytest

from bistrovoice.config import DATA_DIR
from bistrovoice.db import build_database, load_rules


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_every_csv_is_loaded(conn):
    assert count(conn, "menu") == 20
    assert count(conn, "allergens") == 20
    assert count(conn, "tables") == 11
    assert count(conn, "reservations") == 27
    assert count(conn, "ingredients") > 60


def test_every_dish_has_ingredients(conn):
    orphans = conn.execute(
        "SELECT dish FROM menu WHERE dish_id NOT IN (SELECT dish_id FROM ingredients)"
    ).fetchall()
    assert orphans == []


def test_three_dishes_have_missing_or_unclear_allergens(conn):
    # Deliberate: the guardrail needs records it must refuse to answer from.
    rows = conn.execute(
        "SELECT m.dish FROM allergens a JOIN menu m USING (dish_id) WHERE a.verified != 'yes'"
    ).fetchall()
    assert sorted(r[0] for r in rows) == ["Lamb Tagine", "Soup of the Day", "Sticky Toffee Pudding"]


def test_rules_come_from_the_spreadsheet(conn):
    rules = load_rules(conn)
    assert rules.max_party == 8
    assert rules.booking_minutes == 120
    assert rules.services_on(date(2026, 11, 16)) == []  # Monday: closed
    friday = rules.services_on(date(2026, 11, 20))
    assert [f"{s.opens:%H:%M}-{s.last_seating:%H:%M}" for s in friday] == ["12:00-14:00", "17:30-21:30"]


def test_align_to_moves_bookings_by_whole_weeks(tmp_path):
    conn = build_database(tmp_path / "x.db", align_to=date(2027, 1, 13))  # a Wednesday
    first = conn.execute("SELECT date FROM reservations WHERE reservation_id = 1001").fetchone()[0]
    assert first == "2027-01-15"  # still a Friday
    assert date.fromisoformat(first).weekday() == 4


def test_bad_csv_is_rejected(tmp_path):
    for f in DATA_DIR.glob("*.csv"):
        (tmp_path / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    bad = tmp_path / "reservations.csv"
    bad.write_text(bad.read_text(encoding="utf-8") + "9999,X,07700900999,2,2026-11-20,19:00,T99\n",
                   encoding="utf-8")
    with pytest.raises(ValueError, match="unknown table"):
        build_database(":memory:", data_dir=tmp_path)
