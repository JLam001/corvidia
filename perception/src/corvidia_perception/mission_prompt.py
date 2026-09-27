"""Compile a mission brief without discarding clothing or timing constraints.

Ordinary descriptions stop at the first match, with a 60-second search limit.
Explicit collection requests continue saving matches for a bounded duration.
Only the collection wrapper is removed; the entire remaining description must
pass the same strict appearance grammar used by ordinary missions.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal
import unicodedata

from .appearance import AppearanceRequirements, AppearanceValidationError, compile_appearance


DEFAULT_DURATION_MS = 60_000
MAX_DURATION_MS = 60_000


class MissionPromptValidationError(AppearanceValidationError):
    """The mission brief cannot be represented in full."""


@dataclass(frozen=True)
class ParsedMissionPrompt:
    prompt: str
    appearance: str
    requirements: AppearanceRequirements
    completion_mode: Literal["first_match", "timed_collection"]
    duration_ms: int


_ACTIONS = r"(?:find|locate|detect|search for|look for|photograph|capture)"
_TIMED_SUFFIX = re.compile(r"^(.+) (within|for) ([0-9]+)\s*(?:seconds?|secs?|s)$")
_MANY = re.compile(r"^(?:please )?" + _ACTIONS + r" as many (people|humans)(.*)$")
_PLURAL = re.compile(r"^(?:please )?" + _ACTIONS + r" (people|humans)(?: .+)?$")
_TIMING_HELP = (
    "For a timed collection, use 'find as many people as possible within 30 seconds' "
    "or 'find people wearing a blue polo for 30 seconds'."
)


def _requirements(appearance: str) -> AppearanceRequirements:
    try:
        return compile_appearance(appearance)
    except AppearanceValidationError as exc:
        raise MissionPromptValidationError(str(exc)) from exc


def parse_mission_prompt(raw) -> ParsedMissionPrompt:
    """Return explicit completion semantics and compiled appearance requirements.

    Supported timed forms are ``find as many people [wearing ...] as possible
    within/for N seconds`` and ``find people [wearing ...] for N seconds``.
    Clothing may also follow ``as possible``. ``N`` must be a whole number from
    1 through 60; ``s``, ``sec``, and ``secs`` are accepted unit abbreviations.
    A bare ``within`` deadline is intentionally rejected because it does not
    state whether to collect repeatedly or stop at the first match.
    """
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 240:
        raise MissionPromptValidationError("Describe a mission in 1–240 characters")
    if any(unicodedata.category(char).startswith("C")
           or unicodedata.category(char) in {"Zl", "Zp"} for char in raw):
        raise MissionPromptValidationError("Use a single-line mission without control characters")
    prompt = raw.strip()
    text = " ".join(prompt.casefold().split())
    if text[-1:] in {".", "!", "?"}:
        text = text[:-1].rstrip()
    timed = _TIMED_SUFFIX.fullmatch(text)
    if timed is None:
        return ParsedMissionPrompt(
            prompt, prompt, _requirements(prompt), "first_match", DEFAULT_DURATION_MS,
        )

    body, relation, seconds_text = timed.groups()
    duration_ms = int(seconds_text) * 1000
    if not 1000 <= duration_ms <= MAX_DURATION_MS:
        raise MissionPromptValidationError("Timed collections must last 1–60 whole seconds")
    many = _MANY.fullmatch(body)
    if many:
        subject, clothing = many.groups()
        if clothing.startswith(" as possible"):
            clothing = clothing[len(" as possible"):]
        elif clothing.endswith(" as possible"):
            clothing = clothing[:-len(" as possible")]
        else:
            raise MissionPromptValidationError(_TIMING_HELP)
        appearance = subject + clothing
    elif relation == "for" and _PLURAL.fullmatch(body):
        appearance = body
    else:
        raise MissionPromptValidationError(_TIMING_HELP)
    return ParsedMissionPrompt(
        prompt, appearance, _requirements(appearance), "timed_collection", duration_ms,
    )
