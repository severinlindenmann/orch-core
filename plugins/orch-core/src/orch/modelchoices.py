"""Model routing's closed lists, a leaf module (no imports) so the model, the schemas and the host share one source.

A model name reaches an argv or a signed setting only if it is here. Adding one is a reviewed code change.
"""

from __future__ import annotations

__all__ = ["MODEL_CHOICES", "MODEL_LABELS", "ROUTE_MODES"]

#: The only model names that ever reach an argv or the signed setting, per harness (a reviewed code change; the JSON
#: schemas of ``settings.changed`` and the workspace repeat the lists and a test pins them equal).
#: ``None`` ("harness default") is not a name: no ``--model`` is passed.
MODEL_CHOICES: dict[str, tuple[str, ...]] = {
    "claude-code": ("fable", "opus", "sonnet", "haiku"),
    "codex": ("gpt-5.4", "gpt-5-codex", "gpt-5", "o3"),
}
MODEL_LABELS = {
    "fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku",
    "gpt-5.4": "GPT-5.4", "gpt-5-codex": "GPT-5 Codex", "gpt-5": "GPT-5", "o3": "o3",
}  # fmt: skip
#: the Start agent modes a model can be set for (a "Read and answer" start is mode ``context``)
ROUTE_MODES = ("work", "refine", "fix", "continue", "context")
