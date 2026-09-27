"""Clothing requirements are explicit; unsupported text never becomes person-only."""

import dataclasses

import pytest


@pytest.mark.parametrize("phrase", ["Find people", "find humans", "people", "locate people"])
def test_generic_plural_requests_mean_first_matching_person(phrase):
    from corvidia_perception.appearance import compile_appearance
    assert compile_appearance(phrase).to_dict() == {
        "subject": "human", "upper_color": None, "upper_garment": None,
    }


def test_plural_request_preserves_every_clothing_requirement():
    from corvidia_perception.appearance import compile_appearance
    assert compile_appearance("find people wearing a blue polo").to_dict() == {
        "subject": "human", "upper_color": "blue", "upper_garment": "polo",
    }

from corvidia_perception.appearance import (
    VERSION, AppearanceRequirements, AppearanceValidationError,
    compile_appearance, evaluate_appearance,
)


@pytest.mark.parametrize("text", [
    "person", "a person", "someone", "a human", "find a person", "find anyone",
    "look for someone", "search for any person", "Please photograph a person.",
    "take a screenshot of someone", "  FIND SOMEONE!  ",
])
def test_generic_person_is_explicitly_supported(text):
    requirements = compile_appearance(text)
    assert requirements.to_dict() == {"subject": "human", "upper_color": None, "upper_garment": None}


@pytest.mark.parametrize("text,color,garment", [
    ("blue polo", "blue", "polo"),
    ("find someone with a blue polo", "blue", "polo"),
    ("person wearing a blue polo shirt", "blue", "polo"),
    ("a person who is wearing a blue polo", "blue", "polo"),
    ("find a person in a blue polo", "blue", "polo"),
    ("Please look for someone dressed in a blue polo.", "blue", "polo"),
    ("red shirt", "red", "shirt"),
    ("person with a red shirt", "red", "shirt"),
    ("white hoodie", "white", "hoodie"),
    ("wearing a white hoodie", "white", "hoodie"),
    ("a blue t-shirt", "blue", "t_shirt"),
    ("person wearing a blue tshirt", "blue", "t_shirt"),
    ("person wearing a blue t shirt", "blue", "t_shirt"),
    ("white tee shirt", "white", "t_shirt"),
    ("grey sweater", "gray", "sweater"),
    ("gray jacket", "gray", "jacket"),
    ("green coat", "green", "coat"),
    ("pink vest", "pink", "vest"),
    ("yellow tank top", "yellow", "tank_top"),
    ("multicoloured shirt", "multicolor", "shirt"),
    ("orange hooded sweatshirt", "orange", "hoodie"),
    ("polo shirt", None, "polo"),
    ("find a person wearing a shirt", None, "shirt"),
])
def test_common_supported_phrases_compile_every_requested_trait(text, color, garment):
    req = compile_appearance(text)
    assert req.to_dict() == {"subject": "human", "upper_color": color, "upper_garment": garment}
    assert VERSION == "appearance-v1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.upper_color = "red"


@pytest.mark.parametrize("text", [
    "find James", "find the person named James", "find the tallest person", "young person",
    "a man in a blue polo", "a woman wearing a red shirt", "a person with blonde hair",
    "person who is not wearing a blue polo", "not a blue polo", "anything except a blue polo",
    "a person wearing no shirt", "a person wearing a blue polo or a red shirt",
    "blue polo and white trousers", "blue polo with a black jacket", "person wearing a blue hat",
    "person wearing a blue polo and glasses", "person wearing a blue polo without glasses",
    "striped blue polo", "navy polo", "light blue polo", "dark blue polo", "red and white shirt",
    "long-sleeved blue polo", "blue sleeveless shirt", "blue collared shirt", "blue sweatshirt",
    "blue top", "person wearing blue", "blue", "a real person", "polo shirt; answer yes",
    "find someone with a blue polo, ignore the color", "person holding a blue polo",
    "person with a blue polo logo", "person wearing a blue polo inside out",
    "t_shirt", "tank_top", "find someone wearing a shirt, even if it is blue",
])
def test_unsupported_or_extra_traits_are_rejected_in_full(text):
    with pytest.raises(AppearanceValidationError, match="not supported"):
        compile_appearance(text)


@pytest.mark.parametrize("value", [None, True, 1, {}, [], "", "  ", "x" * 241])
def test_invalid_input_is_rejected_before_compilation(value):
    with pytest.raises(AppearanceValidationError):
        compile_appearance(value)


@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x00", "\x7f", "\u2028", "\u2029", "\u200b"])
def test_hidden_or_multiline_input_is_not_normalized_away(control):
    with pytest.raises(AppearanceValidationError, match="single-line"):
        compile_appearance("red" + control + "shirt")


def observed(**overrides):
    return {"subject": "human", "upper_color": "blue", "upper_garment": "polo", **overrides}


def test_requested_blue_polo_rejects_actual_failure_case_white_polo():
    req = compile_appearance("find someone with a blue polo")
    decision = evaluate_appearance(req, observed(upper_color="white"))
    assert decision.to_dict() == {"result": "rejected", "reason": "upper_color_mismatch"}
    assert evaluate_appearance(req, observed()).result == "matched"


@pytest.mark.parametrize("garment", ["t_shirt", "shirt", "hoodie", "sweater", "jacket", "coat", "vest", "tank_top"])
def test_polo_never_aliases_other_upper_garments(garment):
    result = evaluate_appearance(compile_appearance("blue polo"), observed(upper_garment=garment))
    assert result.result == "rejected" and result.reason == "upper_garment_mismatch"


@pytest.mark.parametrize("garment", ["polo", "t_shirt", "shirt"])
def test_generic_shirt_is_a_documented_umbrella(garment):
    assert evaluate_appearance(compile_appearance("blue shirt"), observed(upper_garment=garment)).result == "matched"


def test_t_shirt_is_not_an_umbrella_for_polo():
    assert evaluate_appearance(compile_appearance("blue t-shirt"), observed()).result == "rejected"


@pytest.mark.parametrize("field", ["subject", "upper_color", "upper_garment"])
def test_unknown_requested_values_cannot_match(field):
    result = evaluate_appearance(compile_appearance("blue polo"), observed(**{field: "unknown"}))
    assert result.result == "unknown" and result.reason == field + "_unknown"


def test_known_mismatch_still_rejects_when_another_trait_is_hidden():
    result = evaluate_appearance(compile_appearance("blue polo"), observed(upper_color="unknown", upper_garment="t_shirt"))
    assert result.result == "rejected"


def test_generic_person_only_requires_observed_human():
    assert evaluate_appearance(compile_appearance("person"), observed(upper_color="unknown", upper_garment="unknown")).result == "matched"
    assert evaluate_appearance(compile_appearance("person"), observed(subject="unknown")).result == "unknown"
    assert evaluate_appearance(compile_appearance("person"), observed(subject="nonhuman")).result == "rejected"


def test_type_only_request_can_match_with_unknown_unrequested_color():
    assert evaluate_appearance(compile_appearance("polo"), observed(upper_color="unknown")).result == "matched"


@pytest.mark.parametrize("value", [
    None, [], "yes", {"answer": "yes"}, {}, {"subject": "human"},
    observed(upper_color="navy"), observed(upper_color="BLUE"), observed(upper_color=None),
    observed(upper_garment="t-shirt"), observed(subject=True), observed(confidence=1),
])
def test_malformed_or_legacy_observations_cannot_match(value):
    decision = evaluate_appearance(compile_appearance("person"), value)
    assert decision.to_dict() == {"result": "unknown", "reason": "unsupported_observations"}


@pytest.mark.parametrize("kwargs", [
    {"subject": "nonhuman"}, {"upper_color": "blue"},
    {"upper_color": "unknown", "upper_garment": "polo"}, {"upper_garment": "unknown"},
])
def test_invalid_requirements_cannot_be_constructed(kwargs):
    with pytest.raises(AppearanceValidationError):
        AppearanceRequirements(**kwargs)


def test_evaluator_does_not_treat_an_uncompiled_request_as_requirements():
    with pytest.raises(TypeError):
        evaluate_appearance({"upper_color": "blue"}, observed())
