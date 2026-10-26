"""The closed lists of models a start can use, a leaf module (no imports).

A model name reaches an argv only if it is here (an add-on that chooses the model answers with one of these names).
Adding one is a reviewed code change.
"""

from __future__ import annotations

__all__ = ["MODEL_CHOICES", "MODEL_LABELS"]

#: The only model names that ever reach an argv, per harness (a reviewed code change).
#: ``None`` ("harness default") is not a name: no ``--model`` is passed.
MODEL_CHOICES: dict[str, tuple[str, ...]] = {
    "claude-code": ("fable", "opus", "sonnet", "haiku"),
    # TODO(owner): the Codex ids wait for the owner's confirmation; edit ONLY this tuple.
    "codex": ("gpt-5.4", "gpt-5-codex", "gpt-5", "o3"),
}
MODEL_LABELS = {
    "fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku",
    "gpt-5.4": "GPT-5.4", "gpt-5-codex": "GPT-5 Codex", "gpt-5": "GPT-5", "o3": "o3",
}  # fmt: skip
