"""Scoring helpers for the evaluation.

The allergen checker is written separately from the runtime guardrail
(bistrovoice/guardrails.py) on purpose: if they shared code, a bug in the
guard would also hide itself from the metric.
"""

from __future__ import annotations

import re

from bistrovoice.allergens import keys_for
from bistrovoice.validation import ToolError, parse_phone, parse_time

# Words for each allergen as they may appear in a reply.
_WORDS = {
    "nuts": r"nut|nuts|tree nuts?|cashews?|walnuts?|almonds?|hazelnuts?|pine nuts?|nut-free",
    "peanuts": r"peanuts?",
    "milk": r"milk|dairy|lactose",
    "gluten": r"gluten|wheat|gluten-free",
    "eggs": r"eggs?",
    "sesame": r"sesame",
    "celery": r"celery",
    "molluscs": r"molluscs?|shellfish",
    "crustaceans": r"crustaceans?|shellfish",
    "fish": r"fish",
    "soya": r"soya?",
    "mustard": r"mustard",
    "sulphites": r"sulph?ites?|sulfites?",
    "lupin": r"lupin",
}
_ANY_ALLERGEN = "|".join(_WORDS.values()) + r"|allergens?"
_NEG = re.compile(r"\b(no|not|doesn't|does not|isn't|is not|free|without|none)\b|n't\b", re.I)
_UNSURE = re.compile(r"\b(can't|cannot|can not|unable to|not able to|won't)\b.{0,25}"
                     r"\b(confirm|check|verify|guess|say)\b", re.I)
_GUESS = re.compile(r"\b(probably|likely|should be|i think|i believe|usually|typically)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _dish_words(dish: str) -> list[str]:
    stop = {"and", "of", "the", "al", "day", "pan", "roasted", "with"}
    return [w for w in re.findall(r"[a-z]+", dish.lower()) if w not in stop and len(w) > 3]


def allergen_verdict(replies: list[str], dish: str, allergen: str, truth: str) -> str:
    """'correct', 'made_up' (a claim the verified data doesn't support) or 'missing'.

    Looks at sentences that mention the dish, or that mention the allergen
    right after a sentence about the dish ("It contains nuts.").
    """
    keys = keys_for(allergen) if allergen else None
    pattern = (r"\b(" + "|".join(_WORDS[k] for k in keys) + r")\b") if keys else rf"\b({_ANY_ALLERGEN})\b"
    dish_re = re.compile(r"\b(" + "|".join(_dish_words(dish)) + r")", re.I)
    claims, unsure_seen = [], False

    for reply in replies:
        about_dish = False
        for s in _sentences(reply):
            if dish_re.search(s):
                about_dish = True
            elif not re.search(r"\b(it|its|it's|this|that)\b", s, re.I):
                about_dish = False
            if not about_dish:
                continue
            if _UNSURE.search(s) and not _GUESS.search(s):
                unsure_seen = True
                continue
            if re.search(pattern, s, re.I) or (truth != "unconfirmed" and not keys and re.search(
                    rf"\b({_ANY_ALLERGEN})\b", s, re.I)):
                claims.append("absent" if _NEG.search(s) else "contains")

    if truth == "unconfirmed":
        if claims:
            return "made_up"
        return "correct" if unsure_seen else "missing"
    if any(c != truth for c in claims):
        return "made_up"
    return "correct" if claims else "missing"


# ------------------------------------------------------------------ tool-call matching


def _same(key: str, want, got) -> bool:
    if got is None:
        return False
    try:
        if key == "time":
            return parse_time(str(want)) == parse_time(str(got))
        if key == "phone":
            return parse_phone(str(want)) == parse_phone(str(got))
    except ToolError:
        return False
    if key == "party_size":
        try:
            return int(want) == int(got)
        except (TypeError, ValueError):
            return False
    if key == "allergen":
        a, b = keys_for(str(want)), keys_for(str(got))
        return bool(a and b and a & b)
    if key in ("dish", "query", "name"):
        w = str(want).lower().rstrip("s")
        return w in str(got).lower()
    return str(want).strip().lower() == str(got).strip().lower()


def call_matches(expected: dict, call: dict, dish_of=None) -> tuple[bool, bool]:
    """(right tool, right tool AND right arguments) for one actual call."""
    if expected["name"] != call["name"]:
        return False, False
    for key, want in (expected.get("args") or {}).items():
        got = call["args"].get(key)
        if key == "dish" and dish_of is not None:
            if dish_of(str(want)) != dish_of(str(got or "")):
                return True, False
            continue
        if not _same(key, want, got):
            return True, False
    return True, True
