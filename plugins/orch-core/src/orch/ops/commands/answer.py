"""orch answer: answer a question"""

from orch.ops._dsl import MSG, REF_PATTERN, STR, S, err, obj, operation

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
)
