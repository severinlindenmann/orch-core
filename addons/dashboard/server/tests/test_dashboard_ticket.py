import hashlib

import pytest
from conftest import ORIGIN, git
from golden import check


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def url(stub, tail):
    return f"/api/w/{stub.ws}/tickets/{tail}"


@pytest.mark.parametrize("tail", ["DEMO-0001", "DEMO-0001/events", "DEMO-0001/changes", "DEMO-0001/artifacts/out.log"])
def test_ticket_routes_need_authentication(client, stub, tail):
    assert client.get(url(stub, tail)).status_code == 401


@pytest.mark.parametrize(
    "tail",
    [
        "demo-1/x",
        "DEMO-0001/events?since=-1",
        "DEMO-0001/events?limit=501",
        "DEMO-0001/events?file=x",
        "DEMO-0001/changes?repo=../../x",
        "DEMO-0001/changes?context=11",
        "DEMO-0001/changes?ref=other",
        "DEMO-0001/artifacts/..%2Fconfig.json",
        "DEMO-0001/artifacts/out.log?offset=0",
        "DEMO-1x",
    ],
)
def test_typed_inputs_are_refused(client, token, stub, rich, tail):
    res = client.get(url(stub, tail), headers=headers(token))
    assert res.status_code in (400, 404), res.text
    assert "config.json" not in res.text and "../../x" not in res.text


def test_document_is_adapted_to_the_ui_type(client, token, stub, rich):
    res = client.get(url(stub, "DEMO-0001"), headers=headers(token))
    assert res.status_code == 200, res.text
    doc = res.json()
    assert doc["key"] == "DEMO-0001" and doc["status"] == "open" and doc["priority"] == "high"
    assert [a["id"] for a in doc["acceptance_state"]] == ["AC1"] and doc["acceptance_state"][0]["state"] == "unproven"
    assert [t["state"] for t in doc["tasks_state"]] == ["todo", "todo"]
    assert {q["id"]: q["state"] for q in doc["questions_state"]} == {"Q1": "answered", "Q2": "open"}
    answered = doc["questions_state"][0]["answer"]
    assert answered["by"] == stub.person and answered["at"].startswith("2026-")
    assert doc["gates"]["requirements"]["state"] == "pending" and doc["gates"]["code"]["required"] is False
    assert doc["gates"]["requirements"]["hash"] and doc["body"]["requirements"] == "req"
    assert (
        doc["links"]["branches"] == {"code": "feat/one"}
        and doc["branch"]["head"] == ""
        and len(doc["sources"][0]["sha"]) == 40
        and doc["branch"]["name"] == "feat/one"
    )
    assert doc["restricted"] is False and doc["verdict"] is None
    assert doc["verdict"] is None and "branch commits" in doc["unavailable"]  # named, not invented
    assert doc["turn"]["who"] == stub.person
    assert doc["head"]["seq"] > 0 and doc["created_at"] and doc["updated_at"]
    check("ticket", doc)


def test_events_are_objects_not_strings(client, token, stub, rich):
    events = client.get(url(stub, "DEMO-0001/events"), headers=headers(token)).json()
    assert events[0]["type"] == "ticket.created" and events[0]["seq"] == 1
    assert all(set(e) >= {"v", "id", "seq", "at", "type", "actor", "summary"} for e in events)
    assert any("AC1 added" in e["summary"] for e in events)
    tail = client.get(url(stub, f"DEMO-0001/events?since={events[2]['seq']}&limit=2"), headers=headers(token)).json()
    assert [e["seq"] for e in tail] == [events[3]["seq"], events[4]["seq"]]
    check("events", events)


def test_evidence_per_criterion_and_content(client, token, stub, rich):
    env, ids = rich
    data = b"first line\nsecond line\n"
    env.store.append(
        {
            "type": "artifact.added",
            "actor": env.agent,
            "name": "out.log",
            "kind": "log",
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "ac": "AC1",
        },
        log=ids["First"],
        artifacts={"out.log": data},
    )
    doc = client.get(url(stub, "DEMO-0001"), headers=headers(token)).json()
    assert doc["acceptance_state"][0]["state"] == "proven"
    assert doc["artifacts"][0]["name"] == "out.log" and doc["artifacts"][0]["sha256"].endswith(
        hashlib.sha256(data).hexdigest()
    )
    assert doc["artifacts"][0]["ac"] == "AC1"
    content = client.get(url(stub, "DEMO-0001/artifacts/out.log"), headers=headers(token)).json()
    assert content["text"] == data.decode() and content["verified"] is True and content["truncated"] is False
    assert content["image"] is None  # a log is not a picture
    assert client.get(url(stub, "DEMO-0001/artifacts/missing.log"), headers=headers(token)).status_code == 404
    # A file that changed on disk after its digest was recorded is refused, never shown as evidence.
    (env.store_dir / "tickets" / ids["First"] / "artifacts" / "out.log").write_bytes(b"tampered")
    bad = client.get(url(stub, "DEMO-0001/artifacts/out.log"), headers=headers(token))
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "artifact.mismatch"


def _png(*extra):
    import struct
    import zlib

    def chunk(kind, body=b""):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    return (
        b"\x89PNG\r\n\x1a\n"
        + ihdr
        + b"".join(extra)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND")
    )


def test_a_screenshot_is_offered_as_bytes_only_when_core_accepts_it(client, token, stub, rich):
    """R3 part B: a PNG with metadata, or a JPEG, is not offered as bytes (the page hashes what it gets)."""
    import base64

    env, ids = rich
    good = _png()
    pictures = {
        "paste-20261019-101500-aaaaaaaa.png": good,
        "paste-20261019-101502-cccccccc.png": _png(b"\x00\x00\x00\x04tEXtabcd\x00\x00\x00\x00"),
        "exif.jpg": b"\xff\xd8\xff\xe1" + b"Exif\x00\x00GPS",
    }
    for name, data in pictures.items():
        env.tick()
        env.store.append(
            env.person_event(
                env.owner,
                ids["First"],
                "artifact.added",
                name=name,
                kind="screenshot",
                sha256="sha256:" + hashlib.sha256(data).hexdigest(),
                bytes=len(data),
            ),
            log=ids["First"],
            artifacts={name: data},
        )
    got = client.get(url(stub, "DEMO-0001/artifacts/paste-20261019-101500-aaaaaaaa.png"), headers=headers(token)).json()
    digest = "sha256:" + hashlib.sha256(good).hexdigest()
    assert got["image"] == {"type": "png", "b64": base64.b64encode(good).decode(), "digest": digest}
    # an agent's image (same bytes, not a person's paste) is never offered for inline drawing
    agent_data = good
    env.store.append(
        {
            "type": "artifact.added",
            "actor": env.agent,
            "name": "paste-20261019-101501-bbbbbbbb.png",
            "kind": "screenshot",
            "sha256": "sha256:" + hashlib.sha256(agent_data).hexdigest(),
            "bytes": len(agent_data),
        },
        log=ids["First"],
        artifacts={"paste-20261019-101501-bbbbbbbb.png": agent_data},
    )
    agent_got = client.get(url(stub, "DEMO-0001/artifacts/paste-20261019-101501-bbbbbbbb.png"), headers=headers(token))
    assert agent_got.json()["image"] is None
    for name in ("paste-20261019-101502-cccccccc.png", "exif.jpg"):
        assert client.get(url(stub, f"DEMO-0001/artifacts/{name}"), headers=headers(token)).json()["image"] is None


def _gif(sw, sh, frames=((0, 0, 1, 1),), trailer=True, cut=0):
    """A GIF with the given logical screen and frames (left, top, width, height); the pixel data is one empty block."""
    out = b"GIF89a" + sw.to_bytes(2, "little") + sh.to_bytes(2, "little") + b"\x00\x00\x00"
    for left, top, w, h in frames:
        out += b"\x2c" + b"".join(v.to_bytes(2, "little") for v in (left, top, w, h)) + b"\x00"
        out += b"\x02" + b"\x01\x00" + b"\x00"  # LZW minimum code size, one sub-block of 1 byte, block terminator
    out += b"\x3b" if trailer else b""
    return out[: len(out) - cut] if cut else out


def _jpeg(w, h):
    sof = b"\xff\xc0\x00\x0b\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x01\x01\x11\x00"
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + b"\xff\xd9"


def test_evidence_pictures_are_offered_as_media_only_when_complete_and_verified(client, token, stub, rich):
    """#1069: an agent's PNG/JPEG/GIF/SVG is offered as `media` (the page shows it as an <img> after re-hashing);
    text and binary files are not; a file that no longer matches its digest is refused outright."""
    import base64

    env, ids = rich
    files = {
        "a.png": _png(),
        "b.jpg": _jpeg(8, 8),
        "c.gif": _gif(8, 8, ((0, 0, 8, 8),)),
        "d.svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><rect width="1" height="1"/></svg>',
        "e.txt": b"just text",
        "f.bin": b"\x00\x01\x02\x03",
    }
    for name, data in files.items():
        env.tick()
        env.store.append(
            {
                "type": "artifact.added",
                "actor": env.agent,
                "name": name,
                "kind": "other",
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            },
            log=ids["First"],
            artifacts={name: data},
        )
    kinds = {}
    for name, data in files.items():
        got = client.get(url(stub, f"DEMO-0001/artifacts/{name}"), headers=headers(token)).json()
        kinds[name] = got["media"]["type"] if got["media"] else None
        if got["media"]:
            assert got["media"]["b64"] == base64.b64encode(data).decode()
            assert got["media"]["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
        assert got["image"] is None  # the pasted-screenshot path is unchanged: an agent's picture is not `image`
        if got["media"]:
            assert got["text"] is None  # the bytes travel once
    assert kinds == {"a.png": "png", "b.jpg": "jpeg", "c.gif": "gif", "d.svg": "svg", "e.txt": None, "f.bin": None}
    (env.store_dir / "tickets" / ids["First"] / "artifacts" / "a.png").write_bytes(b"tampered")
    assert client.get(url(stub, "DEMO-0001/artifacts/a.png"), headers=headers(token)).status_code == 409


def _bomb_png(w, h):
    import struct
    import zlib

    def chunk(kind, body=b""):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND")
    )


def _offer(env, ids, files):
    for name, data in files.items():
        env.tick()
        env.store.append(
            {
                "type": "artifact.added",
                "actor": env.agent,
                "name": name,
                "kind": "other",
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            },
            log=ids["First"],
            artifacts={name: data},
        )


SVG_NS = b'xmlns="http://www.w3.org/2000/svg"'


def test_evidence_pictures_with_bomb_headers_are_not_offered(client, token, stub, rich):
    """Opus M1 and delta MED-1: a decompression-bomb header (PNG, JPEG, GIF), a GIF whose frame sticks out of the screen
    (the browser enlarges the canvas), a truncated GIF, a GIF without trailer or with too many frames: no `media`."""
    env, ids = rich
    files = {
        "ok.gif": _gif(4, 4, ((0, 0, 4, 4), (1, 1, 2, 2))),
        "bomb.png": _bomb_png(60000, 60000),
        "bomb.jpg": _jpeg(60000, 60000),
        "bomb.gif": _gif(60000, 60000),
        "lying.gif": _gif(1, 1, ((0, 0, 65535, 65535),)),
        "offset.gif": _gif(10, 10, ((8, 8, 4, 4),)),
        "truncated.gif": _gif(4, 4, cut=6),
        "notrailer.gif": _gif(4, 4, trailer=False),
        "many.gif": _gif(2, 2, ((0, 0, 1, 1),) * 501),
        "noframe.gif": _gif(2, 2, ()),
        "tiny_frames_huge_canvas.gif": _gif(6000, 6000, ((0, 0, 1, 1),) * 7),
    }
    _offer(env, ids, files)
    got = {n: client.get(url(stub, f"DEMO-0001/artifacts/{n}"), headers=headers(token)).json()["media"] for n in files}
    assert got["ok.gif"] is not None and got["ok.gif"]["type"] == "gif"
    assert {n: m for n, m in got.items() if n != "ok.gif"} == dict.fromkeys(set(files) - {"ok.gif"})


def test_evidence_svg_is_an_allowlist_not_a_scan(client, token, stub, rich):
    """Opus delta MED-2: only a strict, plain SVG is offered. Nested raster images, character references, a prefixed
    <svg:image>, style url(data:), <use>, scripts, event handlers, any DTD or entity, base64-nested SVG, an element or
    depth bomb and an oversize file are refused (the page shows a card)."""
    env, ids = rich
    import base64

    nested = base64.b64encode(b'<!DOCTYPE svg [<!ENTITY a "x">]><svg ' + SVG_NS + b">&a;</svg>")
    files = {
        "ok.svg": b"<svg "
        + SVG_NS
        + b' width="9" height="9" viewBox="0 0 9 9"><rect x="0" y="0" width="5" height="5" fill="#0a7"/>'
        b'<circle cx="3" cy="3" r="2" fill="url(#g)"/><path d="M0 0L5 5"/><text x="1" y="9">hi</text></svg>',
        "bom.svg": b"\xef\xbb\xbf<svg " + SVG_NS + b"><rect/></svg>",
        "data.svg": b"<svg " + SVG_NS + b'><image href="data:image/png;base64,AAAA"/></svg>',
        "charref.svg": b"<svg " + SVG_NS + b'><rect fill="&#100;ata:x"/></svg>',
        "prefixed.svg": b'<svg:svg xmlns:svg="http://www.w3.org/2000/svg"><svg:image/></svg:svg>',
        "style.svg": b"<svg " + SVG_NS + b'><rect style="fill:url(data:image/png;base64,AA)"/></svg>',
        "use.svg": b"<svg " + SVG_NS + b'><use href="#a"/></svg>',
        "script.svg": b"<svg " + SVG_NS + b"><script>alert(1)</script></svg>",
        "onload.svg": b"<svg " + SVG_NS + b' onload="x()"/>',
        "doctype.svg": b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg ' + SVG_NS + b">&a;</svg>",
        "base64.svg": b"<svg " + SVG_NS + b'><image href="data:image/svg+xml;base64,' + nested + b'"/></svg>',
        "depth.svg": b"<svg " + SVG_NS + b">" + b"<g>" * 40 + b"</g>" * 40 + b"</svg>",
        "count.svg": b"<svg " + SVG_NS + b">" + b"<g/>" * 6000 + b"</svg>",
        "huge.svg": b"<svg " + SVG_NS + b"><!-- " + b"x" * (600 * 1024) + b" --></svg>",
        "fe.svg": b"<svg " + SVG_NS + b'><filter><feImage href="#a"/></filter></svg>',
        "href.svg": b"<svg "
        + SVG_NS
        + b'><linearGradient xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="http://x/y"/></svg>',
        "ns.svg": b'<svg xmlns="http://example.com/other"><rect/></svg>',
        # Opus third delta: CSS escapes defeat any value check, so attributes are an allowlist (no style at all)
        "esc_style.svg": b"<svg " + SVG_NS + rb' ><rect style="background:u\72l(d\61ta:image/png;base64,AAAA)"/></svg>',
        "esc_mask.svg": b"<svg " + SVG_NS + rb' ><rect style="mask-image:u\72l(d\61ta:image/png;base64,AAAA)"/></svg>',
        "esc_imageset.svg": b"<svg "
        + SVG_NS
        + rb""" ><rect style="background:image-set('d\61ta:image/png;base64,AAAA' 1x)"/></svg>""",
        "esc_fill.svg": b"<svg " + SVG_NS + rb' ><rect fill="u\72l(d\61ta:image/png;base64,AAAA)"/></svg>',
        "plain_style.svg": b"<svg " + SVG_NS + b'><rect style="fill:red"/></svg>',
        "backslash.svg": b"<svg " + SVG_NS + rb' ><rect fill="re\64"/></svg>',
        "class.svg": b"<svg " + SVG_NS + b'><rect class="a"/></svg>',
        "latin1.svg": b'<?xml version="1.0" encoding="ISO-8859-1"?><svg ' + SVG_NS + b"><rect/></svg>",
    }
    _offer(env, ids, files)
    got = {n: client.get(url(stub, f"DEMO-0001/artifacts/{n}"), headers=headers(token)).json()["media"] for n in files}
    assert got["ok.svg"] is not None and got["ok.svg"]["type"] == "svg"
    assert got["bom.svg"] is not None  # a BOM is stripped by the one rule on both sides
    assert {n: m for n, m in got.items() if n not in ("ok.svg", "bom.svg")} == dict.fromkeys(
        set(files) - {"ok.svg", "bom.svg"}
    )


def test_changes_are_bound_to_the_observed_source(client, token, stub, rich):
    env, ids = rich
    res = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert res.status_code == 200, res.text
    changes = res.json()
    assert changes["branch"] == "feat/one" and len(changes["head"]) == 40 and changes["base"] != changes["head"]
    assert {f["path"]: f["status"] for f in changes["files"]} == {"a.txt": "M", "b.txt": "A"}
    assert "+two" in next(f for f in changes["files"] if f["path"] == "a.txt")["lines"]
    assert changes["additions"] == 2 and changes["truncated"] is False
    check("changes", changes)
    repo = env.tmp / "code"
    (repo / "c.txt").write_text("late\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "late")
    stale = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "observe.unavailable"
    assert "late" not in stale.text


def test_unknown_and_closed_tickets(client, token, stub, rich):
    env, ids = rich
    assert client.get(url(stub, "DEMO-0099"), headers=headers(token)).status_code == 404
    env.tick()
    env.close(ids["Third"])
    res = client.get(url(stub, "DEMO-0004"), headers=headers(token))
    assert res.status_code == 200  # a plain read of a closed ticket never errors (v1.4)
    doc = res.json()
    assert doc["status"] == "closed" and doc["settled"] is True and doc["closed"]["resolution"] == "other"
    assert doc["closed"]["by"] and doc["closed"]["duplicate_of"] is None
    closed = client.get(f"/api/w/{stub.ws}/tickets?status=closed", headers=headers(token)).json()
    assert [r["key"] for r in closed["rows"]] == ["DEMO-0004"] and closed["rows"][0]["status"] == "closed"
    listed = client.get(f"/api/w/{stub.ws}/board", headers=headers(token)).json()
    assert "DEMO-0004" not in [r["key"] for r in listed["rows"]]


def test_ticket_routes_have_no_write_method(client, token, stub):
    for tail in ("DEMO-0001", "DEMO-0001/events", "DEMO-0001/changes"):
        res = client.post(url(stub, tail), json={}, headers={**headers(token), "Origin": ORIGIN})
        assert res.status_code == 405


def test_without_read_ext_the_page_says_unknown(client, token, stub, rich):
    stub.ext = False  # a host without read_ext/1: absent fields mean unknown, never zero
    doc = client.get(url(stub, "DEMO-0001"), headers=headers(token)).json()
    assert doc["read_ext"] is False and doc["verdict"] is None and "verdict" in doc["unavailable"]
    assert doc["claim"] is None and "claim details" in doc["unavailable"]
    board = client.get(f"/api/w/{stub.ws}/board", headers=headers(token)).json()
    unknown = ("progress", "restricted", "blocking_questions")
    assert all(r[k] is None for r in board["rows"] for k in unknown)
    check("ticket-v13", doc)
    check("board-page-v13", board)


def test_artifact_empty_chunk_without_last_is_an_error(client, token, stub, gateway, monkeypatch):
    calls = []

    def call(ws, op, args=None):
        calls.append(op)
        return {"b64": "", "last": False, "size": 10, "digest": "sha256:" + "0" * 64}

    monkeypatch.setattr(gateway, "call", call)
    res = client.get(url(stub, "DEMO-0001/artifacts/out.log"), headers=headers(token))
    assert res.status_code >= 500 and res.json()["error"]["code"] == "internal"
    assert len(calls) == 1  # no second read of a host that makes no progress


def test_null_priority_stays_null(client, token, stub, gateway, rich, monkeypatch):
    real = gateway.call

    def call(ws, op, args=None):
        data = real(ws, op, args)
        for row in data.get("rows", []):
            row["priority"] = None
        if op == "ticket.get":
            data["doc"]["priority"] = None
        return data

    monkeypatch.setattr(gateway, "call", call)
    doc = client.get(url(stub, "DEMO-0001"), headers=headers(token)).json()
    assert doc["priority"] is None
    rows = client.get(f"/api/w/{stub.ws}/tickets", headers=headers(token)).json()["rows"]
    assert rows and all(r["priority"] is None for r in rows)


def test_a_diff_git_cannot_produce_reaches_the_page_as_diff_unavailable(client, token, stub, rich, monkeypatch):
    """#561 R7-M1, the server route: an error the Changes tab shows as "Diff unavailable", not an empty diff. Since
    #565 the diff is orch's reader in-process: a refusal of it (here: a blob it cannot read) is that error."""
    from orch.gitread.errors import Unavailable
    from orch.gitread.repo import Repo

    def blob(self, oid):
        raise Unavailable("an object is missing")

    monkeypatch.setattr(Repo, "blob", blob)
    res = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert res.status_code == 409 and res.json()["error"]["code"] == "observe.unavailable"
    assert "diff unavailable" in res.json()["error"]["message"]


def test_the_person_check_is_bound_to_the_digest_the_ticket_names(client, token, stub, rich, monkeypatch):
    """R3 part B: a verified file whose digest is not the one the signed artifact names is never offered as an image."""
    from orch_dashboard import reads

    env, ids = rich
    data = _png()
    name = "paste-20261019-101503-dddddddd.png"
    env.tick()
    env.store.append(
        env.person_event(
            env.owner,
            ids["First"],
            "artifact.added",
            name=name,
            kind="screenshot",
            sha256="sha256:" + hashlib.sha256(data).hexdigest(),
            bytes=len(data),
        ),
        log=ids["First"],
        artifacts={name: data},
    )
    assert client.get(url(stub, f"DEMO-0001/artifacts/{name}"), headers=headers(token)).json()["image"] is not None
    real = reads.pasted_by_person
    monkeypatch.setattr(reads, "pasted_by_person", lambda g, w, k, n, d: real(g, w, k, n, "sha256:" + "0" * 64))
    assert client.get(url(stub, f"DEMO-0001/artifacts/{name}"), headers=headers(token)).json()["image"] is None
