"""The five tools the agent can call. Plain Python + SQL, no LLM inside.

Each tool takes a ToolContext (database connection, booking rules, clock) and
returns a JSON-serialisable dict. Errors the customer can fix (a past date,
a closed day) come back as {"ok": False, "error": ..., "message": ...};
system failures are caught by run_tool() and never reported as success.
"""

from __future__ import annotations

import difflib
import inspect
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable

from . import allergens
from .db import Rules
from .validation import (
    ToolError,
    check_booking_time,
    parse_date,
    parse_name,
    parse_party_size,
    parse_phone,
    parse_time,
    spoken_date,
    spoken_time,
)

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    rules: Rules
    clock: Callable[[], datetime]
    # Tool names that should fail, used by tests and the evaluation to
    # simulate a database outage.
    faults: set[str] = field(default_factory=set)

    def now(self) -> datetime:
        return self.clock()


# ====================================================================== availability


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _free_table(ctx: ToolContext, day: date, at: time, party: int) -> sqlite3.Row | None:
    """Smallest free table that fits the party, or None.

    A table is busy if any booking on it overlaps [at, at + booking length).
    Tables with more than `max_empty_seats` spare seats are not offered.
    """
    start = _minutes(at)
    end = start + ctx.rules.booking_minutes
    return ctx.conn.execute(
        """
        SELECT table_id, seats, area FROM tables
        WHERE seats >= :party AND seats - :party <= :spare
          AND table_id NOT IN (
              SELECT table_id FROM reservations
              WHERE date = :day AND status = 'confirmed'
                AND CAST(substr(time, 1, 2) AS INTEGER) * 60 + CAST(substr(time, 4, 2) AS INTEGER) < :end
                AND CAST(substr(time, 1, 2) AS INTEGER) * 60 + CAST(substr(time, 4, 2) AS INTEGER)
                    + :length > :start)
        ORDER BY seats, table_id
        LIMIT 1
        """,
        {"party": party, "spare": ctx.rules.max_empty_seats, "day": day.isoformat(),
         "start": start, "end": end, "length": ctx.rules.booking_minutes},
    ).fetchone()


def _bookable_slots(ctx: ToolContext, day: date):
    now = ctx.now()
    earliest = now + timedelta(minutes=ctx.rules.min_notice_minutes)
    for s in ctx.rules.services_on(day):
        slot = datetime.combine(day, s.opens, tzinfo=now.tzinfo)
        last = datetime.combine(day, s.last_seating, tzinfo=now.tzinfo)
        while slot <= last:
            if slot >= earliest:
                yield slot.time()
            slot += timedelta(minutes=ctx.rules.slot_minutes)


def nearest_free_times(ctx: ToolContext, day: date, at: time, party: int, limit: int = 3,
                       skip_requested: bool = True) -> list[time]:
    free = [t for t in _bookable_slots(ctx, day)
            if not (skip_requested and t == at) and _free_table(ctx, day, t, party)]
    free.sort(key=lambda t: (abs(_minutes(t) - _minutes(at)), _minutes(t)))
    return free[:limit]


def _other_days(ctx: ToolContext, day: date, at: time, party: int, days: int = 7) -> list[dict]:
    """Next two open days that have a free table near the requested time."""
    out = []
    for offset in range(1, days + 1):
        d = day + timedelta(days=offset)
        if d > ctx.now().date() + timedelta(days=ctx.rules.max_days_ahead):
            break
        times = nearest_free_times(ctx, d, at, party, limit=2, skip_requested=False)
        if times:
            out.append({"date": d.isoformat(), "day": spoken_date(d),
                        "times": [t.strftime("%H:%M") for t in times]})
        if len(out) == 2:
            break
    return out


def _alternatives(ctx: ToolContext, day: date, at: time, party: int,
                  skip_requested: bool = True) -> dict:
    same_day = nearest_free_times(ctx, day, at, party, skip_requested=skip_requested)
    if same_day:
        spoken = ", ".join(spoken_time(t) for t in same_day)
        return {"alternatives": [t.strftime("%H:%M") for t in same_day],
                "alternatives_text": f"Nearest free times on {spoken_date(day)}: {spoken}."}
    other = _other_days(ctx, day, at, party)
    if other:
        text = "; ".join(f"{o['day']} at " + " or ".join(
            spoken_time(parse_time(t)) for t in o["times"]) for o in other)
        return {"alternatives": [], "other_days": other,
                "alternatives_text": f"Nothing else free that day. Next options: {text}."}
    return {"alternatives": [], "alternatives_text": "No free tables nearby; offer a staff handoff."}


# ====================================================================== tool 1


def check_table_availability(ctx: ToolContext, date: str, time: str, party_size: int) -> dict:
    """Is there a free table for this party at this date and time?"""
    size = parse_party_size(party_size, ctx.rules)
    day = parse_date(date, ctx.now().date())
    at = parse_time(time)
    try:
        check_booking_time(day, at, ctx.rules, ctx.now())
    except ToolError as err:
        # Wrong day/time for the restaurant: still suggest something bookable.
        if err.code in ("closed", "outside_hours", "off_slot", "too_soon", "past_time"):
            target = day
            if err.code == "closed":
                target = next((day + timedelta(days=i) for i in range(1, 8)
                               if ctx.rules.services_on(day + timedelta(days=i))), day)
            alt = _alternatives(ctx, target, at, size, skip_requested=target == day)
            err.message += " " + alt.pop("alternatives_text")
            err.extra.update(alt)
        raise

    when = f"{spoken_date(day)} at {spoken_time(at)}"
    base = {"ok": True, "date": day.isoformat(), "time": at.strftime("%H:%M"), "party_size": size}
    if _free_table(ctx, day, at, size):
        return {**base, "available": True,
                "message": f"A table for {size} is available on {when}.",
                "next": "Get name and phone if missing, then call create_reservation."}
    alt = _alternatives(ctx, day, at, size)
    text = alt.pop("alternatives_text")
    return {**base, "available": False, **alt, "message": f"No table for {size} on {when}. {text}"}


# ====================================================================== tool 2

_STOPWORDS = set("""a an the any do does you have what whats is are on menu your of with dish dishes
something anything options option i me please tell about there which can get got serve
food order like would want for and or some kind kinds""".split())

_CATEGORIES = {
    "starter": "starter", "starters": "starter", "appetiser": "starter", "appetisers": "starter",
    "appetizer": "starter", "appetizers": "starter",
    "main": "main", "mains": "main", "entree": "main", "entrees": "main",
    "side": "side", "sides": "side",
    "dessert": "dessert", "desserts": "dessert", "puddings": "dessert", "sweets": "dessert",
}


def _words(text: str) -> list[str]:
    return [allergens._singular(w) for w in re.findall(r"[a-z]+", text.lower())]


def _menu_rows(ctx: ToolContext) -> list[dict]:
    rows = ctx.conn.execute(
        """SELECT m.*, group_concat(i.ingredient, ', ') AS ingredients
           FROM menu m LEFT JOIN ingredients i USING (dish_id)
           GROUP BY m.dish_id ORDER BY m.rowid"""
    ).fetchall()
    return [dict(r) for r in rows]


def _score(tokens: list[str], row: dict) -> int:
    name = set(_words(row["dish"]))
    ingredients = set(_words(row["ingredients"] or ""))
    description = set(_words(row["description"]))
    score = 0
    for tok in tokens:
        if tok in name:
            score += 3
        elif any(difflib.SequenceMatcher(None, tok, w).ratio() >= 0.8 for w in name):
            score += 2
        elif tok in ingredients:
            score += 2
        elif tok in description:
            score += 1
    return score


def _price(value: float) -> str:
    return f"£{value:.2f}"


def search_menu(ctx: ToolContext, query: str) -> dict:
    """Find dishes by name, ingredient, description or course."""
    raw = re.findall(r"[a-z]+", str(query).lower())
    category = next((_CATEGORIES[w] for w in raw if w in _CATEGORIES), None)
    tokens = [allergens._singular(w) for w in raw
              if w not in _STOPWORDS and w not in _CATEGORIES]
    rows = _menu_rows(ctx)

    if not tokens and not category:
        return {"ok": True, "query": query, "results": [
            {"dish": r["dish"], "category": r["category"], "price": _price(r["price"])} for r in rows]}

    scored = []
    for r in rows:
        if category and r["category"] != category:
            continue
        s = _score(tokens, r) if tokens else 1
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    results = [{"dish": r["dish"], "category": r["category"], "price": _price(r["price"]),
                "description": r["description"], "ingredients": r["ingredients"]}
               for _, r in scored[:6]]
    out = {"ok": True, "query": query, "results": results}
    if results:
        out["note"] = ("Ingredients describe the dish; they are not an allergen check. "
                       "For allergies use get_allergen_info.")
    else:
        out["message"] = f"Nothing on the menu matches {query!r}."
    return out


# ====================================================================== tool 3


def resolve_dish(ctx: ToolContext, text: str) -> tuple[dict | None, list[str]]:
    """Map what the customer said to one menu dish. Returns (dish, options_if_ambiguous)."""
    rows = _menu_rows(ctx)
    wanted = " ".join(_words(text))
    for r in rows:
        if " ".join(_words(r["dish"])) == wanted:
            return r, []
    tokens = [t for t in _words(text) if t not in _STOPWORDS]
    if not tokens:
        return None, []
    scored = sorted(((_score(tokens, r), r) for r in rows), key=lambda x: -x[0])
    best = scored[0][0]
    if best == 0:
        return None, []
    top = [r for s, r in scored if s == best]
    if len(top) == 1:
        return top[0], []
    return None, [r["dish"] for r in top]


def get_allergen_info(ctx: ToolContext, dish: str, allergen: str | None = None) -> dict:
    """Allergens for one dish, from the verified field only.

    Returns a `say` sentence built by code. Ingredients and unverified notes
    are never included, so the model has nothing to guess from.
    """
    row, options = resolve_dish(ctx, dish)
    if row is None:
        if options:
            raise ToolError("ambiguous_dish", f"{dish!r} could be: {', '.join(options)}. "
                            "Ask which one before answering.", options=options)
        raise ToolError("unknown_dish", f"There is no dish called {dish!r} on our menu.")

    record = allergens.classify(ctx.conn.execute(
        "SELECT * FROM allergens WHERE dish_id = ?", (row["dish_id"],)).fetchone())
    asked = allergens.keys_for(allergen) if allergen else None
    say = allergens.describe(row["dish"], record, ctx.rules.kitchen_note, asked, allergen or "")

    out = {"ok": True, "dish": row["dish"], "status": record.status, "say": say}
    if record.status == "verified":
        out["contains"] = [allergens.ALLERGENS[k] for k in record.contains]
    else:
        out["reason"] = record.reason
        out["offer_handoff"] = True
    return out


# ====================================================================== tool 4


def create_reservation(ctx: ToolContext, name: str, phone: str, party_size: int,
                       date: str, time: str) -> dict:
    """Book a table. Validates everything again; never trusts the earlier check."""
    name = parse_name(name)
    phone = parse_phone(phone)
    size = parse_party_size(party_size, ctx.rules)
    day = parse_date(date, ctx.now().date())
    at = parse_time(time)
    check_booking_time(day, at, ctx.rules, ctx.now())
    when = f"{spoken_date(day)} at {spoken_time(at)}"

    conn = ctx.conn
    conn.execute("BEGIN IMMEDIATE")  # one writer at a time: no double booking
    try:
        existing = conn.execute(
            "SELECT * FROM reservations WHERE phone = ? AND date = ? AND time = ? "
            "AND party_size = ? AND status = 'confirmed'",
            (phone, day.isoformat(), at.strftime("%H:%M"), size),
        ).fetchone()
        if existing:  # a retry of the same booking: don't book twice
            conn.execute("COMMIT")
            return {"ok": True, "status": "booked", "already_existed": True,
                    "reference": str(existing["reservation_id"]), "name": existing["name"],
                    "party_size": size, "date": day.isoformat(), "time": at.strftime("%H:%M"),
                    "message": f"This booking already exists: reference {existing['reservation_id']}, {when}."}

        table = _free_table(ctx, day, at, size)
        if table is None:
            conn.execute("ROLLBACK")
            alt = _alternatives(ctx, day, at, size)
            raise ToolError("unavailable",
                            f"No table for {size} on {when}. {alt.pop('alternatives_text')}", **alt)
        cur = conn.execute(
            "INSERT INTO reservations (name, phone, party_size, date, time, table_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, phone, size, day.isoformat(), at.strftime("%H:%M"), table["table_id"]),
        )
        conn.execute("COMMIT")
    except ToolError:
        raise
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    ref = str(cur.lastrowid)
    return {"ok": True, "status": "booked", "reference": ref, "name": name, "party_size": size,
            "date": day.isoformat(), "time": at.strftime("%H:%M"), "table_area": table["area"],
            "message": f"Booked: table for {size} on {when} under {name}. Reference {ref}."}


# ====================================================================== tool 5


def handoff_to_staff(ctx: ToolContext, reason: str) -> dict:
    """Log a request for a member of staff to take over."""
    reason = " ".join(str(reason).split())[:300]
    if not reason:
        raise ToolError("missing_reason", "Give a short reason for the handoff.")
    cur = ctx.conn.execute("INSERT INTO handoffs (reason) VALUES (?)", (reason,))
    return {"ok": True, "status": "handed_off", "ticket": cur.lastrowid,
            "message": "A member of staff has been notified and will take over."}


# ====================================================================== executor

TOOLS: dict[str, Callable[..., dict]] = {
    "check_table_availability": check_table_availability,
    "search_menu": search_menu,
    "get_allergen_info": get_allergen_info,
    "create_reservation": create_reservation,
    "handoff_to_staff": handoff_to_staff,
}

SYSTEM_FAILURE = {
    "ok": False,
    "error": "system_unavailable",
    "message": ("The system did not respond, so nothing was done. Apologise, do not say it worked, "
                "and offer to connect the customer to a member of staff."),
}


def run_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    """Call a tool by name with arguments from the LLM. Never raises."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"ok": False, "error": "unknown_tool", "message": f"There is no tool called {name}."}
    if not isinstance(args, dict):
        return {"ok": False, "error": "bad_arguments", "message": "Arguments must be an object."}

    params = list(inspect.signature(fn).parameters.values())[1:]
    required = [p.name for p in params if p.default is inspect.Parameter.empty]
    missing = [p for p in required if args.get(p) in (None, "")]
    if missing:
        return {"ok": False, "error": "missing_arguments", "missing": missing,
                "message": f"Ask the customer for: {', '.join(missing)}."}
    known = {p.name for p in params}
    kwargs = {k: v for k, v in args.items() if k in known and v is not None}

    try:
        if name in ctx.faults:
            raise sqlite3.OperationalError(f"simulated outage in {name}")
        return fn(ctx, **kwargs)
    except ToolError as err:
        return err.as_result()
    except Exception:
        log.exception("tool %s failed", name)
        return dict(SYSTEM_FAILURE)
