"""Property: any sequence of appends through the Store, then a fresh replay of the files, gives the returned State; the
projection files match the logs; and an external edit at any point is answered without breaking either."""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from orch import canon
from orch.store import StoreError
from orch.store.render import render_ticket
from tests.store.helpers import Env
from tests.store.test_append import replayed

pytestmark = pytest.mark.slow

OPS = "log title priority labels section unsection claim handoff new close reopen tamper_json tamper_body tick".split()
TEXTS = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x24F, blacklist_categories=("Cc", "Cs")), max_size=40
)
STEPS = st.lists(
    st.tuples(
        st.sampled_from(OPS),
        TEXTS,
        st.integers(0, 3),
    ),
    min_size=1,
    max_size=25,
)  # fmt: skip


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(steps=STEPS)
def test_random_appends_then_fresh_replay_equals_the_returned_state(tmp_path_factory, steps):
    env = Env(tmp_path_factory.mktemp("prop"))
    s = env.bootstrap()
    uids = [env.new_ticket()]
    for op, text, k in steps:
        uid = uids[k % len(uids)]
        try:
            if op == "log":
                env.log(uid, text or "n")
            elif op == "title":
                if text.strip() and "\n" not in text:
                    env.update(uid, {"ticket.title": text.strip()})
            elif op == "priority":
                env.update(uid, {"ticket.priority": ["low", "medium", "high", "urgent"][k]})
            elif op == "labels":
                env.update(uid, {"ticket.labels": sorted({f"l{k}", "x"})})
            elif op == "section":
                t = text.strip("\n")
                if t and not t.lstrip().startswith("#") and "\n#" not in t and "```" not in t and "~~~" not in t:
                    env.update(uid, body={["context", "requirements", "plan", "decisions"][k]: t})
            elif op == "unsection":
                env.update(uid, body={["context", "requirements"][k % 2]: None})
            elif op == "claim":
                s.append({"type": "claim.taken", "actor": env.agent}, log=uid)
            elif op == "handoff":
                t = text.strip("\n")
                if t and "#" not in t and "`" not in t and "~" not in t:
                    s.append({"type": "handoff.written", "actor": env.agent, "text": t}, log=uid)
            elif op == "new":
                uids.append(
                    env.new_ticket(text.strip()[:30] or "t") if text.strip() and "\n" not in text else env.new_ticket()
                )
            elif op == "close":
                env.close(uid)
            elif op == "reopen":
                s.append(env.person_event(env.owner, uid, "ticket.reopened"), log=uid)
            elif op == "tamper_json":
                p = env.path(uid, "ticket.json")
                d = json.loads(p.read_bytes())
                d["title"] = "tampered"
                p.write_text(json.dumps(d))
            elif op == "tamper_body":
                p = env.path(uid, "body.md")
                p.write_text(p.read_text() + "\n## Plan\n\nhand written\n")
            else:
                env.tick(k)
        except StoreError:
            pass  # a refusal (claim twice, edit a closed ticket...) changes nothing; the invariant is checked below
    s.scan()  # answer pending external edits
    fresh = replayed(env)
    assert not fresh.chain_errors and not fresh.workspace.invalid
    assert s.chain_errors() == [] and not s.diverged
    for uid, v in s.state.tickets.items():
        f = fresh.tickets[uid]
        assert f.fields == v.fields and f.sections == v.sections and f.status == v.status
        assert {g: x.gen for g, x in f.gates.items()} == {g: x.gen for g, x in v.gates.items()}
        assert env.path(uid, "ticket.json").read_bytes() == render_ticket(uid, f.key, f.fields)
        body = s.body_sections(uid)
        assert {k: canon.section_hash(t) for k, t in body.items() if t} == {
            k: x["hash"] for k, x in f.sections.items() if x["hash"] != canon.section_hash("")
        }
    assert s.scan() == []
    s.close()
