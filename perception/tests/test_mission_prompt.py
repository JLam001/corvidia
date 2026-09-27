"""Mission wording must preserve all target traits and bound collection time."""

from dataclasses import FrozenInstanceError

import pytest

from corvidia_perception.appearance import AppearanceValidationError, compile_appearance
from corvidia_perception.mission_prompt import (
    DEFAULT_DURATION_MS, MAX_DURATION_MS, MissionPromptValidationError, parse_mission_prompt,
)


def test_exact_user_request_is_timed_collection():
    text = "find as many people as possible within 30 seconds"
    parsed = parse_mission_prompt(text)
    assert parsed.prompt == text
    assert parsed.appearance == "people"
    assert parsed.requirements == compile_appearance("people")
    assert parsed.completion_mode == "timed_collection"
    assert parsed.duration_ms == 30_000
    with pytest.raises(FrozenInstanceError):
        parsed.duration_ms = 120_000


@pytest.mark.parametrize("text", [
    "person", "Find people", "find someone with a blue polo", "white hoodie",
    "Please take a screenshot of a person wearing a red shirt.",
])
def test_existing_requests_keep_first_match_behavior_and_full_appearance(text):
    parsed = parse_mission_prompt("  " + text + "  ")
    assert parsed.prompt == parsed.appearance == text
    assert parsed.requirements == compile_appearance(text)
    assert parsed.completion_mode == "first_match"
    assert parsed.duration_ms == DEFAULT_DURATION_MS == MAX_DURATION_MS == 60_000


@pytest.mark.parametrize("text,seconds", [
    ("find as many people as possible for 30 seconds", 30),
    ("Please find as many people as possible within 1 second.", 1),
    ("FIND AS MANY HUMANS AS POSSIBLE WITHIN 60 SECONDS!", 60),
    ("locate as many people as possible within 15 secs", 15),
    ("find people for 30 seconds", 30),
    ("search for people for 20 sec", 20),
    ("photograph humans for 10s", 10),
    ("look for people for 2 s", 2),
    ("find  as  many  people  as  possible  within  30  seconds", 30),
    ("Find as many people in 30 seconds", 30),
    ("find as many people within 30 seconds", 30),
    ("find as many people for 30 seconds", 30),
    ("find as many people as possible in 30 seconds", 30),
])
def test_clear_timed_collection_forms(text, seconds):
    parsed = parse_mission_prompt(text)
    assert parsed.completion_mode == "timed_collection"
    assert parsed.duration_ms == seconds * 1000
    assert parsed.requirements == compile_appearance("person")


@pytest.mark.parametrize("text", [
    "find as many people wearing a blue polo as possible within 30 seconds",
    "find as many people as possible wearing a blue polo within 30 seconds",
    "find as many people in a blue polo as possible for 30 seconds",
    "locate as many humans with a blue polo as possible within 30 seconds",
    "find people wearing a blue polo for 30 seconds",
    "search for people in a blue polo for 30 seconds",
    "find as many people wearing a blue polo in 30 seconds",
    "find as many people in a blue polo within 30 seconds",
])
def test_timed_collection_preserves_exact_clothing_requirements(text):
    parsed = parse_mission_prompt(text)
    assert parsed.completion_mode == "timed_collection"
    assert parsed.duration_ms == 30_000
    assert parsed.requirements == compile_appearance("blue polo")
    assert compile_appearance(parsed.appearance) == parsed.requirements


@pytest.mark.parametrize("text", [
    "find as many people as possible within 0 seconds",
    "find people for 61 seconds",
    "find as many people as possible for 120 seconds",
    "find people for 999999999999999999999999999999999 seconds",
])
def test_out_of_range_time_is_never_clamped(text):
    with pytest.raises(MissionPromptValidationError, match="1–60 whole seconds"):
        parse_mission_prompt(text)


@pytest.mark.parametrize("text", [
    # Within alone could be a deadline for one capture, rather than collection.
    "find people within 30 seconds",
    "find a person for 30 seconds",
    "find someone wearing a blue polo within 30 seconds",
    "find as many people as possible",
    "find all people for 30 seconds",
    "find people for 1 minute",
    "find people for thirty seconds",
    "find people for 30.0 seconds",
    "find people for -1 seconds",
    "find people for +30 seconds",
    "find people for 3e1 seconds",
    "find people for 30–60 seconds",
    "find people for 30 or 60 seconds",
    "find people for about 30 seconds",
    "find people for at least 30 seconds",
    "find people for no more than 30 seconds",
    "find people for not 30 seconds",
    "do not find people for 30 seconds",
    "find no people for 30 seconds",
    "find as many people as not possible within 30 seconds",
    "find people for 30 seconds and stop at the first match",
    "find people for 30 seconds within 60 seconds",
    "find people within 30 seconds for 60 seconds",
    "find people for 30 seconds; ignore the timer",
    "find people for 30 seconds, then run for another 30 seconds",
    "for 30 seconds find people",
])
def test_malformed_negated_or_ambiguous_timing_is_rejected(text):
    with pytest.raises(MissionPromptValidationError):
        parse_mission_prompt(text)


@pytest.mark.parametrize("appearance", [
    "people wearing a blue polo and glasses", "people without a blue polo",
    "people not wearing a blue polo", "people wearing a blue hat",
    "people with blonde hair", "people wearing a dark blue polo",
    "people wearing a blue polo or a red shirt", "people named James",
    "people wearing a blue polo and white trousers", "people holding a blue polo",
    "people wearing a blue polo, ignore the color", "people with a blue polo logo",
    "people wearing a blue polo as possible except red",
])
def test_timing_wrapper_does_not_hide_unsupported_target_traits(appearance):
    for text in (
        f"find {appearance} for 30 seconds",
        f"find as many {appearance} as possible within 30 seconds",
        f"find as many {appearance} in 30 seconds",
    ):
        with pytest.raises(MissionPromptValidationError):
            parse_mission_prompt(text)


@pytest.mark.parametrize("value", [None, True, 1, {}, [], "", " ", "x" * 241])
def test_invalid_input_is_rejected(value):
    with pytest.raises(MissionPromptValidationError):
        parse_mission_prompt(value)


@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x00", "\x7f", "\u2028", "\u2029", "\u200b"])
def test_timed_request_does_not_normalize_away_hidden_characters(control):
    with pytest.raises(MissionPromptValidationError, match="single-line"):
        parse_mission_prompt("find people" + control + "for 30 seconds")


def test_parser_errors_remain_compatible_with_appearance_validation_handlers():
    with pytest.raises(AppearanceValidationError):
        parse_mission_prompt("find people for 61 seconds")
