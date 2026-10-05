import pytest

from model_routing.routing import (Choice, ModelNameError, State, choose, model_of, next_tier)

S = {"tier_light": "haiku", "tier_standard": "sonnet", "tier_strong": "opus", "mode_refine": "strong",
     "mode_work": "standard", "mode_fix_checks": "light", "mode_continue": "same"}


def pick(mode, settings=S, **kw):
    kw.setdefault("strong_next", False)
    kw.setdefault("escalation", None)
    kw.setdefault("last_tier", None)
    return choose(settings, mode, **kw)


@pytest.mark.parametrize("mode,tier,model", [("refine", "strong", "opus"), ("work", "standard", "sonnet"),
                                             ("fix-checks", "light", "haiku")])
def test_each_mode_gets_its_tier_and_alias(mode, tier, model):
    c = pick(mode)
    assert (c.tier, c.model) == (tier, model) and mode in c.reason and model in c.reason


def test_work_may_be_opusplan():
    assert pick("work", {**S, "tier_standard": "opusplan"}).model == "opusplan"


def test_continue_repeats_the_last_start_and_falls_back_to_standard():
    assert pick("continue", last_tier="strong").model == "opus"
    assert pick("continue", last_tier="light").model == "haiku"
    c = pick("continue")
    assert c.tier == "standard" and c.model == "sonnet"


def test_nothing_is_set_by_default():
    c = pick("refine", {})
    assert c == Choice(None, None, "No tier is set for refine: the harness default")
    assert pick("continue", {}).model is None  # same, with no model saved: nothing to run


def test_none_means_the_harness_default():
    assert pick("work", {**S, "mode_work": "none"}).model is None


def test_light_is_optional_and_falls_back_to_standard():
    c = pick("fix-checks", {**S, "tier_light": ""})
    assert (c.tier, c.model) == ("standard", "sonnet") and "Light has no model" in c.reason


def test_a_tier_without_a_model_says_so():
    c = pick("refine", {**S, "tier_strong": ""})
    assert c.model is None and "Strong has no model set" in c.reason


def test_the_humans_mark_wins_over_the_mode():
    c = pick("work", strong_next=True)
    assert (c.tier, c.model) == ("strong", "opus") and "You marked" in c.reason


def test_an_accepted_escalation_names_the_receipt():
    c = pick("work", escalation={"tier": "strong", "task": "T3", "receipt": "receipt-T3-1.log"})
    assert c.model == "opus" and "receipt-T3-1.log" in c.note and "T3" in c.note and "T3 failed" in c.reason


def test_an_escalation_with_odd_task_or_receipt_adds_no_note():
    for esc in ({"tier": "strong", "task": "x; rm", "receipt": "r.log"}, {"tier": "strong", "task": "T3", "receipt": "../x"}):
        assert pick("work", escalation=esc).note == ""


@pytest.mark.parametrize("bad", ["--dangerously-skip-permissions", "opus; ls", "a b", "x" * 65])
def test_a_setting_that_is_no_model_name_is_refused(bad):
    with pytest.raises(ModelNameError, match="is not a model name"):
        model_of({"tier_strong": bad}, "strong")
    with pytest.raises(ModelNameError):
        pick("refine", {**S, "tier_strong": bad})


def test_aliases_and_full_names_are_taken_as_they_are():
    for name in ("opus", "opusplan", "sonnet[1m]", "claude-opus-5-5"):
        assert model_of({"tier_strong": f"  {name} "}, "strong") == name


def test_one_tier_up():
    assert [next_tier(t) for t in ("light", "standard", "strong", None)] == ["standard", "strong", None, "strong"]


def test_state_remembers_starts_and_one_shot_escalations(tmp_path):
    st = State(tmp_path / "x")
    assert st.last_tier("D-1") is None and st.escalation("D-1") is None and st.handled("D-1", "T1") == 0
    st.record_start("D-1", "light", "work", "D-1")
    st.set_escalation("D-1", "standard", "T1", "r.log", 2)
    st.set_handled("D-1", "T1", 2)
    again = State(tmp_path / "x")
    assert again.last_tier("D-1") == "light" and again.escalation("D-1")["tier"] == "standard"
    assert again.handled("D-1", "T1") == 2
    again.consume_escalation("D-1")
    assert State(tmp_path / "x").escalation("D-1") is None
    assert State(tmp_path / "x").last_tier("D-1") == "light"


def test_a_damaged_state_file_is_empty_state(tmp_path):
    (tmp_path / "state.json").write_text("[1,2", encoding="utf-8")
    assert State(tmp_path).last_tier("D-1") is None
    (tmp_path / "state.json").write_text('{"starts": [], "handled": 5}', encoding="utf-8")
    assert State(tmp_path).handled("D-1", "T1") == 0
