from addon_fixtures import GOOD
from orch.addons.manifest import parse_manifest
from orch.addons.settings import form_value, parse_settings

M = parse_manifest({**GOOD, "settings_schema": [
    {"key": "greeting", "label": "Greeting", "type": "text", "default": "Hello"},
    {"key": "scope", "label": "Scope", "type": "select", "options": ["mine", "prefix", "all"], "default": "mine"},
    {"key": "show_clean", "label": "Show clean repos", "type": "bool"},
    {"key": "envs", "label": "Environments", "type": "map"},
]})


def test_valid_form():
    values, errors = parse_settings(M, {"greeting": " Hi ", "scope": "prefix", "show_clean": "1",
                                        "envs": "dev = acme_test\n\nprod = prod_profile\n", "sneaky": "x"})
    assert errors == []
    assert values == {"greeting": "Hi", "scope": "prefix", "show_clean": True,
                      "envs": {"dev": "acme_test", "prod": "prod_profile"}}


def test_unchecked_bool_is_false():
    assert parse_settings(M, {"scope": "mine"})[0]["show_clean"] is False


def test_errors():
    _, errors = parse_settings(M, {"greeting": "a\nb", "scope": "everyone", "envs": "no equals sign"})
    assert any("Greeting" in e for e in errors) and any("Scope" in e for e in errors) and any("Environments" in e for e in errors)
    _, errors = parse_settings(M, {"scope": "mine", "envs": "\n".join(f"k{i} = v" for i in range(51))})
    assert any("at most 50" in e for e in errors)
    _, errors = parse_settings(M, {"scope": "mine", "greeting": "x" * 501})
    assert any("500" in e for e in errors)


def test_form_values():
    assert form_value(M.field("greeting"), {}) == "Hello"
    assert form_value(M.field("envs"), {"envs": {"dev": "a"}}) == "dev = a"
    assert form_value(M.field("show_clean"), {"show_clean": True}) is True
