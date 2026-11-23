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
    llm = ScriptedLLM(call("create_reservation", **BOOKING))
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
    assert len(llm.sent) == 1  # read-back and receipt are code; the "yes" never reaches the LLM


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
    t2 = agent.respond("Could we make it 8pm instead?")
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
    assert "Thu 19 Nov (tomorrow) = 2026-11-19" in system
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


def test_plain_yes_is_booked_by_code_without_the_llm(ctx):
    llm = ScriptedLLM(call("create_reservation", **BOOKING))
    agent = Agent(ctx, llm)
    agent.respond("Sam Lee, 07700 900555")
    t = agent.respond("Yep, perfect.")
    assert len(llm.sent) == 1  # the yes never reached the model
    assert t.tool_calls[0].forced and t.tool_calls[0].result["status"] == "booked"
    assert rows(ctx) == 1


def test_a_yes_with_more_in_it_goes_to_the_llm(ctx):
    llm = ScriptedLLM(call("create_reservation", **BOOKING), say("Of course. Shall I book it first?"))
    agent = Agent(ctx, llm)
    agent.respond("Sam Lee, 07700 900555")
    agent.respond("Yes, and is the tiramisu nut free?")
    assert len(llm.sent) == 2 and rows(ctx) == 0


def test_failed_booking_is_not_retried_by_the_next_yes(ctx):
    llm = ScriptedLLM(call("create_reservation", **BOOKING), say("Connecting you now."))
    agent = Agent(ctx, llm)
    agent.respond("Sam Lee, 07700 900555")
    ctx.faults.add("create_reservation")
    t = agent.respond("Yes please.")
    assert "not booked" in t.reply and agent.pending is None
    ctx.faults.clear()
    agent.respond("Yes, put me through.")
    assert rows(ctx) == 0


def test_skipped_allergy_question_is_answered_by_code(ctx):
    # The model handles the booking half and forgets the allergy (seen in testing).
    llm = ScriptedLLM(call("check_table_availability", date="2026-11-18", time="19:30", party_size=4),
                      say("Great, a table for four is free at 7:30pm. Can I take a name?"))
    t = Agent(ctx, llm).respond("Can I book four at 7:30 tonight? Also, does the pesto contain nuts? I'm allergic.")
    assert t.reply.startswith("Yes, the Trofie al Pesto contains tree nuts.")
    assert t.reply.endswith("Can I take a name?")
    assert "allergen_question_answered_by_code" in t.guard_events


def test_weekday_in_the_callers_words_is_checked_against_the_date(ctx):
    # Seen in the evaluation: "Tuesday at 7pm then" became 2026-11-23, a Monday.
    llm = ScriptedLLM(call("check_table_availability", date="2026-11-23", time="19:00", party_size=4),
                      call("check_table_availability", date="2026-11-24", time="19:00", party_size=4),
                      say("Tuesday at 7pm is free. Can I take a name?"))
    t = Agent(ctx, llm).respond("Tuesday at 7pm then.")
    first, second = t.tool_calls
    assert first.result["error"] == "date_mismatch" and "2026-11-24" in first.result["message"]
    assert second.result["available"]


def test_allergen_named_by_the_caller_is_passed_on(ctx):
    # Seen in the evaluation: the model sent allergen "" for "any dairy in the sorbet?".
    llm = ScriptedLLM(call("get_allergen_info", dish="lemon sorbet", allergen=""), say("Here you go."))
    t = Agent(ctx, llm).respond("Is there any dairy in the lemon sorbet?")
    assert t.tool_calls[0].args["allergen"] == "dairy"
    assert t.reply.startswith("No, the Lemon Sorbet doesn't contain milk.")


def test_model_narration_is_not_spoken():
    from bistrovoice.agent import tidy

    assert tidy("Could you give me the number? User hasn't responded yet.") == "Could you give me the number?"


def test_malformed_tool_call_asks_again_instead_of_offering_staff(ctx):
    from bistrovoice.llm import MalformedToolCall

    class Fumbles:
        def chat(self, *_):
            raise MalformedToolCall('{"party_size":1","phone":"0770092"}')

    t = Agent(ctx, Fumbles()).respond("It's Alex Morgan, 07700 900201.")
    assert t.reply == CLARIFY_LINE and t.clarified
    assert "team" not in t.reply


def test_provider_errors_are_logged_as_a_category_only(ctx):
    class Limited:
        def chat(self, *_):
            from bistrovoice.llm import LLMError
            raise LLMError("Error code: 429 - Rate limit reached for model in organization org_abc123")

    t = Agent(ctx, Limited()).respond("Hi")
    assert t.guard_events == ["llm_error: rate limited"]


def test_a_readback_the_caller_moved_past_cannot_be_confirmed(ctx):
    # Evaluation M05: read-back Friday, "No, I said Thursday", the model asks its
    # own question without a new read-back, then "Yes" must NOT book Friday.
    friday = {**BOOKING, "date": "2026-11-20", "time": "13:00"}
    llm = ScriptedLLM(call("create_reservation", **friday),
                      call("check_table_availability", date="2026-11-19", time="13:00", party_size=4),
                      say("Thursday 1pm is free. Shall I book it for Sam Lee?"),
                      call("create_reservation", **{**friday, "date": "2026-11-19"}))
    agent = Agent(ctx, llm)
    agent.respond("Sam Lee, 07700 900555, Friday at 1pm for four")
    agent.respond("No, I said Thursday, not Friday.")
    t = agent.respond("Yes, that's right.")
    assert rows(ctx) == 0  # nothing booked: Thursday hasn't been read back yet
    assert t.tool_calls[-1].result["status"] == "needs_confirmation"
    assert "Thursday 19 November" in t.reply


def test_failed_handoff_gives_the_restaurant_number(ctx):
    ctx.faults.add("handoff_to_staff")
    llm = ScriptedLLM(call("handoff_to_staff", reason="manager"))
    t = Agent(ctx, llm).respond("Can I speak to a manager please?")
    assert "020 7946 0321" in t.reply and "member of" not in t.reply


def test_allergen_outage_is_said_plainly(ctx):
    ctx.faults.add("get_allergen_info")
    llm = ScriptedLLM(call("get_allergen_info", dish="pesto", allergen="nuts"),
                      say("I'm sorry, I couldn't get that information."))
    t = Agent(ctx, llm).respond("Does the pesto contain nuts? I'm allergic.")
    assert "can't check allergen information right now, so I won't guess" in t.reply


def test_explicit_request_for_a_person_is_honoured(ctx):
    llm = ScriptedLLM(say("Could I take your phone number first?"))
    t = Agent(ctx, llm).respond("Yes, please put me through.")
    assert t.tool_calls[-1].name == "handoff_to_staff" and t.tool_calls[-1].forced
    assert t.reply.startswith("I've passed you to a member of our team")


def test_declining_a_person_is_not_a_request():
    from bistrovoice.guardrails import asks_for_a_person

    assert asks_for_a_person("Could I speak to a real person please?")
    assert asks_for_a_person("Yes, could you connect me please?")
    assert not asks_for_a_person("No thanks, I don't need to speak to a person.")
    assert not asks_for_a_person("Is the pesto nut free?")


def test_a_complete_readback_written_by_the_model_counts(ctx):
    # Evaluation B01: the model read the details back itself instead of calling the
    # tool. Every detail was in what the caller heard, so their "yes" books it.
    llm = ScriptedLLM(say("Just to confirm: a table for 4 on Wednesday 18 November at 7:30 pm, under the name "
                          "Sam Lee, phone 07700 900555. Shall I book it?"),
                      call("create_reservation", **BOOKING))
    agent = Agent(ctx, llm)
    agent.respond("Sam Lee, 07700 900555")
    t = agent.respond("Yes, that's right.")
    assert t.tool_calls[0].result["status"] == "booked" and rows(ctx) == 1


def test_an_incomplete_readback_still_needs_the_code_readback(ctx):
    llm = ScriptedLLM(say("Lovely, shall I book that for you?"), call("create_reservation", **BOOKING))
    agent = Agent(ctx, llm)
    agent.respond("Sam Lee, 07700 900555, four people tonight at 7:30")
    t = agent.respond("Yes, that's right.")
    assert t.tool_calls[0].result["status"] == "needs_confirmation" and rows(ctx) == 0
