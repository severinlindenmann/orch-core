"""Start agent (M7b, contract v1.6 sections 12.3 and 12.4, H11c): preview and start an agent run, and the person's
runner grant as far as this process may touch it.

Typed routes with closed input models, like the terminals: the host op and its arguments are fixed here. `agent.start`
is Tier A, dashboard-direct (no signature); the browser draws one idem per dialog opening and keeps it for every retry,
so a retry returns the recorded run (`r_` + the idem's ULID) and never starts a second process.

**The runner grant's secret never comes here.** Issuing the grant is a click request of the signer child (`click.py`,
the `runner_grant` action, `deposit:true`); the child hands the secret to the runner itself (`grant.deposit`, which
this process can never send: `hostlink.FORBIDDEN`). This module only reads the person's current runner grant
(`whoami.runner.grant`), asks where a deposit stands (`grant.deposit_status`, no secret) and forgets the grant
(`grant.forget`).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import reads
from .adapters import runs as project
from .gateway import Gateway
from .terminals import Idem, Label, Model

GrantId = Annotated[str, StringConstraints(pattern=r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
DepositId = Annotated[str, StringConstraints(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
Spec = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
DEPOSITS = ("confirmed", "rejected", "unknown", "none")
#: H11c-2's closed set for a rejected deposit (static codes, never host text); anything else reads `other`
DEPOSIT_REASONS = (
    "grant.forgotten",
    "grant.not_runner",
    "grant.verbs",
    "grant.expiring",
    "rate.limited",
    "runner.unavailable",
    "other",
)


HARNESS_WORDS = {"claude-code": "Claude", "codex": "Codex"}


class AgentPreviewBody(BaseModel):
    """`agent.preview`'s arguments (section 12.3). A model of its own: `term.preview`'s `mode` is `context` only."""

    model_config = ConfigDict(extra="forbid", strict=True)
    ticket: reads.Key  # required by the host (section 12.3); a run always belongs to a ticket
    harness: Literal["claude-code", "codex"]
    mode: Literal["refine", "work", "fix", "continue", "context"]
    model: Model | None = None
    subagent_model: Model | None = None
    label: Label | None = None
    worktree: bool = True

    def host_args(self) -> dict:
        fields = {"ticket", "harness", "mode", "model", "subagent_model", "label"}
        args = self.model_dump(exclude_none=True, include=fields)
        args["worktree"] = self.worktree
        return args


class AgentStartBody(AgentPreviewBody):
    spec: Spec
    cols: int | None = Field(default=None, ge=20, le=500)
    rows: int | None = Field(default=None, ge=5, le=200)
    idem: Idem


class ForgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grant: GrantId
    idem: Idem


def register(app: FastAPI, gateway: Gateway) -> None:
    @app.post("/api/w/{ws}/agents/preview")
    def preview(body: AgentPreviewBody, ws: str = Depends(reads.workspace)):
        return project.agent_preview(gateway.call(ws, "agent.preview", body.host_args()))

    @app.get("/api/w/{ws}/model-routing")
    def model_routing(ws: str = Depends(reads.workspace)):
        """The closed lists of models a start can name (core's `orch.modelchoices`, nothing from the host): the agents
        and their models, in the words the page shows."""
        from orch.modelchoices import MODEL_CHOICES, MODEL_LABELS

        gateway.enabled(ws)
        return {
            "harnesses": [
                {"id": h, "label": HARNESS_WORDS[h], "models": [{"id": m, "label": MODEL_LABELS[m]} for m in names]}
                for h, names in MODEL_CHOICES.items()
            ],
        }

    @app.post("/api/w/{ws}/agents/start")
    def start(body: AgentStartBody, ws: str = Depends(reads.workspace)):
        # Exactly the preview's values plus its spec (section 12.4: never a value edited between preview and start).
        args = {**body.host_args(), "spec": body.spec}
        args.update({k: v for k, v in (("cols", body.cols), ("rows", body.rows)) if v is not None})
        data = gateway.enabled(ws).call("agent.start", args, write=True, idem=body.idem)
        return {"run": project.run(data["run"])}

    @app.get("/api/w/{ws}/runner")
    def runner(ws: str = Depends(reads.workspace)):
        """The runner card: `whoami.runner` (connected, enabled, terminals, the caller's runner grant), or null when
        this host says nothing about a runner. Re-read after a live-stream resync: a runner restart drops the grant."""
        return {"runner": project.runner(gateway.call(ws, "whoami").get("runner"))}

    @app.get("/api/w/{ws}/runner/deposits/{deposit_id}")
    def deposit_status(deposit_id: DepositId, ws: str = Depends(reads.workspace)):
        """Where a deposit stands (the person's own; another's or a missing id reads `none`). No secret involved."""
        data = gateway.call(ws, "grant.deposit_status", {"deposit_id": deposit_id})
        got = data.get("deposit") if isinstance(data, dict) else None
        out = {"deposit": got if got in DEPOSITS else "unknown"}
        if out["deposit"] == "rejected":
            reason = data.get("reason")
            out["reason"] = reason if reason in DEPOSIT_REASONS else "other"
        return out

    @app.post("/api/w/{ws}/runner/forget")
    def forget(body: ForgetBody, ws: str = Depends(reads.workspace)):
        """ "Forget runner grant": the runner drops it for good; the grant stays issued, running agents keep theirs."""
        data = gateway.enabled(ws).call("grant.forget", {"grant": body.grant}, write=True, idem=body.idem)
        return {"forgotten": isinstance(data, dict) and data.get("forgotten") is True}
