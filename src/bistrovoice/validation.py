"""Input validation for tool arguments. Plain code, no LLM.

Every check raises ToolError with a short machine-readable code and a
sentence the agent can say to the customer. The LLM never decides whether a
date is in the past or a time is inside opening hours: this module does.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .db import WEEKDAYS, Rules


class ToolError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_result(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message, **self.extra}


# ------------------------------------------------------------------ formatting


def spoken_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} {d:%B}"


def spoken_time(t: time) -> str:
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{hour}{suffix}" if t.minute == 0 else f"{hour}:{t.minute:02d}{suffix}"


# ------------------------------------------------------------------ parsing

_TIME_24 = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
_TIME_12 = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s*m\.?\s*$", re.I)


def parse_date(value: str, today: date) -> date:
    text = str(value).strip().lower()
    if text in ("today", "tonight", "this evening"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ToolError("invalid_date", f"I couldn't read the date {value!r}. Use YYYY-MM-DD.")


def parse_time(value: str) -> time:
    text = str(value).strip()
    m = _TIME_24.match(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = _TIME_12.match(text)
        if not m:
            raise ToolError("invalid_time", f"I couldn't read the time {value!r}. Use HH:MM.")
        hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
        if m.group(3).lower() == "p":
            hour += 12
    if hour > 23 or minute > 59:
        raise ToolError("invalid_time", f"{value!r} is not a valid time.")
    return time(hour, minute)


def parse_party_size(value, rules: Rules) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value).is_integer():
        size = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        size = int(value.strip())
    else:
        raise ToolError("invalid_party_size", f"I couldn't read the party size {value!r}.")
    if size < rules.min_party:
        raise ToolError("invalid_party_size", "The party needs at least one person.")
    if size > rules.max_party:
        raise ToolError(
            "large_party",
            f"Parties of more than {rules.max_party} are booked by our staff, not online. "
            "Offer to connect the customer to a member of staff.",
            max_party=rules.max_party,
        )
    return size


_NAME = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ' .-]{0,58}[A-Za-zÀ-ÖØ-öø-ÿ.]$")


def parse_name(value: str) -> str:
    name = " ".join(str(value).split())
    if not _NAME.match(name):
        raise ToolError("invalid_name", "I need a name for the booking (letters only).")
    return name


def parse_phone(value: str) -> str:
    """UK numbers: 0 + 10 digits, or +44 + 10 digits. Stored as 11 digits."""
    digits = re.sub(r"[\s().-]", "", str(value))
    if digits.startswith("+44"):
        digits = "0" + digits[3:]
    elif digits.startswith("0044"):
        digits = "0" + digits[4:]
    if not re.fullmatch(r"0\d{10}", digits):
        raise ToolError(
            "invalid_phone",
            f"{value!r} doesn't look like a UK phone number (11 digits starting with 0).",
        )
    return digits


# ------------------------------------------------------------------ booking rules


def check_booking_time(day: date, at: time, rules: Rules, now: datetime) -> None:
    """Raise ToolError if a table can't be booked for this date and time."""
    today = now.date()
    if day < today:
        raise ToolError("past_date", f"{spoken_date(day)} has already passed.")
    if day > today + timedelta(days=rules.max_days_ahead):
        raise ToolError(
            "too_far_ahead", f"We only take bookings up to {rules.max_days_ahead} days ahead."
        )

    services = rules.services_on(day)
    if not services:
        raise ToolError("closed", f"We're closed on {WEEKDAYS[day.weekday()]}s.",
                        hours=rules.hours_text(day))
    if not any(s.opens <= at <= s.last_seating for s in services):
        message = f"{spoken_time(at)} is outside our booking times; we're {rules.hours_text(day)}."
        if at.hour < 12:
            pm = time(at.hour + 12, at.minute)
            if any(s.opens <= pm <= s.last_seating for s in services):
                message += f" Did the customer mean {spoken_time(pm)}?"
        raise ToolError("outside_hours", message, hours=rules.hours_text(day))

    if at.minute % rules.slot_minutes:
        down = at.minute - at.minute % rules.slot_minutes
        options = [time(at.hour, down)]
        up = datetime.combine(day, options[0]) + timedelta(minutes=rules.slot_minutes)
        options.append(up.time())
        raise ToolError(
            "off_slot",
            f"Bookings are every {rules.slot_minutes} minutes. "
            f"Nearest slots: {', '.join(t.strftime('%H:%M') for t in options)}.",
            nearest=[t.strftime("%H:%M") for t in options],
        )

    start = datetime.combine(day, at, tzinfo=now.tzinfo)
    if start < now + timedelta(minutes=rules.min_notice_minutes):
        if start < now:
            raise ToolError("past_time", f"{spoken_time(at)} today has already passed.")
        raise ToolError(
            "too_soon",
            f"We need at least {rules.min_notice_minutes} minutes' notice for a booking.",
        )
