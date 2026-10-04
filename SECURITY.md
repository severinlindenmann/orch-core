# Security policy

orch-core exists to keep certain decisions with a human, so a weakness in that boundary is a security issue, not just a bug.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability reporting:

https://github.com/severinlindenmann/orch-core/security/advisories/new

Please do **not** open a public issue, pull request or discussion for a suspected vulnerability, and do not post working bypass steps anywhere public. Describe the problem in the private report; it is handled there until a fix is released.

Include what you can: the affected version or commit, what an attacker (or an agent) can achieve, and the steps to reproduce it.

## In scope

- Anything that lets an agent, an addon or another process act as the human: approving, answering, giving a verdict, closing or moving a ticket where only a human may, or getting such a decision counted without the human's signed approval.
- Getting a decision to bind text other than what the human was shown (gate, question and verdict hashes, pinned artifacts and widget files).
- Bypassing the guard hook for the actions it is documented to stop.
- Mission Control: cross-site scripting, cross-site request forgery, token leaks, or agent-written content (Markdown, widgets, agent HTML) escaping its sandbox.
- Addons: running code or commands outside what a trusted version allows, or escaping the trust pin.
- Phone pairing and remote decisions: forging, replaying or misapplying a decision.
- Reading or changing the approval ledger or its key through orch itself.

## Out of scope

- Limits the documentation already states, such as code running as your own user outside the guard's view (see "Who is the human" in the plugin README).
- Problems in third-party tools orch calls (`gh`, `databricks`, `tmux`, Claude Code itself); report those upstream.
- Issues that need an already compromised machine or user account.
