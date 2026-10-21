"""Dashboard-compatible widget structure parsing, independent of add-on policy."""

from .core_types import CORE_TYPES, PROPOSED_TYPES
from .limits import (
    MAX_BLOCK_BYTES,
    MAX_BLOCKS,
    MAX_COLUMNS,
    MAX_DEPTH,
    MAX_DIFF_LINES,
    MAX_LABEL,
    MAX_MAGNITUDE,
    MAX_ROWS,
    MAX_SERIES,
    MAX_STRING,
)
from .parse import (
    WIDGET_SECTIONS,
    Block,
    BlockResult,
    MarkdownSegment,
    Segment,
    WidgetSegment,
    clip_reason,
    parse_block,
    parse_section,
    resolve_ticket_widgets,
    segments_to_json,
    spec_to_json,
)
from .strict import strict_json

__all__ = [
    "CORE_TYPES",
    "PROPOSED_TYPES",
    "WIDGET_SECTIONS",
    "MAX_BLOCK_BYTES",
    "MAX_BLOCKS",
    "MAX_COLUMNS",
    "MAX_DEPTH",
    "MAX_DIFF_LINES",
    "MAX_LABEL",
    "MAX_MAGNITUDE",
    "MAX_ROWS",
    "MAX_SERIES",
    "MAX_STRING",
    "Block",
    "BlockResult",
    "MarkdownSegment",
    "Segment",
    "WidgetSegment",
    "clip_reason",
    "parse_block",
    "parse_section",
    "resolve_ticket_widgets",
    "segments_to_json",
    "spec_to_json",
    "strict_json",
]
