"""Checks that run on every reply before it is spoken. Plain code, no LLM.

1. Allergen rule: an allergen statement can only reach the customer as the
   exact sentence get_allergen_info built from the verified field. Any other
   sentence that talks about allergens is removed and replaced by that
   verified sentence (looked up by code if the model didn't call the tool).
2. Action claims: the reply may not say a table is booked, or that staff
   have been called, unless the matching tool actually succeeded.
3. Failure honesty: if a tool failed this turn, the reply must apologise.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Words that only make sense when talking about allergies.
_STRONG = re.compile(
    r"\b(allerg\w*|intoleran\w*|nuts?|nut-free|tree nuts?|peanuts?|gluten\w*|dairy\w*|lactose|"
    r"shellfish|crustaceans?|molluscs?|sesame|soya?|sulph?ites?|sulfites?|lupin|coeliac|celiac)\b",
    re.I,
)
# Words that are also ordinary ingredients; they count only next to a claim word.
_WEAK = re.compile(
    r"\b(milk|eggs?|fish|mustard|celery|wheat|almonds?|walnuts?|hazelnuts?|cashews?|pecans?|"
    r"pistachios?|pine nuts?|prawns?|squid)\b",
    re.I,
)
_CLAIM = re.compile(
    r"\b(contains?|containing|free|safe|without|traces?|suitable|includes?|"
    r"no|not|n't|doesn't|does not|isn't|aren't)\b", re.I,
)
_CANNOT_CONFIRM = re.compile(r"\b(can't|cannot|can not|unable to|not able to) (confirm|check|verify)", re.I)
_HEDGE = re.compile(r"\b(probably|likely|should be|might|may|i think|i believe|usually|typically|"
                    r"normally|generally|most likely)\b", re.I)

_BOOKED_CLAIM = re.compile(
    r"(you'?re (all )?(booked|set)|(is|are|has been|have been) (now )?(booked|confirmed|reserved)|"
    r"i'?ve (now )?(booked|reserved|confirmed|made (the|your|a) (booking|reservation))|"
    r"(booking|reservation) (is )?(confirmed|complete)|confirmed your|"
    r"see you (on|at|then|tonight|tomorrow))",
    re.I,
)
_HANDOFF_CLAIM = re.compile(
    r"(i'?ve (passed|transferred|connected|put you through|notified|let)|"
    r"(someone|a member of (our )?(staff|team)) (will|is going to) (call|be with|contact|ring|get back))",
    re.I,
)

FAILURE_LINE = "I'm sorry, our system isn't responding, so that didn't go through."
NOT_BOOKED_LINE = "Your table isn't booked yet."
ASK_DISH_LINE = "I want to get allergens exactly right. Which dish would you like me to check?"


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def is_allergen_claim(sentence: str) -> bool:
    """Does this sentence state or imply anything about allergens?"""
    if sentence.rstrip().endswith("?") and not _CLAIM.search(sentence.split(",")[0]):
        return False  # a question like "Do you have any allergies?"
    if _CANNOT_CONFIRM.search(sentence) and not _HEDGE.search(sentence):
        return False  # "I can't confirm ..." makes no claim
    if _STRONG.search(sentence):
        return True
    return bool(_WEAK.search(sentence) and _CLAIM.search(sentence))


def enforce_allergen_rule(reply: str, verified: list[str], lookup) -> tuple[str, list[str]]:
    """Replace model-written allergen sentences with the verified ones.

    `verified` are the `say` sentences returned by get_allergen_info this turn.
    `lookup()` is called when the model talked about allergens without calling
    the tool; it returns verified sentences for the dishes being discussed
    (or [] when it can't tell which dish).
    """
    events: list[str] = []
    verified_norm = [_norm(v) for v in verified]
    kept: list[str] = []
    replaced_at: int | None = None
    for sentence in split_sentences(reply):
        n = _norm(sentence)
        if any(n and n in v for v in verified_norm):
            kept.append(sentence)  # the model quoted (part of) a verified sentence
            continue
        if is_allergen_claim(sentence):
            events.append(f"allergen_sentence_removed: {sentence}")
            if replaced_at is None:
                replaced_at = len(kept)
            continue
        kept.append(sentence)

    if events and not verified:
        verified = lookup()
        events.append("allergen_lookup_forced" if verified else "allergen_dish_unclear")
        if not verified:
            verified = [ASK_DISH_LINE]

    joined = _norm(" ".join(kept))
    missing = [v for v in verified if _norm(v) not in joined]
    if missing:
        # Drop the model's own "can't confirm" / "I can connect you" sentences
        # that say the same thing as the verified text, so nothing is said twice.
        verified_parts = [_norm(p) for v in missing for p in split_sentences(v)]
        deduped = []
        for sentence in kept:
            n = _norm(sentence)
            if any(SequenceMatcher(None, n, v).ratio() > 0.6 for v in verified_parts) or (
                    _CANNOT_CONFIRM.search(sentence) and _STRONG.search(sentence)):
                if replaced_at is None:
                    replaced_at = len(deduped)
                events.append(f"duplicate_removed: {sentence}")
                continue
            deduped.append(sentence)
        kept = deduped
        at = replaced_at if replaced_at is not None else 0
        kept[at:at] = missing
        if not any(not e.startswith("duplicate_removed") for e in events):
            events.append("verified_allergen_sentence_added")
    return " ".join(kept), events


def enforce_action_claims(reply: str, *, booked: bool, handed_off: bool,
                          tool_failed: bool) -> tuple[str, list[str]]:
    events: list[str] = []
    kept = []
    for sentence in split_sentences(reply):
        if not booked and _BOOKED_CLAIM.search(sentence):
            events.append(f"false_booking_claim_removed: {sentence}")
            continue
        if not handed_off and _HANDOFF_CLAIM.search(sentence):
            events.append(f"false_handoff_claim_removed: {sentence}")
            continue
        kept.append(sentence)
    if any(e.startswith("false_booking") for e in events) and not tool_failed:
        kept.insert(0, NOT_BOOKED_LINE)
    if tool_failed and not re.search(r"\b(sorry|apologi[sz]e)\b", " ".join(kept), re.I):
        kept.insert(0, FAILURE_LINE)
        events.append("failure_apology_added")
    return " ".join(kept), events


def asks_about_allergens(text: str) -> bool:
    """Is the caller asking about allergens (as opposed to just naming food)?"""
    if _STRONG.search(text):
        return True
    return bool(_WEAK.search(text) and re.search(
        r"\b(contains?|have|has|in it|free|safe|any|without|allergic|intoleran\w*)\b", text, re.I))


def is_affirmative(text: str) -> bool:
    """A clear yes, with no "but", "no", "change" or "wait" in it."""
    t = text.lower()
    yes = re.search(r"\b(yes|yeah|yep|yup|sure|correct|right|confirm\w*|go ahead|book it|please do|"
                    r"perfect|sounds good|absolutely|definitely|ok|okay|great|lovely|exactly)\b", t)
    no = re.search(r"\b(no|nope|not|don'?t|wait|actually|change|instead|hold on|cancel|wrong|but)\b", t)
    return bool(yes) and not no
