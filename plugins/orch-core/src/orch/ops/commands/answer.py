"""orch answer: answer a question"""

from typing import Any

from orch import canon
from orch.host import signcontent
from orch.ops._dsl import MSG, REF_PATTERN, STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human
from orch.store.render import thaw


def _review(h: Human, view: Any, doc: Any, event: dict[str, Any]) -> list[str]:
    """The terminal review of an answer: the SAME renderer as every signing surface (#1123 section 4,
    :func:`orch.host.signcontent.build` of the stored raw question and this event, then its plain text), so it holds
    every bound field. A question or answer that renderer does not know refuses (fail closed): the model would refuse
    the answer too, and no review that hides a bound byte is ever shown."""
    material = {"kind": "question", "ticket": {"key": view.key, "uid": view.uid}, "question": thaw(doc)}
    prepared = dict(op="answer", events=[event], material=material)  # noqa: C408 - keywords keep the layout lint quiet
    try:
        # verified: the question and its hash are the model's own (replay recomputed the hash from this question)
        content = signcontent.build(prepared, verified=True)
    except signcontent.PlainUnavailable as exc:
        raise OrchError("invalid.input", f"this answer cannot be shown for review: {exc}") from None
    return [*h.header(view), *signcontent.text(content, canon.clean)]


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    h = Human(ctx, "answer")
    view = h.ticket(args.get("ref"))
    qid = args["question"]
    q = next((x for x in view.questions if x.id == qid), None)
    if q is None:
        raise OrchError("not_found", f"{view.key} has no question {qid}")
    text = h.text(args, what="the answer")
    option = args.get("option")
    if option is None and text is None:
        raise OrchError("invalid.input", "answer with --option KEY, -m TEXT, or both")
    event: dict[str, Any] = {"type": "question.answered", "question": qid, "hash": q.hash}
    if option is not None:
        event["option"] = option
    if text is not None:
        event["text"] = text
    doc = next((x for x in view.fields["questions"] if x["id"] == qid), None)
    done = h.run(event, view.uid, f"answer {qid} of {view.key}", review=_review(h, view, doc, event))
    return h.ticket_result(view, done, {"question": qid}, f"orch show {view.key}")


OP = operation(
    "answer",
    "Human only",
    "Answer a question with an option, text, or both.",
    who="human",
    props={
        "question": S("question id", pattern=r"^Q[1-9][0-9]*$", **{"x-metavar": "Q"}),
        "option": S("option key", **{"x-metavar": "KEY"}),
        "message": MSG,
        "ref": S("ticket REF (flag)", pattern=REF_PATTERN, **{"x-metavar": "REF"}),
    },
    required=("question",),
    positional=("question",),
    pre=("ticket_exists", "question_open", "role_allows", "user_presence", "text_clean"),
    emits=("question.answered",),
    text="ok {key} question.answered {question} seq={seq}\nnext: {next}",
    data=obj({"question": STR}),
    errors=(err("role.denied"), err("parse.text"), err("not_found")),
    handler=handle,
)
