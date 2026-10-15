"""The host bridge client: the core side of orch-relay (contract 11A, ``docs/architecture/orch-v2-host.md``).

R1 modules: :mod:`.wire` (pure codecs), :mod:`.sign` (``RelaySigner``), :mod:`.http` (the stdlib transport),
:mod:`.retry` (refusal classes and backoff), :mod:`.session` (challenge, login, holder), :mod:`.card` (card checks and
the sealed part), :mod:`.directory` (card, genesis, member list and checkpoint calls that verify what they return) and
:mod:`.link` (the first-link sequence). Nothing here imports the host, the store's signer or custody
(``tests/relay/test_import_ban.py``).
"""
