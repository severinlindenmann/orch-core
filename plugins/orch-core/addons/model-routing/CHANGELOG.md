# Changelog

## 0.1.0

- First version (issue #47): a model per Start agent mode (three tiers, aliases recommended), a subagent model,
  "next start on the Strong model" per ticket, and one decision card per task that failed its verify in two separate
  sessions, offering the next session one tier up with the failure log named in its prompt.
- Uses the new `launch` addon capability (API 2.7); off until a human enables the addon in a workspace.
