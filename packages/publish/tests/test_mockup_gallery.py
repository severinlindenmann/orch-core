"""Product copy/style changes require regenerating the committed visitor gallery."""

import importlib.util
from pathlib import Path

GALLERY = Path(__file__).resolve().parents[3] / "docs" / "mockups" / "publish"


def test_mockup_gallery_is_fresh():
    spec = importlib.util.spec_from_file_location("publish_mockup", GALLERY / "render.py")
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert (GALLERY / "index.html").read_bytes() == generator.render().encode("utf-8"), (
        "Regenerate: cd packages/publish && uv run python ../../docs/mockups/publish/render.py"
    )
