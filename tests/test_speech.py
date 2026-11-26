"""Text normalisation for the voice: what Piper is asked to say."""

from bistrovoice.agent import tidy
from bistrovoice.speech import for_speech


def test_phone_numbers_are_read_digit_by_digit():
    assert for_speech("phone 98200 10555.") == "phone 9 8 2 0 0, 1 0 5 5 5."


def test_reference_numbers_are_spelled_out():
    assert for_speech("Your reference number is 1028.") == "Your reference number is 1 0 2 8."


def test_prices():
    assert for_speech("It's ₹1,250, the sorbet is Rs 225.") == "It's 1250 rupees, the sorbet is 225 rupees."


def test_times():
    assert for_speech("at 19:30") == "at 7 30 P M"
    assert for_speech("at 7:30pm") == "at 7 30 P M"
    assert for_speech("at 9pm or 21:15") == "at 9 P M or 9 15 P M"
    # a bare "7:30" is how people say it, not 07:30 in the morning
    assert for_speech("a table at 7:30 tonight") == "a table at 7:30 tonight"
    assert for_speech("open 12:00-15:00") == "open 12 P M-3 P M"


def test_ampersand():
    assert for_speech("Olive & Ember") == "Olive and Ember"


def test_model_output_is_tidied_before_speaking():
    raw = "**Sure!** We’re open at 12 pm — see you.\n\n- Bring friends"
    assert tidy(raw) == "Sure! We're open at 12 pm, see you. Bring friends"
    assert tidy("It's 11 digits.And the date?") == "It's 11 digits. And the date?"
    assert tidy("It costs 1250 rupees.") == "It costs 1250 rupees."


def test_mumbai_landline():
    assert for_speech("call us on 022 4000 0321.") == "call us on 0 2 2, 4 0 0 0, 0 3 2 1."


def test_long_sentences_are_spoken_in_short_pieces():
    from bistrovoice.speech import Speaker

    assert Speaker.sentences(None, "I'm sorry, I can't confirm the allergens in the Sticky Toffee Pudding "
                                   "right now, so I won't guess. I can connect you.") == [
        "I'm sorry,", "I can't confirm the allergens in the Sticky Toffee Pudding right now,",
        "so I won't guess.", "I can connect you."]
    assert Speaker.sentences(None, "Just to confirm: a table for 4 on Thursday 26 November at 7:30pm, under "
                                   "the name Priya Nair, phone 98200 10211. Shall I book that?") == [
        "Just to confirm: a table for 4 on Thursday 26 November at 7:30pm,",
        "under the name Priya Nair, phone 98200 10211.", "Shall I book that?"]
    assert Speaker.sentences(None, "Yes, that's booked.") == ["Yes, that's booked."]


def test_spoken_text_is_cut_into_short_runs():
    from bistrovoice.speech import _pieces

    spoken = "under the name Priya Nair, phone 9 8 2 0 0, 1 0 2 1 1."
    pieces = _pieces(spoken, 40)
    assert all(len(p) <= 40 for p in pieces)
    assert " ".join(pieces) == spoken


def test_leaked_planning_is_not_spoken():
    raw = ("Yes, the Ro​ We need to respond short. Yes, the Roasted Cauliflower Steak contains sesame. "
           "The sesame comes from tahini.")
    assert tidy(raw) == ("Yes, the Roasted Cauliflower Steak contains sesame. The sesame comes from tahini.")
    assert tidy("I need to check that. One moment.") == "I need to check that. One moment."
