"""``orch.addons.lockfile`` (V3 G4, design-g4 C2): the one lockfile parser gives the verdict of the reference checker on
every ``lock`` vector, and its ``plan`` decides what this computer installs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.addons import lockfile
from orch.market import MarketError

CASES = [
    c for c in json.loads(Path(__file__).with_name("vectors_market.json").read_text())["cases"] if c["check"] == "lock"
]
H = "a" * 64
S512 = "sha512-" + "A" * 86 + "=="
HOST = lockfile.Host(
    {"python_version": "3.14", "python_full_version": "3.14.8", "sys_platform": "darwin", "platform_system": "Darwin",
     "os_name": "posix", "implementation_name": "cpython", "platform_machine": "arm64",
     "platform_python_implementation": "CPython", "platform_release": "", "platform_version": "",
     "implementation_version": "3.14.8"},
    "darwin", "arm64",
)  # fmt: skip


def verdict(kind: str, text: str | bytes) -> dict:
    raw = text if isinstance(text, bytes) else text.encode()
    try:
        lockfile.check(kind, raw)
    except MarketError as e:
        return {"refuse": e.code}
    return {"ok": {}}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_lock_vector(case):
    """C2: the product parser and the reference checker agree on every vector."""
    assert verdict(case["args"]["kind"], case["args"]["text"]) == case["expect"]


def py(*lines: str) -> str:
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "text",
    [
        py(f"x===1.0 --hash=sha256:{H}"),  # arbitrary equality
        py(f"x>=1.0 --hash=sha256:{H}"),
        py(f"x==1.0 --hash=sha512:{H}"),
        py("x==1.0"),  # no hash
        py(f"x @ https://evil.example/x.whl --hash=sha256:{H}"),
        py(f"-e . --hash=sha256:{H}"),
        py(f"--index-url https://evil.example --hash=sha256:{H}"),
        py(f"x==1.0 --hash=sha256:{H} --index-url https://evil.example"),
        py(f"x==1.0 --hash=sha256:{H[:-1]}"),
        py(f"x==1.0 --hash=sha256:{H.upper()}"),
        "\ufeff" + py(f"x==1.0 --hash=sha256:{H}"),
        py(f"x==1.0 --hash=sha256:{H}\x0b y==1.0 --hash=sha256:{H}"),
        py("# only a comment"),
        "",
    ],
)
def test_python_refusals(text):
    assert verdict("python-requirements", text) == {"refuse": "market.lock_invalid"}


def test_python_rows_are_the_only_source():
    lock = lockfile.check(
        "python-requirements",
        py(
            f"Foo_Bar==1.0.0 ; sys_platform == 'win32' --hash=sha256:{H} --hash=sha256:{'b' * 64}",
            f"idna[x]==3.10 --hash=sha256:{H}",
        ).encode(),
    )
    assert [(r.name, r.version, r.marker) for r in lock.rows] == [
        ("foo-bar", "1.0.0", 'sys_platform == "win32"'),
        ("idna", "3.10", None),
    ]
    assert lock.rows[0].hashes == tuple(sorted({H, "b" * 64}))


def test_size_cap_and_bad_utf8():
    big = (py(f"x==1.0 --hash=sha256:{H}") * 40000).encode()
    assert len(big) > lockfile.MAX_LOCK_BYTES
    assert verdict("python-requirements", big) == {"refuse": "market.lock_invalid"}
    assert verdict("python-requirements", b"\xff\xfe") == {"refuse": "market.lock_invalid"}
    with pytest.raises(MarketError) as e:
        lockfile.check("go-mod", b"x")
    assert e.value.code == "market.malformed"


def node(pkgs: dict, **top) -> str:
    return json.dumps({"lockfileVersion": 3, "packages": {"": {}, **pkgs}, **top})


def npm(name: str, version: str = "1.0.0", **extra) -> dict:
    base = name.rsplit("/", 1)[-1]
    return {
        "version": version,
        "resolved": f"https://registry.npmjs.org/{name}/-/{base}-{version}.tgz",
        "integrity": S512,
        **extra,
    }


@pytest.mark.parametrize(
    "text",
    [
        node({"node_modules/x": npm("x", link=True)}),
        node({"node_modules/x": {**npm("x"), "resolved": "http://registry.npmjs.org/x/-/x-1.0.0.tgz"}}),
        node({"node_modules/x": npm("x", dev="yes")}),
        node({"node_modules/x": npm("x", os="darwin")}),
        node({"node_modules/x": npm("x", os=["../x"])}),
        '{"lockfileVersion": 3, "packages": {"": {}, "node_modules/x": '
        + json.dumps(npm("x"))
        + ', "node_modules/x": '
        + json.dumps(npm("x"))
        + "}}",
        '{"lockfileVersion": 3, "packages": {"": {}, "node_modules/x": ' + json.dumps(npm("x")) + '}, "n": NaN}',
        "\ufeff" + node({"node_modules/x": npm("x")}),
        node({}),
        node({"node_modules/x": npm("x")}).replace('"lockfileVersion": 3', '"lockfileVersion": 2'),
        "[]",
    ],
)
def test_node_refusals(text):
    assert verdict("node-package-lock", text) == {"refuse": "market.lock_invalid"}


def test_node_rows():
    lock = lockfile.check(
        "node-package-lock",
        node({"node_modules/ms": npm("ms", "2.1.3"), "node_modules/a/node_modules/b": npm("b", dev=True)}).encode(),
    )
    assert [(r.name, r.version, r.key, r.dev) for r in lock.rows] == [
        ("ms", "2.1.3", "node_modules/ms", False),
        ("b", "1.0.0", "node_modules/a/node_modules/b", True),
    ]
    assert lock.rows[0].hashes == (("00" * 64),)


def test_plan_python_markers_and_ambiguity():
    lock = lockfile.check(
        "python-requirements",
        py(f"a==1 --hash=sha256:{H}", f"w==1 ; sys_platform == 'win32' --hash=sha256:{H}").encode(),
    )
    got = lockfile.plan(lock, HOST)
    assert [(p.row.name, p.install, p.note) for p in got] == [("a", True, None), ("w", False, "other_platform")]
    twice = lockfile.check("python-requirements", py(f"a==1 --hash=sha256:{H}", f"a==2 --hash=sha256:{H}").encode())
    with pytest.raises(MarketError) as e:
        lockfile.plan(twice, HOST)
    assert (e.value.code, e.value.reason) == ("market.env_failed", "lock")
    split = lockfile.check(
        "python-requirements",
        py(
            f"a==1 ; sys_platform == 'darwin' --hash=sha256:{H}", f"a==2 ; sys_platform != 'darwin' --hash=sha256:{H}"
        ).encode(),
    )
    assert [p.install for p in lockfile.plan(split, HOST)] == [True, False]


def test_plan_node_dev_optional_and_platform():
    text = node(
        {
            "node_modules/ok": npm("ok"),
            "node_modules/d": npm("d", dev=True),
            "node_modules/fsevents": npm("fsevents", optional=True, os=["darwin"]),
            "node_modules/win": npm("win", optional=True, os=["win32"]),
            "node_modules/notmac": npm("notmac", optional=True, os=["!darwin"]),
        }
    )
    got = lockfile.plan(lockfile.check("node-package-lock", text.encode()), HOST)
    assert [(p.row.name, p.install, p.note) for p in got] == [
        ("ok", True, None),
        ("d", False, "dev"),
        ("fsevents", True, None),
        ("win", False, "other_platform"),
        ("notmac", False, "other_platform"),
    ]
    hard = lockfile.check("node-package-lock", node({"node_modules/win": npm("win", os=["win32"])}).encode())
    with pytest.raises(MarketError) as e:
        lockfile.plan(hard, HOST)
    assert (e.value.code, e.value.reason) == ("market.env_failed", "platform")
