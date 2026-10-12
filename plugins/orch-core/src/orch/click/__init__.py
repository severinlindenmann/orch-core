"""Click-to-sign (orch-v2-host §0.8.6, D-P2-23): the person-class signer process beside the dashboard.

* :mod:`orch.click.protocol` — the channel's constants, op classes and :class:`ClickError` (no custody).
* :mod:`orch.click.spawn` and :mod:`orch.click.client` — what the dashboard server imports: start the child over a
  socketpair and call it. They never import custody code.
* :mod:`orch.click.signer`, :mod:`orch.click.view`, :mod:`orch.click.server` — the child (``python -I -m orch.click``):
  it alone holds the custody handle.

The host never imports this package (its audit hook refuses it).
"""
