"""The conversation loop: LLM + tools + the code that keeps it honest.

What the LLM does: understand the customer, split two requests into two
tool calls, pick arguments, and phrase the reply.

What code does (here and in tools.py / guardrails.py): validate every
argument, hold a booking until the customer confirms it, refuse to act on
low-confidence transcripts, and check the reply before it is spoken.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta

from . import allergens, guardrails
from .db import WEEKDAYS
from .llm import LLMClient, LLMError, MalformedToolCall
from .schemas import TOOL_SCHEMAS
from .tools import ToolContext, run_tool
from .validation import (
    ToolError,
    parse_date,
    parse_name,
    parse_party_size,
    parse_phone,
    parse_time,
    spoken_date,
    spoken_time,
)

MAX_STEPS = 5  # LLM calls per customer turn
# Calibrated on our own audio: correctly transcribed phone numbers scored as
# low as 0.45 (digit strings lower Whisper's confidence); noise scores ~0.1.
ASR_MIN_CONFIDENCE = 0.40

CLARIFY_LINE = "Sorry, I didn't quite catch that. Could you say it again?"
ALLERGEN_DOWN_LINE = ("I'm sorry, I can't check allergen information right now, so I won't guess. "
                      "I can connect you with a member of our team.")
LLM_DOWN_LINE = ("I'm sorry, I'm having technical trouble right now. "
                 "I can connect you with a member of our team.")

SYSTEM_PROMPT = """\
You are the phone host for {name}, a restaurant in Mumbai. The caller hears everything you write.
Today is {today}. Dates: {calendar}.
Hours: {hours}. Bookings every 15 minutes, 1 to {max_party} people.

Rules:
- Menu, prices, allergens and free tables come only from tools, never from memory.
- Allergens: call get_allergen_info with the allergen the caller named and repeat its "say" text exactly. \
Never judge allergens from ingredients.
- Answer every request in the message, with a tool call for each.
- Booking: once you know party size, date and time, check availability, then ask for name and phone. \
With all five, call create_reservation straight away; it reads the details back to the caller. \
After a clear yes, call it again with the same details.
- Never invent a name or phone number.
- If the time is taken, offer the alternative times the tool gives.
- Only say a table is booked after create_reservation returns status "booked".
- If a tool fails: apologise, say it didn't work, offer to connect them to staff.
- If the message seems misheard or makes no sense, ask a short question. Never guess booking details.
- Offer staff for unconfirmed allergens, parties over {max_party}, failures or complaints. \
Call handoff_to_staff only if the caller agrees or asks for a person. If it fails, give our number, {phone}.
- Reply in one to three short spoken sentences, no lists or markdown. Times like 7:30pm.
- Always reply in English, even if the caller uses another language.
- Before each caller message is a "Call notes" line with the time and what is already done."""


@dataclass
class ToolCallRecord:
    name: str
    args: dict
    result: dict
    ms: float
    forced: bool = False  # called by code (guardrail), not by the LLM


@dataclass
class TurnResult:
    user_text: str
    reply: str
    draft: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    llm_ms: float = 0.0
    llm_calls: int = 0
    tool_ms: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    guard_events: list[str] = field(default_factory=list)
    clarified: bool = False


@dataclass
class _Pending:
    args: dict
    turn: int


def join_phone_groups(text: str) -> str:
    """Whisper writes a spoken mobile number as "98200, 10207". The model then
    read it as two numbers and kept asking for "a 10-digit number" (run 4,
    B05 and B07). Joining the two halves keeps it one number."""
    return re.sub(r"\b([6-9]\d{4})[,.]?\s+(\d{5})\b", r"\1 \2", text)


def tidy(text: str) -> str:
    """Make model output safe to speak: plain spaces and quotes, no markdown."""
    # Run 5, A11: "Yes, the Ro<zero-width space> We need to respond short. Yes, the
    # Roasted...". The model broke off a sentence and leaked its own planning.
    # A zero-width space marks the cut, so the fragment before it goes.
    text = re.sub(r"[^.!?​]*​", " ", text).replace("​", "")
    text = text.translate({0x202F: " ", 0x00A0: " ", 0x2009: " ", 0x2011: "-", 0x2010: "-",
                           0x2013: "-", 0x2014: ", ", 0x2018: "'", 0x2019: "'", 0x201C: '"',
                           0x201D: '"', 0x2026: "..."})
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"^\s*[-\u2022]\s+", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"([?!])\.+", r"\1", text)  # "number?........." -> "number?"
    text = re.sub(r"\.{4,}", ".", text)
    text = re.sub(r"([a-z0-9][.!?])([A-Z])", r"\1 \2", text)  # "digits.And" -> "digits. And"
    text = re.sub(r"\s+([,.!?])", r"\1", text).strip()
    # The model occasionally narrates the conversation ("User hasn't responded
    # yet."). A host never says "user" to a caller, so drop those sentences.
    # Planning notes ("We need to respond short.") are dropped the same way.
    return " ".join(x for x in re.split(r"(?<=[.!?])\s+", text)
                    if not re.search(r"\b(the )?user\b|\bassistant\b", x, re.I)
                    and not re.match(r"(we|i) (need|should|must|have) to (respond|answer|reply|keep)\b", x, re.I))


def _questions_last(reply: str, verified: list[str]) -> str:
    """If code placed verified allergen text after the model's question, ask last.

    Otherwise the caller hears "May I have your name?" and then three more
    sentences before they can answer.
    """
    sentences = guardrails.split_sentences(reply)
    verified_text = " ".join(verified)
    asked = [i for i, x in enumerate(sentences) if x.endswith("?") and x not in verified_text]
    if not asked or not any(x in verified_text for x in sentences[asked[0] + 1:]):
        return reply
    questions = [sentences[i] for i in asked]
    return " ".join([x for i, x in enumerate(sentences) if i not in asked] + questions)


def _error_kind(err: Exception) -> str:
    text = str(err).lower()
    if "per day" in text:
        return "daily quota used up"
    if "429" in text or "rate limit" in text:
        return "rate limited"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "connection" in text:
        return "connection error"
    m = re.search(r"error code: (\d{3})", text)
    return f"provider error {m.group(1)}" if m else "provider error"


def _phone_for_speech(digits: str) -> str:
    return f"{digits[:5]} {digits[5:]}" if len(digits) == 10 else digits


class Agent:
    def __init__(self, ctx: ToolContext, llm: LLMClient | None, *, guards: bool = True,
                 history_turns: int = 12):
        self.ctx = ctx
        self.llm = llm
        self.guards = guards
        self.history_turns = history_turns
        self.turns: list[list[dict]] = []  # messages per turn, OpenAI format
        self.turn_no = 0
        self.pending: _Pending | None = None
        self.bookings: list[dict] = []
        self.handed_off = False

    # ------------------------------------------------------------------ prompt

    def _system(self) -> str:
        """Instructions, dates and hours. Identical for every request made on the
        same day, so the provider can cache it."""
        today = self.ctx.now().date()
        # Every entry keeps its weekday name: an earlier version wrote "tomorrow"
        # instead of "Thu" and the model then mapped "Thursday" to the wrong date.
        calendar = ", ".join(
            f"{WEEKDAYS[d.weekday()][:3]} {d.day} {d:%b}"
            f"{' (today)' if i == 0 else ' (tomorrow)' if i == 1 else ''} = {d.isoformat()}"
            for i, d in enumerate(today + timedelta(days=k) for k in range(8))
        )
        hours = "; ".join(
            f"{day[:3]} " + (" and ".join(f"{s.opens:%H:%M}-{s.closes:%H:%M}" for s in services)
                             if (services := self.ctx.rules.hours.get(i)) else "closed")
            for i, day in enumerate(WEEKDAYS)
        )
        return SYSTEM_PROMPT.format(
            name=self.ctx.rules.name, today=f"{spoken_date(today)} {today.year}",
            calendar=calendar, hours=hours, max_party=self.ctx.rules.max_party,
            phone=self.ctx.rules.phone,
        )

    def _note(self) -> str:
        """Per-turn facts kept by code, so the model doesn't have to remember them."""
        state = [f"Call notes: time now {self.ctx.now():%H:%M}."]
        if self.pending:
            a = self.pending.args
            state.append(f"Waiting for the customer to confirm: table for {a['party_size']} on "
                         f"{a['date']} at {a['time']}, {a['name']}, {a['phone']}.")
        for b in self.bookings:
            state.append(f"Booked on this call: reference {b['reference']}, {b['party_size']} people, "
                         f"{b['date']} {b['time']}.")
        if self.handed_off:
            state.append("A member of staff has already been asked to take over.")
        return " ".join(state)

    def _messages(self, current: list[dict]) -> list[dict]:
        """System prompt + the call so far, append-only.

        History is never rewritten, so each request starts with the previous
        request as an exact prefix and the provider's prompt cache covers it.
        """
        out = [{"role": "system", "content": self._system()}]
        for turn in self.turns[-self.history_turns:]:
            out.extend(turn)
        return out + current

    # ------------------------------------------------------------------ main loop

    def respond(self, user_text: str, confidence: float | None = None) -> TurnResult:
        self.turn_no += 1
        user_text = join_phone_groups(user_text)
        result = TurnResult(user_text=user_text, reply="")

        # A read-back only counts while it is the last thing the caller heard.
        # Found by the evaluation (M05): read-back "Friday", caller "No, I said
        # Thursday", the model asked its own question without a new read-back,
        # caller "Yes" -> the old Friday details were booked.
        if self.pending and self.pending.turn < self.turn_no - 1:
            self.pending = None

        # Never act on doubtful input: a low-confidence transcript gets a
        # clarifying question and is not shown to the LLM at all.
        if self.guards and (not user_text.strip() or
                            (confidence is not None and confidence < ASR_MIN_CONFIDENCE)):
            result.reply = result.draft = CLARIFY_LINE
            result.clarified = True
            result.guard_events.append(f"asr_low_confidence ({confidence})")
            self.turns.append([{"role": "user", "content": "(inaudible)"},
                               {"role": "assistant", "content": CLARIFY_LINE}])
            # "Sorry, could you say that again?" adds nothing new, so a read-back
            # given just before it is still the last real thing the caller heard.
            if self.pending and self.pending.turn == self.turn_no - 1:
                self.pending.turn = self.turn_no
            return result

        # A short, clear "yes" to a read-back the caller heard: code already
        # holds the details, so it books them itself. No LLM call, and no
        # chance for the model to re-type the details wrongly (seen in testing:
        # party_size "1\"" and a phone number cut to 7 digits).
        if self.guards and self._is_plain_yes(user_text):
            return self._book_pending(result)

        current: list[dict] = [{"role": "system", "content": self._note()},
                               {"role": "user", "content": user_text}]
        draft = ""
        try:
            for _ in range(MAX_STEPS):
                reply = self.llm.chat(self._messages(current), TOOL_SCHEMAS)
                result.llm_ms += reply.ms
                result.llm_calls += 1
                result.prompt_tokens += reply.prompt_tokens
                result.cached_tokens += reply.cached_tokens
                result.completion_tokens += reply.completion_tokens
                if not reply.tool_calls:
                    draft = reply.content
                    break
                current.append({
                    "role": "assistant", "content": reply.content or None,
                    "tool_calls": [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.name, "arguments": tc.arguments}}
                                   for tc in reply.tool_calls],
                })
                step = []
                for tc in reply.tool_calls:
                    record = self._call_tool(tc.name, tc.arguments, user_text)
                    step.append(record)
                    result.tool_calls.append(record)
                    result.tool_ms += record.ms
                    current.append({"role": "tool", "tool_call_id": tc.id,
                                    "content": json.dumps(record.result, separators=(",", ":"),
                                                          ensure_ascii=False)})
                if self.guards and (fixed := self._fixed_reply(step)):
                    # Read-backs, booking receipts and handoffs are worded by code:
                    # they must be exact, and it saves an LLM round trip.
                    draft = fixed
                    result.guard_events.append("templated_reply")
                    break
            else:
                draft = ""
        except MalformedToolCall as err:
            # The model fumbled its own tool call three times. The system isn't
            # down, so don't offer staff (a "yes" would then hand the caller over);
            # ask them to say it again, which gives the model a fresh attempt.
            result.guard_events.append(f"malformed_tool_call: {str(err)[:160]}")
            result.clarified = True
            draft = CLARIFY_LINE
        except LLMError as err:
            # Only a category: raw provider messages can contain account details.
            result.guard_events.append(f"llm_error: {_error_kind(err)}")
            draft = LLM_DOWN_LINE

        # The caller asked for a person and the model didn't hand over (seen in
        # the evaluation: "Yes, please put me through" answered with another
        # question about the phone number). Code does it.
        if (self.guards and not self.handed_off and guardrails.asks_for_a_person(user_text)
                and not any(c.name == "handoff_to_staff" for c in result.tool_calls)):
            start = time.perf_counter()
            res = run_tool(self.ctx, "handoff_to_staff", {"reason": "caller asked for a member of staff"})
            record = ToolCallRecord("handoff_to_staff", {"reason": "caller asked for a member of staff"},
                                    res, (time.perf_counter() - start) * 1000, forced=True)
            result.tool_calls.append(record)
            result.tool_ms += record.ms
            self.handed_off = res.get("status") == "handed_off"
            draft = self._fixed_reply([record])
            result.guard_events.append("handoff_requested_by_caller")

        result.draft = draft = tidy(draft)
        result.reply = self._check_reply(draft, result) if self.guards else draft
        if not result.reply and self.guards:
            # The model looked up allergens but never wrote a reply (seen in run 4:
            # four desserts checked, then nothing). The verified sentences are
            # already there, so the caller hears those, not "say that again".
            said = [c.result["say"] for c in result.tool_calls
                    if c.name == "get_allergen_info" and c.result.get("say")]
            if said:
                sentences = re.split(r"(?<=[.!?])\s+", " ".join(said))
                result.reply = " ".join(dict.fromkeys(sentences))
                result.guard_events.append("reply_built_from_verified_sentences")
        if not result.reply:
            result.reply = CLARIFY_LINE
        current.append({"role": "assistant", "content": result.reply})
        self.turns.append(current)
        return result

    def _weekday_mismatch(self, name: str, args, user_text: str) -> dict | None:
        """Code checks the model's date arithmetic against the caller's own words.

        Seen in testing: "Tuesday then" became 2026-11-23, a Monday.
        """
        if name not in ("check_table_availability", "create_reservation") or not isinstance(args, dict):
            return None
        try:
            day = parse_date(args.get("date", ""), self.ctx.now().date())
        except ToolError:
            return None
        text = user_text.lower()
        today = self.ctx.now().date()
        said = [i for i, d in enumerate(WEEKDAYS) if re.search(rf"\b{d.lower()}\b", text)]
        if "tomorrow" in text and not said:
            expected = today + timedelta(days=1)
            if day != expected:
                return {"ok": False, "error": "date_mismatch",
                        "message": f"The caller said tomorrow, which is {expected.isoformat()}."}
            return None
        if len(said) != 1 or day.weekday() == said[0]:
            return None
        correct = today + timedelta(days=(said[0] - today.weekday()) % 7)
        return {"ok": False, "error": "date_mismatch",
                "message": (f"The caller said {WEEKDAYS[said[0]]}, but {day.isoformat()} is a "
                            f"{WEEKDAYS[day.weekday()]}. The coming {WEEKDAYS[said[0]]} is "
                            f"{correct.isoformat()}.")}

    def mark_interrupted(self, heard: str) -> None:
        """The customer talked over the last reply; record what they actually heard.

        If the cut-off reply was a booking read-back, the read-back doesn't
        count: a "yes" to the next question must not confirm details the
        customer never heard.
        """
        if not self.turns:
            return
        last = self.turns[-1][-1]
        if last.get("role") != "assistant":
            return
        full = last["content"]
        if heard.strip() == full.strip():
            return
        last["content"] = (f"{heard.strip()} [cut off: the customer interrupted and did not hear "
                           "the rest]" if heard.strip() else
                           "[the customer interrupted before hearing this reply]")
        if self.pending and self.pending.turn == self.turn_no:
            self.pending = None

    # ------------------------------------------------------------------ tools

    def _call_tool(self, name: str, raw_args: str, user_text: str) -> ToolCallRecord:
        start = time.perf_counter()
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            args = None
        if args is not None and isinstance(args, dict) and self.guards:
            # The model sometimes drops the allergen the caller named ("is there
            # any dairy in...?" -> allergen ""). Fill it from the caller's words so
            # the verified answer is about the right allergen.
            if name == "get_allergen_info" and not args.get("allergen"):
                word = next((w for w in re.findall(r"[a-z]+", user_text.lower())
                             if allergens.keys_for(w)), None)
                if word:
                    args["allergen"] = word
        mismatch = self._weekday_mismatch(name, args, user_text) if self.guards else None
        if args is None or not isinstance(args, dict):
            res = {"ok": False, "error": "bad_arguments", "message": "Arguments were not valid JSON."}
        elif mismatch:
            res = mismatch
        elif name == "create_reservation" and self.guards:
            res = self._create_with_confirmation(args, user_text)
        else:
            res = run_tool(self.ctx, name, args)

        if name == "create_reservation" and res.get("status") == "booked":
            self.bookings.append(res)
            self.pending = None
        elif name == "create_reservation" and res.get("error") == "system_unavailable":
            self.pending = None
        if name == "handoff_to_staff" and res.get("status") == "handed_off":
            self.handed_off = True
        return ToolCallRecord(name, args or {}, res, (time.perf_counter() - start) * 1000)

    def _create_with_confirmation(self, args: dict, user_text: str) -> dict:
        """Two-step booking. The LLM proposes; code books only after a clear yes.

        Step 1 (any turn): validate, check the table is free, remember the
        details and ask the LLM to read them back. Nothing is written.
        Step 2 (a later turn): same details + the customer's message is a
        clear yes -> run the real create_reservation.
        """
        try:
            details = {
                "name": parse_name(args.get("name", "")),
                "phone": parse_phone(args.get("phone", "")),
                "party_size": parse_party_size(args.get("party_size"), self.ctx.rules),
                "date": parse_date(args.get("date", ""), self.ctx.now().date()).isoformat(),
                "time": parse_time(args.get("time", "")).strftime("%H:%M"),
            }
        except ToolError as err:
            return err.as_result()

        # The model must not fill in a name or number the customer never gave
        # (a real failure seen in testing: it invented "John Smith").
        said = " ".join([m["content"] for turn in self.turns for m in turn if m["role"] == "user"]
                        + [user_text]).lower()
        missing = []
        if not all(w in re.findall(r"[a-z']+", said)
                   for w in re.findall(r"[a-z']+", details["name"].lower())):
            missing.append("name")
        if details["phone"][-10:] not in re.sub(r"\D", "", said):
            missing.append("phone")
        if missing:
            return {"ok": False, "error": "details_not_given", "missing": missing,
                    "message": f"The customer hasn't said their {' or '.join(missing)}. "
                               "Ask for it. Never make one up."}

        p = self.pending
        if guardrails.is_affirmative(user_text) and (
                (p and p.args == details and p.turn < self.turn_no)
                or self._last_reply_read_back(details)):
            return run_tool(self.ctx, "create_reservation", details)

        check = run_tool(self.ctx, "check_table_availability",
                         {k: details[k] for k in ("date", "time", "party_size")})
        if not check.get("ok") or not check.get("available"):
            return check

        self.pending = _Pending(details, self.turn_no)
        d = parse_date(details["date"], self.ctx.now().date())
        when = f"{spoken_date(d)} at {spoken_time(parse_time(details['time']))}"
        return {
            "ok": True, "status": "needs_confirmation",
            "message": (f"Not booked yet. Read back: table for {details['party_size']} on {when}, "
                        f"name {details['name']}, phone {_phone_for_speech(details['phone'])}. "
                        "Ask the customer to confirm. Book only after they say yes."),
        }

    def _last_reply_read_back(self, details: dict) -> bool:
        """Did the reply the caller just heard spell out every detail being booked?

        The model sometimes writes its own read-back instead of calling the tool.
        If code can find each detail in what the caller heard just before their
        "yes", that read-back counts; otherwise a code-worded one is required.
        """
        # The last reply that said something (skip "Sorry, could you say that again?").
        replies = [t[-1].get("content") or "" for t in self.turns
                   if t and t[-1].get("role") == "assistant" and t[-1].get("content") != CLARIFY_LINE]
        if not replies or self.turns[-1][-1].get("content") not in (replies[-1], CLARIFY_LINE):
            return False
        heard = replies[-1].lower()
        if "cut off" in heard or "interrupted" in heard:
            return False
        day = parse_date(details["date"], self.ctx.now().date())
        at = parse_time(details["time"])
        squashed = re.sub(r"[\s.]", "", heard)
        number_words = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight"]
        checks = [
            details["name"].lower() in heard,
            details["phone"][-6:] in re.sub(r"\D", "", heard),
            WEEKDAYS[day.weekday()].lower() in heard or re.search(rf"\b{day.day}\b", heard),
            spoken_time(at) in squashed or at.strftime("%H:%M") in heard,
            re.search(rf"\b({details['party_size']}|{number_words[details['party_size']]})\b", heard),
        ]
        return all(checks)

    def _is_plain_yes(self, text: str) -> bool:
        p = self.pending
        if not p or p.turn >= self.turn_no or "?" in text:
            return False
        if len(re.findall(r"[a-z']+", text.lower())) > 7:
            return False
        if re.search(r"\b(staff|person|someone|manager|human|also|and|allerg\w*)\b", text, re.I):
            return False
        return guardrails.is_affirmative(text)

    def _book_pending(self, result: TurnResult) -> TurnResult:
        note = self._note()
        details = self.pending.args
        start = time.perf_counter()
        res = run_tool(self.ctx, "create_reservation", details)
        record = ToolCallRecord("create_reservation", dict(details), res,
                                (time.perf_counter() - start) * 1000, forced=True)
        result.tool_calls.append(record)
        result.tool_ms += record.ms
        result.guard_events.append("confirmed_by_code")
        if res.get("status") == "booked":
            self.bookings.append(res)
            reply = self._fixed_reply([record])
        elif res.get("error") == "system_unavailable":
            reply = ("I'm sorry, our booking system isn't responding, so your table is not booked. "
                     "I can connect you with a member of our team.")
        else:
            reply = f"I'm sorry, I couldn't book that. {res.get('message', '')}".strip()
        self.pending = None  # never retry a failed booking on the next "yes"
        result.reply = result.draft = reply
        self.turns.append([{"role": "system", "content": note},
                           {"role": "user", "content": result.user_text},
                           {"role": "assistant", "content": reply}])
        return result

    def _fixed_reply(self, step: list[ToolCallRecord]) -> str | None:
        """A code-written reply when the step was a single booking or handoff action."""
        if len(step) != 1:
            return None
        res = step[0].result
        if step[0].name == "create_reservation" and res.get("status") in ("needs_confirmation", "booked"):
            a = res if res["status"] == "booked" else self.pending.args
            day = parse_date(a["date"], self.ctx.now().date())
            when = f"{spoken_date(day)} at {spoken_time(parse_time(a['time']))}"
            if res["status"] == "needs_confirmation":
                return (f"Just to confirm: a table for {a['party_size']} on {when}, under the name "
                        f"{a['name']}, phone {_phone_for_speech(a['phone'])}. Shall I book that?")
            return (f"You're booked: a table for {a['party_size']} on {when} under the name "
                    f"{a['name']}. Your reference number is {res['reference']}.")
        if step[0].name == "handoff_to_staff" and res.get("status") == "handed_off":
            return "I've passed you to a member of our team, and they'll be with you shortly."
        if step[0].name == "handoff_to_staff" and res.get("error") == "system_unavailable":
            # Offering "a member of staff" again would be the thing that just failed.
            return ("I'm sorry, I can't put you through right now. Please call us directly on "
                    f"{self.ctx.rules.phone}.")
        return None

    # ------------------------------------------------------------------ reply checks

    def _check_reply(self, draft: str, result: TurnResult) -> str:
        if not draft:
            return draft
        calls = result.tool_calls
        verified = [c.result["say"] for c in calls
                    if c.name == "get_allergen_info" and c.result.get("say")]
        # The allergen lookup itself failed: the caller must hear plainly that
        # allergens can't be checked right now (the model once said only "I
        # couldn't get that information").
        if not verified and any(c.name == "get_allergen_info"
                                and c.result.get("error") == "system_unavailable" for c in calls):
            verified = [ALLERGEN_DOWN_LINE]

        # An allergy question must never be dropped. In testing the model once
        # answered the booking half of "...tonight? Also, does the pesto contain
        # nuts? I'm allergic." and ignored the allergy. If the caller asked
        # about allergens in a dish and no lookup happened, code does it.
        if guardrails.asks_about_allergens(result.user_text) and not any(
                c.name == "get_allergen_info" for c in calls):
            says = self._forced_allergen_lookup(result, result.user_text)
            if says:
                verified = verified + says
                result.guard_events.append("allergen_question_answered_by_code")

        def lookup() -> list[str]:
            return self._forced_allergen_lookup(result)

        reply, events = guardrails.enforce_allergen_rule(draft, verified, lookup)
        result.guard_events += events
        if verified:
            reply = _questions_last(reply, verified)
        if ALLERGEN_DOWN_LINE in reply:
            # The plain outage line already apologises; drop the model's vaguer one.
            vague = re.compile(r"^(i'?m )?sorry,? (but )?i (couldn'?t|could not|can'?t|cannot|wasn'?t able to)"
                               r" (get|find|check|access|retrieve)\b(?!.*allergen)", re.I)
            reply = " ".join(s for s in guardrails.split_sentences(reply) if not vague.search(s))
        reply, events = guardrails.enforce_action_claims(
            reply,
            booked=bool(self.bookings),
            handed_off=self.handed_off,
            tool_failed=any(c.result.get("error") == "system_unavailable" for c in calls),
        )
        result.guard_events += events
        return reply

    def _forced_allergen_lookup(self, result: TurnResult, text: str | None = None) -> list[str]:
        """Look up allergens for the dishes being discussed, without the model."""
        text = text if text is not None else f"{result.user_text} {result.draft}"
        allergen = next((w for w in re.findall(r"[a-z]+", result.user_text.lower())
                         if guardrails._STRONG.match(w) or guardrails._WEAK.match(w)), None)
        dishes = self._dishes_mentioned(text)
        if not dishes or len(dishes) > 3:
            return []
        says = []
        for dish in dishes:
            start = time.perf_counter()
            res = run_tool(self.ctx, "get_allergen_info", {"dish": dish, "allergen": allergen})
            result.tool_calls.append(ToolCallRecord("get_allergen_info", {"dish": dish, "allergen": allergen},
                                                    res, (time.perf_counter() - start) * 1000, forced=True))
            if res.get("say"):
                says.append(res["say"])
            elif res.get("error") == "system_unavailable":
                return [ALLERGEN_DOWN_LINE]
        return says

    def _dishes_mentioned(self, text: str) -> list[str]:
        words = set(re.findall(r"[a-z]+", text.lower()))
        generic = {"and", "of", "the", "al", "day", "with", "garden", "roasted", "pan", "wild", "dark"}
        found = []
        for (dish,) in self.ctx.conn.execute("SELECT dish FROM menu"):
            tokens = {w for w in re.findall(r"[a-z]+", dish.lower()) if w not in generic and len(w) > 3}
            if tokens & words:
                found.append(dish)
        return found
