"""static/tokens.json (DTCG 2025.10) -> static/tokens.css, stdlib only (design-system spec §2, decision D1).

    python -m orch.dashboard.design.build           # rewrite static/tokens.css
    python -m orch.dashboard.design.build --check   # exit 1 when tokens.css is not what tokens.json gives

Modes live in `$extensions["io.orch.ds"]`: `css` names the custom property; `dark`, `compact`, `coarse`, `phone`
and `reduced` hold the value for that mode. An alias to a token that has its own CSS name stays a `var()` reference
(`--control-border: var(--ctl)`), so it follows every theme; any other alias is resolved to its value. The output
holds custom properties only, so the phone app can copy it as is."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

NS = "io.orch.ds"
STATIC = Path(__file__).parents[1] / "static"
SOURCE = STATIC / "tokens.json"
OUTPUT = STATIC / "tokens.css"
_REF = re.compile(r"^\{([A-Za-z0-9.\-]+)\}$")


def load(path: Path = SOURCE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def flatten(doc: dict) -> dict[str, tuple[dict, str | None]]:
    """Every token by dotted path -> (its node, its $type, inherited from the nearest group that sets one)."""
    out: dict[str, tuple[dict, str | None]] = {}

    def walk(node, path, typ):
        if not isinstance(node, dict):
            return
        typ = node.get("$type", typ)
        if "$value" in node:
            out[".".join(path)] = (node, typ)
            return
        for key, child in node.items():
            if not key.startswith("$"):
                walk(child, path + [key], typ)

    walk(doc, [], None)
    return out


def _ext(node: dict) -> dict:
    return node.get("$extensions", {}).get(NS, {})


def resolve(value, tokens: dict):
    """Follow `{a.b.c}` aliases to a literal value (KeyError on a dangling alias)."""
    seen = set()
    while isinstance(value, str) and (m := _REF.match(value)):
        if m.group(1) in seen:
            raise ValueError(f"alias cycle at {m.group(1)}")
        seen.add(m.group(1))
        value = tokens[m.group(1)][0]["$value"]
    return value


def _css(value, typ, tokens: dict, *, refs: bool = True, weight=None) -> str:
    if refs and isinstance(value, str) and (m := _REF.match(value)):
        target = _ext(tokens[m.group(1)][0]).get("css")
        if target:
            return f"var({target})"
    value = resolve(value, tokens)
    if isinstance(value, dict) and "colorSpace" in value:
        if "alpha" in value:
            r, g, b = (round(c * 255) for c in value["components"])
            return f"rgba({r}, {g}, {b}, {value['alpha']})"
        return value["hex"]
    if isinstance(value, dict) and "unit" in value:
        return f"{value['value']}{value['unit']}"
    if isinstance(value, dict) and "offsetX" in value:  # shadow
        parts = " ".join(_css(value[k], None, tokens, refs=False) for k in ("offsetX", "offsetY", "blur", "spread"))
        return f"{parts} {_css(value['color'], None, tokens, refs=False)}"
    if isinstance(value, dict) and "fontFamily" in value:  # typography -> the `font` shorthand
        w = weight if weight is not None else _css(value["fontWeight"], None, tokens, refs=False)
        size = _css(value["fontSize"], None, tokens, refs=False)
        family = _css(value["fontFamily"], None, tokens, refs=False)
        return f"{w} {size}/{value['lineHeight']} {family}"
    if isinstance(value, list) and typ == "cubicBezier":
        return "cubic-bezier(" + ", ".join(map(str, value)) + ")"
    if isinstance(value, list):  # a font family stack
        return ", ".join(f'"{f}"' if " " in f or f[0].isupper() else f for f in value)
    return str(value)


def _modes(doc: dict) -> dict[str, list[str]]:
    tokens = flatten(doc)
    modes: dict[str, list[str]] = {k: [] for k in ("base", "light", "dark", "compact", "coarse", "phone", "reduced")}
    for node, typ in tokens.values():
        ext = _ext(node)
        name = ext.get("css")
        if not name:
            continue
        weight = ext.get("weight")
        modes["base"].append(f"{name}: {_css(node['$value'], typ, tokens, weight=weight)};")
        if "dark" in ext:  # a light wrapper switches back exactly what a dark one switched, nothing else
            modes["light"].append(f"{name}: {_css(node['$value'], typ, tokens, weight=weight)};")
        for mode in ("dark", "compact", "coarse", "reduced"):
            if mode in ext:
                modes[mode].append(f"{name}: {_css(ext[mode], typ, tokens, weight=weight)};")
        if "phone" in ext:  # a partial typography override on top of the base value
            merged = dict(resolve(node["$value"], tokens))
            merged.update(ext["phone"])
            modes["phone"].append(f"{name}: {_css(merged, typ, tokens, weight=weight)};")
    return modes


def breakpoints(doc: dict | None = None) -> dict[str, int]:
    """The viewport and container thresholds as numbers: CSS cannot read custom properties inside @media or
    @container, so app.css repeats them literally and tests compare against these."""
    tokens = flatten(doc or load())
    v = lambda path: int(resolve(tokens[path][0]["$value"], tokens)["value"])  # noqa: E731
    return {"shell": v("semantic.layout.viewport.shell"), "split": v("semantic.layout.viewport.split"),
            "wide": v("semantic.layout.viewport.wide"), "narrow": v("semantic.layout.container.narrow"),
            "medium": v("semantic.layout.container.medium"), "wide_container": v("semantic.layout.container.wide")}


def render(doc: dict) -> str:
    m = _modes(doc)
    block = lambda lines, pad: "\n".join(pad + line for line in lines)  # noqa: E731
    version = doc["$extensions"][NS]["version"]
    shell = breakpoints(doc)["shell"]
    return f"""/* GENERATED from static/tokens.json (DTCG 2025.10) v{version} by `python -m orch.dashboard.design.build`.
   Do not edit: change tokens.json and run the build. Other apps (the phone companion) copy it as is, so it
   holds custom properties only. Theme: data-theme=light|dark|system on any element (system follows the OS); a wrapper that
   sets data-theme also paints background: var(--bg) and color: var(--text). Only themed tokens repeat under
   [data-theme], so a nested theme wrapper never resets density, pointer, phone or motion values. */
:root {{
  color-scheme: light;
{block(m["base"], "  ")}
}}
[data-theme=light] {{
  color-scheme: light;
{block(m["light"], "  ")}
}}
[data-theme=dark] {{
  color-scheme: dark;
{block(m["dark"], "  ")}
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme=light]), [data-theme=system] {{
    color-scheme: dark;
{block(m["dark"], "    ")}
  }}
}}
[data-density=compact] {{
{block(m["compact"], "  ")}
}}
@media (pointer: coarse) {{
  :root {{
{block(m["coarse"], "    ")}
  }}
}}
@media (max-width: {shell - 1}px) {{
  :root {{
{block(m["phone"], "    ")}
  }}
}}
@media (prefers-reduced-motion: reduce) {{
  :root {{
{block(m["reduced"], "    ")}
  }}
}}
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out = OUTPUT
    if "--out" in args:
        out = Path(args[args.index("--out") + 1])
    text = render(load())
    if "--check" in args:
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        if current != text:
            print(f"{out} is out of date: run python -m orch.dashboard.design.build", file=sys.stderr)
            return 1
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
