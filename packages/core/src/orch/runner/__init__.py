"""The orch runner (D-P2-24): the per-UID service that owns every long-lived child of the addons #307/#308.

Tier A runs it as the person (a LaunchAgent ``io.orch.runner`` or the systemd user unit ``orch-runner.service``). It
supervises agent sessions and terminals through a fixed trampoline and never holds authority of its own: same-UID
separation is not containment; runner reports are untrusted and cannot establish authorization; the host accepts them
only for runs assigned to that runner.
"""
