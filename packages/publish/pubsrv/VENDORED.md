# Vendored code

`relay_proto.py` is a byte-identical copy of `packages/relay/src/orch_relay/proto/__init__.py` (PolyForm
Noncommercial 1.0.0 like this repo). publish ships as its own wheel and cannot import the relay package, so it
carries the copy; `tests/test_publish_api_auth.py::test_vendored_proto_is_the_relay_packages_proto` fails when the
two differ.

It provides `verify_delegation` and `verify_card`, both used by the hub card route (`PUT /v2/hub/cards/{ws}`, V3-D36:
the card is how a workspace is admitted). `relay_accept_card` stays vendored but is unused; the card vectors still run
against it to pin the file. Never edit it here: change it in `packages/relay` and copy it again.
