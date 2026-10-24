"""V3 G4 (C6): the dependency diff compares what this computer INSTALLS, so a package that keeps its version and hash
but starts to be installed here (flag flips) is changed and widens an update."""

from __future__ import annotations

from orch.addons import lockfile
from orch.market import deps
from tests.addons.test_lockfile import HOST, node, npm

KIND = "node-package-lock"


def planned(pkgs: dict, host=HOST):
    lock = lockfile.check(KIND, node(pkgs).encode())
    return lockfile.plan(lock, host)


def diff(old: dict, new: dict, host=HOST):
    return deps.diff(KIND, planned(old, host), KIND, planned(new, host))


def test_nothing_changed_is_not_widened():
    p = {"node_modules/x": npm("x", optional=True, os=["win32"])}
    d = diff(p, p)
    assert d["widened"] is False and d["changed"] == [] and d["lines"] == []


def test_optional_to_required_on_this_platform_is_changed_and_widened():
    old = {"node_modules/x": npm("x", optional=True, os=["win32"])}  # skipped here
    new = {"node_modules/x": npm("x", os=["darwin"])}  # now installed here, same version and hash
    d = diff(old, new)
    assert d["widened"] is True and d["changed"] == [{"name": "x", "from": "1.0.0", "to": "1.0.0"}]
    assert "~ dep x 1.0.0 -> 1.0.0 (install status changed)" in d["lines"]


def test_dev_to_prod_is_changed_and_widened():
    d = diff({"node_modules/x": npm("x", dev=True)}, {"node_modules/x": npm("x")})
    assert d["widened"] is True and d["changed"][0]["name"] == "x"


def test_an_os_or_cpu_change_that_makes_a_package_install_here_is_widened():
    for field, was, now in (("os", ["win32"], ["darwin"]), ("cpu", ["x64"], ["arm64"])):
        d = diff(
            {"node_modules/x": npm("x", optional=True, **{field: was})},
            {"node_modules/x": npm("x", optional=True, **{field: now})},
        )
        assert d["widened"] is True, field


def test_flags_that_change_nothing_installed_still_count_as_changed_and_going_dev_does_not_hide_it():
    d = diff({"node_modules/x": npm("x")}, {"node_modules/x": npm("x", dev=True)})
    assert d["changed"], "a flag change is shown even when it removes the package from the install set"


def test_a_python_marker_change_that_installs_here_is_widened():
    h = "a" * 64
    kind = "python-requirements"

    def plan(text):
        return lockfile.plan(lockfile.check(kind, text.encode()), HOST)

    old = plan(f"a==1 ; sys_platform == 'win32' --hash=sha256:{h}\n")
    new = plan(f"a==1 ; sys_platform == 'darwin' --hash=sha256:{h}\n")
    assert deps.diff(kind, old, kind, new)["widened"] is True
    assert deps.diff(kind, new, kind, new)["widened"] is False
