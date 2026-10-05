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
  scope table and its refusals unchanged. Because Host, Origin and the cookie are the dashboard's own, the gate is
  the only thing between a device and full local power: the dispatcher refuses (`error`, nothing runs) any app that
  does not have `RemoteGate` as its outermost middleware (`app.user_middleware[0]`, read from the app handed over).
  Hand it the app built by `create_app`, never a sub-app or a router.
- Redirects are returned as they are, never followed. A malformed request (path shape, method, header value) is
  refused before anything runs.

## Things the caller must know

- Never rolls back. The body is handed to the app and its handler may finish before the response's first event is
  checked; a revocation in between yields `Refused("not_authorized")` although the change was applied. For a state
  change the check before running is the only gate that matters.
- `origin` is a snapshot. A scope downgrade without a revocation is honoured only if `still_authorized` answers
  False for it.
- A response is cut after `max_seconds`: the device must reconnect streams by itself.
- Response headers pass through as the app sent them, except `Set-Cookie` (any casing, and `Set-Cookie2`). The app
  emits no hop-by-hop headers; the host adds its own framing.
- Redirects carry absolute loopback URLs, and the host must not follow them.
- `still_authorized` has a timeout (`auth_timeout`) for an awaitable answer, and a timeout counts as no. A blocking
  synchronous callable cannot be interrupted: keep it cheap.
- An iterator that is abandoned, neither finished nor closed, is cut by a watchdog after
  `max_seconds + grace_seconds`; still, close it.
- `on_error(exc)` (optional) hears an exception from the app for the host's own log; nothing of it reaches the
  device, which only sees `Refused("error")`.

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
| `auth_timeout` | 2 | how long an async `still_authorized` may take; a timeout counts as no |
