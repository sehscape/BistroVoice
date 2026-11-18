from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bistrovoice.db import build_database, load_rules

LONDON = ZoneInfo("Europe/London")
# Wednesday 18 November 2026, 3pm. The sample reservations are in this week.
NOW = datetime(2026, 11, 18, 15, 0, tzinfo=LONDON)


@pytest.fixture
def conn():
    c = build_database(":memory:")
    yield c
    c.close()


@pytest.fixture
def ctx(conn):
    from bistrovoice.tools import ToolContext

    return ToolContext(conn=conn, rules=load_rules(conn), clock=lambda: NOW)
