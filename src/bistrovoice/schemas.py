"""Tool definitions sent to the LLM (OpenAI function-calling format).

Descriptions are short on purpose: they are sent with every request, and on
a voice call every token is latency.
"""

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


DATE = {"type": "string", "description": "YYYY-MM-DD"}
TIME = {"type": "string", "description": "24-hour HH:MM, e.g. 19:30"}
PARTY = {"type": "integer", "description": "Number of people"}

TOOL_SCHEMAS = [
    _fn(
        "check_table_availability",
        "Check if a table is free. Returns nearest free times if not.",
        {"date": DATE, "time": TIME, "party_size": PARTY},
        ["date", "time", "party_size"],
    ),
    _fn(
        "search_menu",
        "Search dishes by name, ingredient or course. Returns price, description, ingredients.",
        {"query": {"type": "string", "description": "e.g. 'pesto', 'desserts', 'mushroom'"}},
        ["query"],
    ),
    _fn(
        "get_allergen_info",
        "Verified allergen information for one dish. Repeat its 'say' text exactly.",
        {
            "dish": {"type": "string", "description": "Dish name as the customer said it"},
            "allergen": {"type": "string", "description": "Allergen the customer asked about, if any"},
        },
        ["dish"],
    ),
    _fn(
        "create_reservation",
        "Book a table. First call returns details to read back; call again with the same "
        "details only after the customer says yes.",
        {
            "name": {"type": "string"},
            "phone": {"type": "string", "description": "Indian mobile number, 10 digits"},
            "party_size": PARTY,
            "date": DATE,
            "time": TIME,
        },
        ["name", "phone", "party_size", "date", "time"],
    ),
    _fn(
        "handoff_to_staff",
        "Pass the call to a member of staff. Only when the customer accepts or asks for a person.",
        {"reason": {"type": "string", "description": "Short reason"}},
        ["reason"],
    ),
]
