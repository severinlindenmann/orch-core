"""The workspace icon's closed lists, a leaf module (no imports) shared by the model, the schemas, the signing review
and the readers. A workspace icon is only ever a pair of ids from here: no text, no URL, no upload.

The JSON schemas of ``settings.changed`` and the workspace, the host person op, the signing words and the dashboard
(``workspaceIcon.tsx``) repeat the lists; a test pins them all equal. Adding an id is a reviewed change in all of them.
"""

from __future__ import annotations

__all__ = ["COLOUR_LABELS", "ICON_LABELS", "fnv1a", "default_icon"]

ICON_LABELS = {
    "folder": "Folder", "rocket": "Rocket", "briefcase": "Briefcase", "building": "Building", "code": "Code",
    "cloud": "Cloud", "compass": "Compass", "globe": "Globe", "leaf": "Leaf", "flame": "Flame", "zap": "Bolt",
    "anchor": "Anchor", "heart": "Heart", "star": "Star",
}  # fmt: skip
COLOUR_LABELS = {
    "orange": "Orange", "blue": "Blue", "green": "Green", "purple": "Purple", "pink": "Pink", "teal": "Teal",
    "amber": "Amber", "slate": "Grey",
}  # fmt: skip


def fnv1a(text: str) -> int:
    """32-bit FNV-1a over the UTF-16 code units of ``text`` (what the dashboard's ``charCodeAt`` loop computes)."""
    h = 0x811C9DC5
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def default_icon(workspace_id: str) -> tuple[str, str]:
    """The icon and colour a workspace shows when none was signed: from its id (signed at creation), never its name."""
    h = fnv1a(workspace_id.strip().lower())
    icons, colours = list(ICON_LABELS), list(COLOUR_LABELS)
    return icons[h % len(icons)], colours[(h >> 8) % len(colours)]
