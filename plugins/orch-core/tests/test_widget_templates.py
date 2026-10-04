"""Built-in widget templates, the vendored libraries and orch-kit (docs/widgets.md)."""

import hashlib
import json
import re

import jsonschema
import pytest

from orch.widgets.assemble import BUILTIN, FRAME_CSP, STATIC, VENDOR, assemble, read_libs, read_manifest

CONTRACT_BUILTINS = {"before-after", "option-prototype", "agent-waterfall", "decision-matrix", "eval-grid",
                     "ci-timeline", "mermaid", "bundle-treemap"}
MOMENTS = {"understand", "decide", "plan", "verify", "review", "debug", "report"}
NAMES = sorted(p.name for p in BUILTIN.iterdir() if (p / "widget.json").is_file())


def _load(name):
    folder = BUILTIN / name
    return (json.loads((folder / "widget.json").read_text()), json.loads((folder / "example.json").read_text()), folder)


def _objects(schema):
    """Every subschema that describes an object."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for v in schema.values():
            yield from _objects(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _objects(v)


def test_every_contract_builtin_is_shipped():
    assert set(NAMES) == CONTRACT_BUILTINS


@pytest.mark.parametrize("name", NAMES)
def test_template_is_valid_and_its_example_validates(name):
    meta, example, folder = _load(name)
    assert meta["name"] == name
    assert meta["moment"] in MOMENTS
    assert isinstance(meta["min_height"], int) and meta["min_height"] > 0
    assert set(meta["versions"]) == set(example), "one example per version"
    manifest = read_manifest()["libs"]
    for lib in meta["libs"]:
        assert lib in manifest, f"{name} lists {lib}, which is not vendored"
    for version, spec in meta["versions"].items():
        schema = spec["schema"]
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        jsonschema.Draft202012Validator.check_schema(schema)
        for obj in _objects(schema):
            if "properties" in obj:
                assert obj.get("additionalProperties") is False, f"{name} v{version}: an object allows unknown keys"
        jsonschema.Draft202012Validator(schema).validate(example[version])
        body = (folder / f"v{version}.html").read_text()
        assert "orch.text(" in body and "orch.ready(" in body
        assert not re.search(r"\b(fetch|XMLHttpRequest|WebSocket|localStorage|sessionStorage|indexedDB)\b", body)


def test_manifest_digests_and_sizes_match_the_files():
    libs = read_manifest()["libs"]
    assert {"uplot", "mermaid", "vega", "plot", "diff2html", "leaflet"} <= set(libs)
    for lib in libs.values():
        assert set(lib["inline_order"]) == set(lib["files"])
        assert lib["packages"] and all(p["version"] and p["licence"] for p in lib["packages"])
        for p in lib["packages"]:
            assert (VENDOR / p["licence_file"]).is_file()
        for path, f in lib["files"].items():
            raw = (VENDOR / path).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == f["sha256"], path
            assert len(raw) == f["bytes"], path


def test_orch_kit_has_no_network_or_storage():
    for name in ("orch-kit.js", "orch-frames.js"):
        text = (STATIC / "widgets" / name).read_text()
        assert not re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|localStorage|sessionStorage|indexedDB)\b", text), name


def test_assemble_builds_the_frame_document_in_contract_order():
    doc = assemble("<p>hi</p>", {"x": "</script><b>"}, nonce="abcdefgh12", kit_js="/*kit*/",
                   tokens_css=":root{--bg:#fff}", libs=[("js", "var a='</script>';"), ("css", ".c{}")])
    assert FRAME_CSP in doc and '<meta name="orch-frame" content="abcdefgh12">' in doc
    assert "</script><b>" not in doc and "<\\/script>" in doc
    order = [doc.index(s) for s in (":root{--bg:#fff}", ".c{}", "/*kit*/", "var a=", 'id="orch-data"', "<p>hi</p>")]
    assert order == sorted(order)
    with pytest.raises(ValueError):
        assemble("", {}, nonce="bad nonce", kit_js="", tokens_css="")


def test_read_libs_returns_each_library_in_inline_order():
    chunks = read_libs(["uplot"])
    assert [k for k, _ in chunks] == ["css", "js"]
    with pytest.raises(KeyError):
        read_libs(["nope"])


LIB_FIXTURES = BUILTIN.parents[3] / "tests" / "fixtures" / "widget_libs"
GLOBALS = {"uplot": "uPlot", "vega": "vegaEmbed", "plot": "Plot", "diff2html": "Diff2Html", "leaflet": "L.map"}


@pytest.mark.parametrize("lib", sorted(GLOBALS))
def test_each_vendored_library_has_a_frame_fixture(lib):
    """One page per library (mermaid: the built-in template) that the browser check loads under the frame CSP: it
    draws with the library, posts its text and calls ready, and never asks for eval or the network."""
    body = (LIB_FIXTURES / f"{lib}.html").read_text()
    data = json.loads((LIB_FIXTURES / "data.json").read_text())[lib]
    assert GLOBALS[lib] in body and "orch.text(" in body and "orch.ready(" in body and "orch.error(" in body
    assert not re.search(r"\b(fetch|XMLHttpRequest|eval|new Function)\b", body)
    if lib == "vega":
        assert "expr: vega.expressionInterpreter" in body and "ast: true" in body
    doc = assemble(body, data, nonce="abcdefgh12", kit_js="/*kit*/", tokens_css="", libs=read_libs([lib]))
    assert FRAME_CSP in doc and "unsafe-eval" not in doc
    assert set(GLOBALS) | {"mermaid"} == set(read_manifest()["libs"])
