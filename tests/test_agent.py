"""The agent's safety logic, tested with a scripted fake LLM (no network).

These pin down behaviour the real model must not be able to override:
the confirmation step, the allergen rule, honest failure reporting and
the low-confidence transcript gate.
"""

import json

from bistrovoice.agent import CLARIFY_LINE, Agent
from bistrovoice.llm import LLMReply, ToolCall


class ScriptedLLM:
    """Returns the queued replies in order and records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def chat(self, messages, tools):
        self.sent.append(messages)
        return self.replies.pop(0)


def call(tool, **args):
    return LLMReply(content="", tool_calls=[ToolCall(f"id_{tool}_{len(args)}", tool, json.dumps(args))])


def say(text):
    return LLMReply(content=text)


BOOKING = dict(name="Sam Lee", phone="07700900555", party_size=4, date="2026-11-18", time="19:30")


def rows(ctx):
    return ctx.conn.execute("SELECT COUNT(*) FROM reservations WHERE phone = '07700900555'").fetchone()[0]


def test_booking_needs_a_yes_in_a_later_turn(ctx):
    llm = ScriptedLLM(call("create_reservation", **BOOKING), call("create_reservation", **BOOKING))
    agent = Agent(ctx, llm)
    t1 = agent.respond("Book it for Sam Lee, 07700 900555")
    assert t1.tool_calls[0].result["status"] == "needs_confirmation"
    # the read-back is worded by code, not the model
    assert t1.reply == ("Just to confirm: a table for 4 on Wednesday 18 November at 7:30pm, under the "
                        "name Sam Lee, phone 07700 900555. Shall I book that?")
    assert rows(ctx) == 0
    t2 = agent.respond("Yes please")
    assert t2.tool_calls[0].result["status"] == "booked"
    assert t2.reply.endswith("Your reference number is 1028.")
    assert rows(ctx) == 1
    assert len(llm.sent) == 2  # no extra LLM call to phrase either reply


def test_model_cannot_confirm_on_the_customers_behalf(ctx):
    # Two create calls in the same turn: the second must not book.
    both = LLMReply(content="", tool_calls=[ToolCall("a", "create_reservation", json.dumps(BOOKING)),
                                            ToolCall("b", "create_reservation", json.dumps(BOOKING))])
    llm = ScriptedLLM(both, say("Shall I book it?"))
    Agent(ctx, llm).respond("Table for 4 tonight 7:30, Sam Lee, 07700900555")
    assert rows(ctx) == 0


def test_no_or_a_change_is_not_a_yes(ctx):
    llm = ScriptedLLM(call("create_reservation", **BOOKING), call("create_reservation", **BOOKING))
    agent = Agent(ctx, llm)
    agent.respond("Sam Lee, 07700900555")
    agent.respond("Yes but actually make it 8pm")
    assert rows(ctx) == 0


def test_changed_details_need_a_new_confirmation(ctx):
    later = {**BOOKING, "time": "20:00"}
    llm = ScriptedLLM(call("create_reservation", **BOOKING), call("create_reservation", **later))
    agent = Agent(ctx, llm)
    agent.respond("Sam Lee, 07700900555")
    t2 = agent.respond("yes")
    assert t2.tool_calls[0].result["status"] == "needs_confirmation"
    assert rows(ctx) == 0


def test_made_up_allergen_answer_is_replaced(ctx):
    # The model "answers" from the ingredient list without calling the tool.
    llm = ScriptedLLM(say("Good news, the sticky toffee pudding has no nuts, just dates and toffee."))
    t = Agent(ctx, llm).respond("Does the sticky toffee pudding have nuts? I'm allergic.")
    assert "no nuts" not in t.reply
    assert "can't confirm the allergens in the Sticky Toffee Pudding" in t.reply
    assert any(c.forced for c in t.tool_calls)
    assert any(e.startswith("allergen_sentence_removed") for e in t.guard_events)


def test_paraphrased_allergen_answer_becomes_the_verified_sentence(ctx):
    llm = ScriptedLLM(call("get_allergen_info", dish="pesto", allergen="nuts"),
                      say("Yep, the pesto is made with cashews so it's not nut-free. Anything else?"))
    t = Agent(ctx, llm).respond("Does the pesto contain nuts?")
    assert t.reply.startswith("Yes, the Trofie al Pesto contains tree nuts.")
    assert "not nut-free" not in t.reply and t.reply.endswith("Anything else?")


def test_verified_sentence_is_kept_when_quoted(ctx):
    verified = ("No, the Lemon Sorbet doesn't contain tree nuts or peanuts. It contains none of the 14 "
                "major allergens. Our kitchen handles all 14 major allergens, so we can't rule out traces.")
    llm = ScriptedLLM(call("get_allergen_info", dish="lemon sorbet", allergen="nuts"), say(verified))
    t = Agent(ctx, llm).respond("Is the lemon sorbet ok for a nut allergy?")
    assert t.reply == verified and t.guard_events == []


def test_false_booking_claim_after_a_failure_is_removed(ctx):
    ctx.faults.add("check_table_availability")
    llm = ScriptedLLM(call("create_reservation", **BOOKING), say("You're all booked for 7:30pm tonight!"))
    t = Agent(ctx, llm).respond("Sam Lee, 07700900555")
    assert "booked" not in t.reply.lower()
    assert t.reply.startswith("I'm sorry")
    assert rows(ctx) == 0


def test_invented_name_or_phone_is_refused(ctx):
    llm = ScriptedLLM(call("create_reservation", **{**BOOKING, "name": "John Smith", "phone": "07123456789"}),
                      say("Could I take your name and number?"))
    t = Agent(ctx, llm).respond("A table for 4 tonight at 7:30 please")
    assert t.tool_calls[0].result["error"] == "details_not_given"
    assert t.tool_calls[0].result["missing"] == ["name", "phone"]


def test_false_handoff_claim_is_removed(ctx):
    llm = ScriptedLLM(say("I've passed you to our manager, they'll be right with you."))
    t = Agent(ctx, llm).respond("I want to complain")
    assert "passed you" not in t.reply


def test_low_confidence_transcript_never_reaches_the_llm(ctx):
    llm = ScriptedLLM()
    t = Agent(ctx, llm).respond("table for for at heaven thirty", confidence=0.31)
    assert t.reply == CLARIFY_LINE and t.clarified and llm.sent == []


def test_llm_outage_offers_staff(ctx):
    class Down:
        def chat(self, *_):
            from bistrovoice.llm import LLMError
            raise LLMError("503")

    t = Agent(ctx, Down()).respond("Hi")
    assert "member of our team" in t.reply


def test_system_prompt_carries_dates_and_pending_booking(ctx):
    llm = ScriptedLLM(call("create_reservation", **BOOKING), say("Sure."))
    agent = Agent(ctx, llm)
    agent.respond("Sam Lee 07700900555")
    agent.respond("hmm")
    messages = llm.sent[-1]
    system, note = messages[0]["content"], messages[-2]["content"]
    assert "Today is Wednesday 18 November 2026" in system
    assert "Fri 20 Nov = 2026-11-20" in system
    assert note.startswith("Call notes: time now 15:00.")
    assert "Waiting for the customer to confirm: table for 4" in note


def test_history_is_append_only_so_it_can_be_cached(ctx):
    llm = ScriptedLLM(say("Hello!"), say("Sure."))
    agent = Agent(ctx, llm)
    agent.respond("Hi")
    agent.respond("Do you take bookings?")
    first, second = llm.sent
    assert second[:len(first)] == first
