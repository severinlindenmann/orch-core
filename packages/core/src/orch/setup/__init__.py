"""``orch setup``: one command that makes a workspace and opens the onboarding wizard (batch B9).

The CLI side (:mod:`orch.setup.flow`) is the SETUP PROCESS: it generates the secrets, holds them in zeroised buffers,
serves the wizard over a 0600 unix socket (:mod:`orch.setup.server`) and does the signing itself. The dashboard only
proxies the browser's calls (contract in ``~/orch-dev/state/batch-9.md``)."""
