"""orch answer: answer a question"""

from typing import Any

from orch.ops._dsl import MSG, REF_PATTERN, STR, S, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.human import Human


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
    done = h.run(event, view.uid, f"answer {qid} of {view.key}")
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
