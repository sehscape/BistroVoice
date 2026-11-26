"""SQLite schema, CSV loader and the restaurant's booking rules.

The CSV files in data/ are the source of truth. The database is a build
artefact: `bistrovoice init-db` drops it and loads the CSVs again, so
anyone can edit the spreadsheet and rebuild.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE restaurant (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE menu (
    dish_id     TEXT PRIMARY KEY,
    dish        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    price       REAL NOT NULL CHECK (price > 0),
    category    TEXT NOT NULL CHECK (category IN ('starter', 'main', 'side', 'dessert'))
);

CREATE TABLE ingredients (
    dish_id    TEXT NOT NULL REFERENCES menu (dish_id),
    ingredient TEXT NOT NULL,
    PRIMARY KEY (dish_id, ingredient)
);

-- Stored exactly as written in the spreadsheet. Whether a row counts as
-- "verified" is decided in code (allergens.py), not here.
CREATE TABLE allergens (
    dish_id    TEXT PRIMARY KEY REFERENCES menu (dish_id),
    contains   TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    verified   TEXT NOT NULL DEFAULT '',
    checked_by TEXT NOT NULL DEFAULT '',
    checked_on TEXT NOT NULL DEFAULT ''
);

CREATE TABLE opening_hours (
    day          TEXT NOT NULL,
    opens        TEXT,
    closes       TEXT,
    last_seating TEXT
);

CREATE TABLE tables (
    table_id TEXT PRIMARY KEY,
    seats    INTEGER NOT NULL CHECK (seats > 0),
    area     TEXT NOT NULL
);

CREATE TABLE reservations (
    reservation_id INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    phone          TEXT NOT NULL,
    party_size     INTEGER NOT NULL CHECK (party_size > 0),
    date           TEXT NOT NULL,
    time           TEXT NOT NULL,
    table_id       TEXT NOT NULL REFERENCES tables (table_id),
    status         TEXT NOT NULL DEFAULT 'confirmed',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_reservations_date ON reservations (date);

CREATE TABLE handoffs (
    handoff_id INTEGER PRIMARY KEY,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status     TEXT NOT NULL DEFAULT 'open'
);
"""

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def restaurant_today(data_dir: Path = DATA_DIR) -> date:
    """Today's date where the restaurant is (IST), not where the server runs.
    A host on UTC is still on "yesterday" until 5:30 am in Mumbai."""
    from zoneinfo import ZoneInfo

    tz = next(r["value"] for r in _read_csv(data_dir / "restaurant.csv") if r["key"] == "timezone")
    return datetime.now(ZoneInfo(tz)).date()


def build_database(
    path: str | Path,
    data_dir: Path = DATA_DIR,
    *,
    align_to: date | None = None,
) -> sqlite3.Connection:
    """Create a fresh database from the CSV files and return a connection.

    `align_to` moves the sample reservations forward by whole weeks so they
    land in the same week as that date (weekdays stay the same). The live demo
    uses it so "Friday is busy" stays true; tests and the evaluation leave it
    off and use a fixed clock instead.
    """
    if str(path) != ":memory:":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)

    conn = connect(path)
    conn.executescript(SCHEMA)

    rows = {name: _read_csv(data_dir / f"{name}.csv") for name in (
        "restaurant", "menu", "ingredients", "allergens", "opening_hours", "tables", "reservations"
    )}
    _check_csvs(rows)

    shift = timedelta(0)
    if align_to is not None and rows["reservations"]:
        first = min(date.fromisoformat(r["date"]) for r in rows["reservations"])
        anchor_monday = first - timedelta(days=first.weekday())
        target_monday = align_to - timedelta(days=align_to.weekday())
        shift = timedelta(weeks=(target_monday - anchor_monday).days // 7)

    conn.execute("BEGIN")
    conn.executemany("INSERT INTO restaurant VALUES (:key, :value)", rows["restaurant"])
    conn.executemany(
        "INSERT INTO menu VALUES (:dish_id, :dish, :description, :price, :category)", rows["menu"]
    )
    conn.executemany("INSERT INTO ingredients VALUES (:dish_id, :ingredient)", rows["ingredients"])
    conn.executemany(
        "INSERT INTO allergens VALUES (:dish_id, :contains, :detail, :verified, :checked_by, :checked_on)",
        rows["allergens"],
    )
    conn.executemany(
        "INSERT INTO opening_hours VALUES (:day, NULLIF(:opens, ''), NULLIF(:closes, ''), NULLIF(:last_seating, ''))",
        rows["opening_hours"],
    )
    conn.executemany("INSERT INTO tables VALUES (:table_id, :seats, :area)", rows["tables"])
    for r in rows["reservations"]:
        d = (date.fromisoformat(r["date"]) + shift).isoformat()
        conn.execute(
            "INSERT INTO reservations (reservation_id, name, phone, party_size, date, time, table_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(r["reservation_id"]), r["name"], r["phone"], int(r["party_size"]), d, r["time"],
             r["table_id"]),
        )
    conn.execute("COMMIT")
    return conn


def _check_csvs(rows: dict[str, list[dict[str, str]]]) -> None:
    """Fail loudly on spreadsheet mistakes instead of loading bad data."""
    dish_ids = {r["dish_id"] for r in rows["menu"]}
    table_ids = {r["table_id"] for r in rows["tables"]}
    problems = []
    for r in rows["ingredients"] + rows["allergens"]:
        if r["dish_id"] not in dish_ids:
            problems.append(f"unknown dish_id {r['dish_id']!r}")
    for r in rows["reservations"]:
        if r["table_id"] not in table_ids:
            problems.append(f"reservation {r['reservation_id']} uses unknown table {r['table_id']!r}")
    for r in rows["opening_hours"]:
        if r["day"] not in WEEKDAYS:
            problems.append(f"bad weekday {r['day']!r}")
    no_allergen_row = dish_ids - {r["dish_id"] for r in rows["allergens"]}
    if no_allergen_row:
        # Not fatal (the tool treats it as "missing"), but every dish should have a row.
        problems.append(f"dishes without an allergens row: {sorted(no_allergen_row)}")
    if problems:
        raise ValueError("CSV check failed:\n  " + "\n  ".join(problems))


# --------------------------------------------------------------------------- rules


@dataclass(frozen=True)
class Service:
    opens: time
    closes: time
    last_seating: time


@dataclass(frozen=True)
class Rules:
    """Booking rules, read from restaurant.csv and opening_hours.csv."""

    name: str
    phone: str
    timezone: str
    booking_minutes: int
    slot_minutes: int
    min_party: int
    max_party: int
    max_empty_seats: int
    max_days_ahead: int
    min_notice_minutes: int
    kitchen_note: str
    hours: dict[int, list[Service]] = field(default_factory=dict)

    def services_on(self, day: date) -> list[Service]:
        return self.hours.get(day.weekday(), [])

    def hours_text(self, day: date) -> str:
        services = self.services_on(day)
        if not services:
            return f"closed on {WEEKDAYS[day.weekday()]}s"
        parts = [f"{s.opens:%H:%M}-{s.closes:%H:%M} (last seating {s.last_seating:%H:%M})"
                 for s in services]
        return f"open {' and '.join(parts)} on {WEEKDAYS[day.weekday()]}s"


def _t(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def load_rules(conn: sqlite3.Connection) -> Rules:
    kv = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM restaurant")}
    hours: dict[int, list[Service]] = {}
    for r in conn.execute("SELECT * FROM opening_hours WHERE opens IS NOT NULL"):
        hours.setdefault(WEEKDAYS.index(r["day"]), []).append(
            Service(_t(r["opens"]), _t(r["closes"]), _t(r["last_seating"]))
        )
    for services in hours.values():
        services.sort(key=lambda s: s.opens)
    return Rules(
        name=kv["name"],
        phone=kv["phone"],
        timezone=kv["timezone"],
        booking_minutes=int(kv["booking_duration_minutes"]),
        slot_minutes=int(kv["slot_minutes"]),
        min_party=int(kv["min_party"]),
        max_party=int(kv["max_online_party"]),
        max_empty_seats=int(kv["max_empty_seats"]),
        max_days_ahead=int(kv["max_days_ahead"]),
        min_notice_minutes=int(kv["min_notice_minutes"]),
        kitchen_note=kv["kitchen_note"],
        hours=hours,
    )
