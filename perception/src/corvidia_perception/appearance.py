"""Compile supported clothing requests and compare independent observations.

This module does not infer attributes from pixels or estimate confidence. Every
word of a request must fit the supported grammar; unsupported traits are errors.
``shirt`` is an ordinary umbrella for shirt, T-shirt, and polo. A request for a
``polo`` or ``t_shirt`` requires that exact garment observation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import unicodedata


VERSION = "appearance-v1"
SUBJECTS = ("human", "nonhuman", "unknown")
UPPER_COLORS = (
    "black", "white", "gray", "red", "orange", "yellow", "green", "blue",
    "purple", "pink", "brown", "beige", "multicolor", "unknown",
)
UPPER_GARMENTS = (
    "t_shirt", "polo", "shirt", "hoodie", "sweater", "jacket", "coat", "vest",
    "tank_top", "unknown",
)


class AppearanceValidationError(ValueError):
    """The entire requested appearance cannot be represented safely."""


@dataclass(frozen=True)
class AppearanceRequirements:
    subject: str = "human"
    upper_color: str | None = None
    upper_garment: str | None = None

    def __post_init__(self):
        if (self.subject != "human"
                or self.upper_color not in (None, *UPPER_COLORS[:-1])
                or self.upper_garment not in (None, *UPPER_GARMENTS[:-1])
                or (self.upper_color is not None and self.upper_garment is None)):
            raise AppearanceValidationError("Invalid supported appearance requirements")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AppearanceEvaluation:
    result: str  # matched, rejected, or unknown; never a confidence value
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


_COLORS = {color: color for color in UPPER_COLORS if color != "unknown"}
_COLORS.update({"grey": "gray", "multicolored": "multicolor", "multicoloured": "multicolor",
                "multi-colored": "multicolor", "multi-coloured": "multicolor"})
_GARMENTS = {garment: garment for garment in UPPER_GARMENTS if garment != "unknown"}
_GARMENTS.update({
    "polo shirt": "polo", "t-shirt": "t_shirt", "t shirt": "t_shirt", "tshirt": "t_shirt",
    "tee shirt": "t_shirt", "tank top": "tank_top", "tank-top": "tank_top",
    "hooded sweatshirt": "hoodie",
})
# Internal schema spellings are not part of natural-language input.
_GARMENTS.pop("t_shirt")
_GARMENTS.pop("tank_top")

_ACTION = re.compile(
    r"^(?:please )?(?:find|locate|detect|search for|look for|photograph|capture|"
    r"take (?:a |an )?(?:photo|picture|image|screenshot) of) (.+)$"
)
_PERSON = re.compile(r"^(?:(?:a|the|any) )?(?:person|human|someone|somebody|anyone)(?: (.+))?$")
_CLOTHING = re.compile(r"^(?:(?:who is|that is|is) )?(?:wearing|dressed in|in|with) (.+)$")
_ARTICLE = re.compile(r"^(?:a|an|the) ")


def compile_appearance(value) -> AppearanceRequirements:
    """Accept a generic person or one upper garment with an optional color.

    Examples: ``find someone``, ``person wearing a blue polo shirt``, ``red
    shirt``, and ``white hoodie``. Negation, shade qualifiers, patterns,
    accessories, demographic/identity traits, and multiple garments are not
    represented by this version and therefore cannot silently be ignored.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise AppearanceValidationError("Describe a person in 1–240 characters")
    if any(unicodedata.category(char).startswith("C")
           or unicodedata.category(char) in {"Zl", "Zp"} for char in value):
        raise AppearanceValidationError("Use a single-line description without control characters")
    text = " ".join(value.strip().casefold().split())
    if text[-1:] in {".", "!", "?"}:
        text = text[:-1].rstrip()
    action = _ACTION.fullmatch(text)
    if action:
        text = action.group(1)
    person = _PERSON.fullmatch(text)
    if person:
        if person.group(1) is None:
            return AppearanceRequirements()
        clothing = _CLOTHING.fullmatch(person.group(1))
        text = clothing.group(1) if clothing else ""
    else:
        clothing = _CLOTHING.fullmatch(text)
        if clothing:
            text = clothing.group(1)
    text = _ARTICLE.sub("", text, count=1)
    if text in _GARMENTS:
        return AppearanceRequirements(upper_garment=_GARMENTS[text])
    for color_text, color in _COLORS.items():
        prefix = color_text + " "
        if text.startswith(prefix) and text[len(prefix):] in _GARMENTS:
            return AppearanceRequirements(upper_color=color, upper_garment=_GARMENTS[text[len(prefix):]])
    raise AppearanceValidationError(
        "Use a person or one upper garment, such as 'a person wearing a blue polo'. "
        "Extra traits, multiple garments, and negation are not supported."
    )


def evaluate_appearance(requirements: AppearanceRequirements, observed) -> AppearanceEvaluation:
    """Require a valid observation and every requested trait to match.

    The model receives no desired attribute values. This comparison does not
    make its observations reliable by itself; visual accuracy needs evaluation.
    Unknown unrequested attributes do not prevent a generic person match.
    """
    if not isinstance(requirements, AppearanceRequirements):
        raise TypeError("requirements must come from compile_appearance")
    vocabularies = {"subject": SUBJECTS, "upper_color": UPPER_COLORS, "upper_garment": UPPER_GARMENTS}
    if (not isinstance(observed, dict) or set(observed) != set(vocabularies)
            or any(not isinstance(observed[key], str) or observed[key] not in vocabulary
                   for key, vocabulary in vocabularies.items())):
        return AppearanceEvaluation("unknown", "unsupported_observations")
    if observed["subject"] == "nonhuman":
        return AppearanceEvaluation("rejected", "subject_nonhuman")
    if observed["subject"] == "unknown":
        return AppearanceEvaluation("unknown", "subject_unknown")
    unknown = None
    for key in ("upper_color", "upper_garment"):
        required = getattr(requirements, key)
        if required is None:
            continue
        actual = observed[key]
        if actual == "unknown":
            unknown = unknown or key + "_unknown"
            continue
        allowed = {"shirt", "t_shirt", "polo"} if key == "upper_garment" and required == "shirt" else {required}
        if actual not in allowed:
            return AppearanceEvaluation("rejected", key + "_mismatch")
    if unknown:
        return AppearanceEvaluation("unknown", unknown)
    return AppearanceEvaluation("matched", "appearance_match")
