"""The addon-api 1.1 contract (G6, #924): ``{python}``, ``choose_model``, ``credential``, ``settings`` and the reserved
official ids. T0: ``uv run pytest tests/addons/test_api_1_1_contract.py -x -q``"""

from __future__ import annotations

import copy
import json

import pytest

from orch import schema
from orch.addons.manifest import ADDON_API, CAPABILITIES, ManifestError, incompatibility, load_manifest
from orch.host import signcontent
from orch.market import MarketError, constants
from orch.market import install as mi
from tests.addons.helpers import manifest

V3 = {"schema": "orch.addon/3", "api": ">=1.1,<2"}
SETTINGS = {
    "title": "Model routing",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "claude": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"work": {"type": "string", "enum": ["opus", "sonnet"]}},
            }
        },
    },
}


def load(**over):
    return load_manifest(json.dumps(manifest(**V3, **over)).encode())


def bad(**over):
    with pytest.raises(ManifestError):
        load(**over)


def test_core_declares_api_1_1_and_a_1_1_add_on_needs_it():
    assert ADDON_API == "1.1"
    assert incompatibility(load(), "2.0.0.dev0") is None
    m = load_manifest(json.dumps(manifest(schema="orch.addon/3", api=">=1.2,<2")).encode())
    assert incompatibility(m, "2.0.0.dev0") is not None


def test_the_two_new_capabilities_are_in_every_list_and_have_words():
    assert CAPABILITIES[-2:] == ("choose_model", "credential")
    assert set(CAPABILITIES) == set(schema.load("common")["$defs"]["capability"]["enum"])
    from importlib import resources

    doc = json.loads(resources.files("orch.addons").joinpath("schemas/market.v1.json").read_text())
    assert set(doc["$defs"]["entry"]["properties"]["capabilities"]["items"]["enum"]) == set(CAPABILITIES)
    assert doc["$defs"]["entry"]["properties"]["capabilities"]["maxItems"] == len(CAPABILITIES)
    assert set(signcontent._CAPABILITY_WORDS) == set(CAPABILITIES)  # noqa: SLF001
    for c in ("choose_model", "credential"):
        assert ";" not in signcontent._CAPABILITY_WORDS[c]  # noqa: SLF001  (the words are joined with "; ")


def test_credential_needs_network():
    assert load(capabilities=["credential", "network"]).capabilities == ["credential", "network"]
    bad(capabilities=["credential"])
    bad(capabilities=["credential", "pty"])
    assert load(capabilities=["choose_model"]).capabilities == ["choose_model"]


def test_python_placeholder_only_as_the_whole_of_cmd0():
    assert load(entry={"cmd": ["{python}", "{pkg}/main.py"]}).entry_cmd[0] == "{python}"
    for cmd in (
        ["{python}x", "a"],
        ["x{python}", "a"],
        ["{python} -c", "a"],
        ["python3", "{python}"],
        ["{pkg}/main.py", "{python}"],
        ["{pkg}/{python}"],
    ):
        bad(entry={"cmd": cmd})


def test_settings_key_loads_and_is_part_of_the_manifest():
    assert load(settings=SETTINGS).doc["settings"]["title"] == "Model routing"
    for extra in ({"x": 1}, {"title": ""}, {"title": "t" * 61}):
        bad(settings={**SETTINGS, **extra})
    bad(settings={"title": "x"})
    bad(settings={"schema": SETTINGS["schema"]})


def mutate(fn):
    s = copy.deepcopy(SETTINGS)
    fn(s["schema"])
    return s


@pytest.mark.parametrize(
    "fn",
    [
        lambda s: s.update(pattern="^a$"),
        lambda s: s.update({"$ref": "#/x"}),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(format="uri"),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(pattern="^a+$"),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(description="x"),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(default="opus"),
        lambda s: s.pop("additionalProperties"),
        lambda s: s.update(additionalProperties=True),
        lambda s: s.update(additionalProperties={"type": "string"}),
        lambda s: s.update(type="string"),
        lambda s: s.update(type=["object", "null"]),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(type="number"),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(enum=["a"] * 2),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(enum=[]),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(enum=["a" * 65]),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(enum=[1]),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(enum=["x\ny"]),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(enum=[str(i) for i in range(65)]),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(maxLength=201),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(maxLength=0),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(maxLength=True),
        lambda s: s["properties"]["claude"]["properties"]["work"].update(minimum=1),
        lambda s: s["properties"].update({"Bad Key": {"type": "boolean"}}),
        lambda s: s.update(required=["nope"]),
        lambda s: s.update(required=["claude", "claude"]),
        lambda s: s.update(maxProperties=65),
        lambda s: s.update(title="a\nb"),
        lambda s: s.update(title="t" * 61),
        lambda s: s["properties"].update(n={"type": "integer", "minimum": 1.5}),
        lambda s: s["properties"].update(n={"type": "integer", "minimum": 5, "maximum": 1}),
        lambda s: s["properties"].update(a={"type": "array", "items": {"type": "string"}}),  # no maxItems
        lambda s: s["properties"].update(a={"type": "array", "items": {"type": "string"}, "maxItems": 65}),
        lambda s: s["properties"].update(a={"type": "array", "maxItems": 3}),
        lambda s: s["properties"].update(
            a={"type": "array", "items": {"type": "string"}, "maxItems": 3, "uniqueItems": True}
        ),
    ],
)
def test_the_settings_subset_refuses_everything_else(fn):
    bad(settings=mutate(fn))


def test_the_settings_subset_accepts_the_named_keywords():
    s = mutate(
        lambda s: (
            s["properties"].update(
                n={"type": "integer", "minimum": 0, "maximum": 10, "title": "N"},
                f={"type": "boolean"},
                t={"type": "string", "maxLength": 200},
                a={"type": "array", "items": {"type": "string", "maxLength": 20}, "maxItems": 64},
            )
            or s.update(required=["f"], maxProperties=64, title="Top")
        )
    )
    assert "n" in load(settings=s).doc["settings"]["schema"]["properties"]


def test_depth_is_at_most_4():
    def nest(n):
        node = {"type": "boolean"}
        for _ in range(n):
            node = {"type": "object", "additionalProperties": False, "properties": {"a": node}}
        return node

    assert load(settings={"title": "T", "schema": nest(3)})  # root + 3 objects + a leaf = 4 levels
    bad(settings={"title": "T", "schema": nest(4)})


def test_the_official_ids_are_reserved_for_the_official_marketplace():
    assert constants.OFFICIAL_IDS == ("github", "landing", "model-routing")
    assert constants.MOVED_IDS == ()
    doc = lambda i: {  # noqa: E731
        "addons": [{"id": i, "version": "1.0.0", "source": {"git": "https://github.com/acme/market"}}]
    }
    ctx = mi.Ctx(
        url="https://github.com/acme/market", cls="community", keys=(), core_version="2.0.0.dev0",
        git_hosts=frozenset(constants.DEFAULT_GIT_HOSTS), installed={}, known_keys={}, seen={},
        builtin=frozenset(), official_ids=frozenset(constants.OFFICIAL_IDS),
    )  # fmt: skip
    for i in constants.OFFICIAL_IDS:
        with pytest.raises(MarketError) as e:
            mi.check_entry(ctx, doc(i), i)
        assert e.value.code == "market.reserved_id"
