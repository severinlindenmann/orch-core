"""Wiki page parsing (spec A1 §7.4): title (front matter, first heading or file name), ticket keys, `documents:`
globs, GitHub file links and a short excerpt. Pure functions."""
from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from .globs import normalize

_FM = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
_H1 = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_KEY = re.compile(r"\b([A-Z][A-Z0-9]*)-(\d+)\b")
_GITHUB_FILE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:blob|tree)/[^/\s)]+/([^\s)#?\"'>]+)")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKS = re.compile(r"[*_`>#~]")


@dataclass(frozen=True)
class ParsedPage:
    title: str
    text: str
    excerpt: str
    keys: tuple[str, ...]
    documents: tuple[str, ...]
    file_links: tuple[str, ...]
    problems: tuple[str, ...] = ()


def front_matter(text: str) -> tuple[dict, str, list[str]]:
    m = _FM.match(text)
    if not m:
        return {}, text, []
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, text[m.end():], ["front matter is not valid YAML"]
    return (data if isinstance(data, dict) else {}), text[m.end():], []


def _documents(value) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else (value if isinstance(value, list) else [])
    out = []
    for v in values:
        if isinstance(v, str) and v.strip() and len(v) <= 200:
            out.append(normalize(v))
    return tuple(dict.fromkeys(out))[:50]


def _title(stem: str, fm: dict, body: str) -> str:
    if isinstance(fm.get("title"), str) and fm["title"].strip():
        return " ".join(fm["title"].split())
    m = _H1.search(body)
    if m:
        return m.group(1).strip()
    return stem.replace("-", " ").strip() or stem


def _keys(text: str, local_prefix: str, pad: int, trackers) -> tuple[str, ...]:
    out = []
    for prefix, number in _KEY.findall(text):
        if local_prefix and prefix == local_prefix:
            out.append(f"{prefix}-{int(number):0{pad}d}")
        elif prefix in trackers:
            out.append(f"{prefix}-{int(number)}")
    return tuple(sorted(set(out)))


def _excerpt(body: str) -> str:
    lines, started = [], False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("```"):
            if started:
                break
            continue
        if not stripped:
            if started:
                break
            continue
        started = True
        lines.append(stripped)
    text = _MARKS.sub("", _MD_LINK.sub(r"\1", " ".join(lines)))
    return " ".join(text.split())[:200]


def parse_page(stem: str, raw: str, *, local_prefix: str, pad: int, tracker_prefixes=()) -> ParsedPage:
    raw = raw.replace("\r\n", "\n").lstrip("﻿")
    fm, body, problems = front_matter(raw)
    title = _title(stem, fm, body)
    links = tuple(dict.fromkeys(normalize(m) for m in _GITHUB_FILE.findall(raw)))[:200]
    return ParsedPage(title=title, text=f"{title}\n{body}", excerpt=_excerpt(body),
                      keys=_keys(raw, local_prefix, int(pad or 4), tuple(tracker_prefixes)),
                      documents=_documents(fm.get("documents")), file_links=links, problems=tuple(problems))
