"""R6: pairing, membership, cards and WK custody, host side (contract 11A.15). R6a (this package so far): key custody
(:mod:`.keystore`: ``membership/keys/``, ``head.json``, EHW recovery, the orphan rule, burned epochs, the seal lock,
the ``EpochKeySource`` R2 reads and :class:`.keystore.R6Gate`) and the first card (:mod:`.firstcard`).

Only ``orch.relay.r6`` imports the key store (``tests/relay/test_import_ban.py``); R2 reaches epoch keys only through
``orch.relay.keys.BridgeKeys``.
"""
