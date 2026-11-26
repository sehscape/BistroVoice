"""The allergen vocabulary and the rule for what counts as "verified".

The vocabulary is the 14 major allergens of international menu labelling (the
EU/UK list, which covers the allergens India's FSSAI labelling rules name). A record is verified only when the
spreadsheet says verified = yes AND every listed allergen is one of the 14
(or the single word "none"). Anything else, such as "nuts?" or a blank cell,
is treated as unconfirmed and its contents are never shown to the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALLERGENS: dict[str, str] = {
    "celery": "celery",
    "gluten": "gluten",
    "crustaceans": "crustaceans",
    "eggs": "eggs",
    "fish": "fish",
    "lupin": "lupin",
    "milk": "milk",
    "molluscs": "molluscs",
    "mustard": "mustard",
    "nuts": "tree nuts",
    "peanuts": "peanuts",
    "sesame": "sesame",
    "soya": "soya",
    "sulphites": "sulphites",
}

# Words a customer might use, mapped to the allergens they cover. "Nuts" in
# everyday speech includes peanuts, so it checks both: answering "no nuts" for
# a dish with peanuts would be the worst possible mistake.
_CUSTOMER_TERMS: dict[str, set[str]] = {
    "nut": {"nuts", "peanuts"}, "nuts": {"nuts", "peanuts"},
    "tree nut": {"nuts"}, "tree nuts": {"nuts"},
    "almond": {"nuts"}, "walnut": {"nuts"}, "hazelnut": {"nuts"}, "cashew": {"nuts"},
    "pecan": {"nuts"}, "pistachio": {"nuts"}, "macadamia": {"nuts"}, "brazil nut": {"nuts"},
    "pine nut": {"nuts"},
    "peanut": {"peanuts"}, "groundnut": {"peanuts"},
    "milk": {"milk"}, "dairy": {"milk"}, "lactose": {"milk"},
    "gluten": {"gluten"}, "wheat": {"gluten"}, "coeliac": {"gluten"}, "celiac": {"gluten"},
    "egg": {"eggs"},
    "fish": {"fish"},
    "shellfish": {"crustaceans", "molluscs"},
    "seafood": {"fish", "crustaceans", "molluscs"},
    "crustacean": {"crustaceans"}, "prawn": {"crustaceans"}, "shrimp": {"crustaceans"},
    "crab": {"crustaceans"}, "lobster": {"crustaceans"},
    "mollusc": {"molluscs"}, "mollusk": {"molluscs"}, "squid": {"molluscs"},
    "mussel": {"molluscs"}, "clam": {"molluscs"}, "oyster": {"molluscs"},
    "soy": {"soya"}, "soya": {"soya"}, "soybean": {"soya"},
    "sesame": {"sesame"}, "mustard": {"mustard"}, "celery": {"celery"},
    "lupin": {"lupin"}, "lupine": {"lupin"},
    "sulphite": {"sulphites"}, "sulfite": {"sulphites"}, "sulphur dioxide": {"sulphites"},
}


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def keys_for(text: str) -> set[str] | None:
    """Map a customer's word ("dairy", "nuts", "shellfish") to allergen keys.

    Returns None when the word isn't one of the 14 (e.g. "garlic").
    """
    t = " ".join(_singular(w) for w in re.findall(r"[a-z]+", text.lower()))
    t = t.replace(" free", "").replace(" allergy", "").strip()
    found: set[str] = set()
    for term, keys in _CUSTOMER_TERMS.items():
        if re.search(rf"\b{re.escape(_singular(term))}\b", t):
            found |= keys
    if "tree nut" in t:
        found.discard("peanuts")
    return found or None


@dataclass(frozen=True)
class AllergenRecord:
    status: str  # "verified" or "unconfirmed"
    reason: str = ""  # for unconfirmed: "missing" or "unclear"
    contains: tuple[str, ...] = ()
    detail: str = ""


def classify(row) -> AllergenRecord:
    """Decide whether an allergens row can be used. `row` may be None."""
    if row is None or not (row["contains"] or "").strip():
        return AllergenRecord("unconfirmed", "missing")
    if (row["verified"] or "").strip().lower() != "yes":
        return AllergenRecord("unconfirmed", "unclear")
    items = [i.strip().lower() for i in row["contains"].split(";") if i.strip()]
    if items == ["none"]:
        return AllergenRecord("verified", contains=())
    if not items or any(i not in ALLERGENS for i in items):
        return AllergenRecord("unconfirmed", "unclear")
    return AllergenRecord("verified", contains=tuple(items), detail=(row["detail"] or "").strip())


def _join(labels: list[str], word: str = "and") -> str:
    if len(labels) <= 1:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} {word} {labels[-1]}"


def _labels(keys) -> list[str]:
    return [ALLERGENS[k] for k in ALLERGENS if k in keys]


def describe(dish: str, record: AllergenRecord, kitchen_note: str,
             asked: set[str] | None = None, asked_text: str = "") -> str:
    """The exact sentence the agent must say. Built from the verified field only."""
    if record.status != "verified":
        return (f"I'm sorry, I can't confirm the allergens in the {dish} right now, so I won't "
                "guess. I can connect you with a member of our team who can check.")

    contains = set(record.contains)
    full_list = (f"Its allergens are {_join(_labels(contains))}." if contains
                 else "It contains none of the 14 major allergens.")

    if asked_text and asked is None:
        return (f"I can only confirm the 14 major allergens, and {asked_text} isn't one of them. "
                f"For the {dish}: {full_list[0].lower()}{full_list[1:]} "
                "A member of our team can help with anything else.")

    if not asked:
        if not contains:
            return f"The {dish} contains none of the 14 major allergens. {kitchen_note}"
        return f"The {dish} contains {_join(_labels(contains))}."

    hits = asked & contains
    if hits:
        text = f"Yes, the {dish} contains {_join(_labels(hits))}."
        if record.detail and any(k in record.detail.lower() or ALLERGENS[k] in record.detail.lower()
                                 for k in hits):
            text += f" {record.detail}"
        if contains - hits:
            text += f" {full_list}"
        return text
    return (f"No, the {dish} doesn't contain {_join(_labels(asked), 'or')}. "
            f"{full_list} {kitchen_note}")
