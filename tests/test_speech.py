"""Text normalisation for the voice: what Piper is asked to say."""

from bistrovoice.agent import tidy
from bistrovoice.speech import for_speech


def test_phone_numbers_are_read_digit_by_digit():
    assert for_speech("phone 07700 900555.") == "phone 0 7 7 0 0, 9 0 0, 5 5 5."


def test_reference_numbers_are_spelled_out():
    assert for_speech("Your reference number is 1028.") == "Your reference number is 1 0 2 8."


def test_prices():
    assert for_speech("It's £16.50, the sorbet is £6.00.") == "It's 16 pounds 50, the sorbet is 6 pounds."


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
    assert tidy("It costs 7.50 pounds.") == "It costs 7.50 pounds."


def test_london_number():
    assert for_speech("call us on 020 7946 0321.") == "call us on 0 2 0, 7 9 4 6, 0 3 2 1."


def test_long_first_sentence_starts_with_a_short_clause():
    from bistrovoice.speech import Speaker

    split = Speaker.sentences(None, "I'm sorry, I can't confirm the allergens in the Sticky Toffee Pudding "
                                    "right now, so I won't guess. I can connect you.")
    assert split == ["I'm sorry,", "I can't confirm the allergens in the Sticky Toffee Pudding right now, "
                     "so I won't guess.", "I can connect you."]
    assert Speaker.sentences(None, "Yes, that's booked.") == ["Yes, that's booked."]
