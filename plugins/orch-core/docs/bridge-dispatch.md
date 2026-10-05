# Bridge dispatcher

`orch.dashboard.bridge_dispatch` runs one request against the dashboard's ASGI app inside the host process and
returns the response as events while the app produces them. It has no crypto, no mailbox, no network client and no
thread. It is a building block of the remote bridge; nothing calls it yet.

```python
async for event in dispatch(app, BridgeRequest(method, "/path?query", headers, body), origin,
                            still_authorized=check, limits=Limits()):
    ...  # Start(status, headers), Body(chunk)..., End()   or a final Refused(reason)
```

Own the iterator: wrap it in `contextlib.aclosing` (or call `aclose()`), because an async generator is closed by
its owner. Leaving it by break, exception, cancel or `aclose()` disconnects the app, waits for it to finish and
cancels it after `grace_seconds`, so a streaming route ends and no task is left behind.

## Rule for the caller

Call it only with an `origin` built from a verified, authorised decision: the device is known, its scope is current
and, where the route needs it, a fresh confirmation was verified for this very request. The dispatcher does not
check who the device is; it only makes the dashboard see the request exactly as a bridged one. Never build an
origin from anything a device sent.

## What the dashboard sees

- The request headers are an allow-list (`ALLOWED_HEADERS`); everything else the caller supplies is dropped.
  Host and Origin are the dashboard's own loopback values, and the dashboard's own session cookie is added by this
  module. Neither the cookie nor any `Set-Cookie` ever appears in the events.
- The client address is not loopback, and the scope carries `orch.remote`, so the outermost remote gate applies its
  scope table and its refusals unchanged. The gate is not bypassed.
- Redirects are returned as they are, never followed. A malformed request (path shape, method, header value) is
  refused before anything runs.

## Refusals

`Refused(reason)` is always the last event: `bad_request`, `not_authorized`, `too_large`, `timeout` or `error`
(the app failed or answered out of order). All fail closed.

`still_authorized` (sync or async; only `True` goes on, an error means no) is asked before the app runs, before the
response's first event and before every body event, so a revoked device is cut off in the middle of a stream.

## Limits (injected, conservative defaults)

| field | default | effect |
| --- | --- | --- |
| `max_request_bytes` | 1 MiB | a larger body is refused (`too_large`) before running |
| `max_response_bytes` | 16 MiB | the response ends with `too_large` |
| `max_seconds` | 120 | the whole response, streams included, ends with `timeout` |
| `max_chunk` | 32 KiB | larger pieces from the app are split into events of at most this size |
| `grace_seconds` | 2 | how long the app may finish after a disconnect before it is cancelled |
