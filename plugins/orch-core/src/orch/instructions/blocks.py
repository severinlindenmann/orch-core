from __future__ import annotations

from orch.errors import ValidationError

BEGIN = "<!-- orch:begin -->"
END = "<!-- orch:end -->"
NOTICE = "<!-- Managed by `orch instructions sync`. Edit outside the orch markers; text inside them is overwritten. -->"


def wrap(body: str) -> str:
    return f"{BEGIN}\n{NOTICE}\n{body.strip()}\n{END}"


def has_block(text: str) -> bool:
    return BEGIN in text


def apply_block(existing: str | None, block: str, *, stub: str = "") -> str:
    """Return the file text with the managed `block` (already wrapped) inserted or replaced.

    An existing file keeps its line endings: a CRLF file comes back CRLF throughout."""
    if existing is None:
        return block + "\n" + (f"\n{stub.strip()}\n" if stub else "")
    crlf = "\r\n" in existing
    text = _apply_lf(existing.replace("\r\n", "\n"), block.replace("\r\n", "\n"))
    return text.replace("\n", "\r\n") if crlf else text


def _apply_lf(text: str, block: str) -> str:
    begins, ends = text.count(BEGIN), text.count(END)
    if begins == 0 and ends == 0:
        return text.rstrip("\n") + "\n\n" + block + "\n"  # append: the customer's content stays first
    if begins != 1 or ends != 1 or text.index(BEGIN) > text.index(END):
        raise ValidationError(
            "the orch markers in this file are broken",
            hint=f"keep exactly one {BEGIN} … {END} pair, then re-run `orch instructions sync`",
        )
    start, stop = text.index(BEGIN), text.index(END) + len(END)
    return text[:start] + block + text[stop:]
