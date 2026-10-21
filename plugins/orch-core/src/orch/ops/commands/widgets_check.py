"""orch widgets check: dry run of the write-time widget check on a text file"""

from typing import Any

from orch.ops._dsl import FILE, INT, STR, E, arr, err, obj, operation
from orch.ops.base import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call


def handle(ctx: Context, args: dict[str, Any]) -> Result:
    """Run the section-write validator; a file that starts with ``{`` and has no fence is one bare block."""
    from orch.widgets import check

    c = Call.of(ctx, "widgets.check")
    text = c.text(c.read_file_text(args["file"]), limit=65536, what="file").strip("\n")  # as section set does
    section = args.get("section") or "context"
    if text.lstrip().startswith("{") and not check.has_fence(text):
        text = "```orch\n" + text.strip("\n") + "\n```"
    blocks = check.count_blocks(text, section)
    problems = check.check_section_text({}, section, text, templates_ok=True)
    if problems:
        raise OrchError(
            "parse.text", check.message(problems, section).replace("The text was not stored; run: ", "Fix, then run: ")
        )
    note = ["note: template blocks also need the Widgets add-on to be on"] if _has_template(text, section) else []
    return Result(
        data={"blocks": blocks, "problems": []},
        lines=[f"{blocks} widget block(s) are valid in {section}", *note],
        hints=["orch section set " + section + " --file FILE"],
    )


def _has_template(text: str, section: str) -> bool:
    from orch.widgets import WidgetSegment, parse_section

    return any(
        isinstance(s, WidgetSegment) and s.block.spec is not None and s.block.spec["layer"] != "type"
        for s in parse_section(text, section)
    )


OP = operation(
    "widgets.check",
    "Context",
    "Check the widget blocks in a text file the way a section write does; nothing is stored.",
    who="read",
    props={
        "file": FILE,
        "section": E(
            "section the text is for (default context)",
            "summary",
            "context",
            "requirements",
            "out_of_scope",
            "plan",
            "decisions",
            "verification",
            "findings",
            "current_state",
            **{"x-metavar": "SECTION"},
        ),
    },
    required=("file",),
    positional=("file",),
    text="ok widgets.check {blocks}\nnext: {next}",
    data=obj({"blocks": INT, "problems": arr(STR)}),
    errors=(err("parse.text"),),
    handler=handle,
)
