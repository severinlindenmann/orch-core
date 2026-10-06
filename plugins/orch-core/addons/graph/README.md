# Graph

Switches on the **Graph** page in Mission Control: tickets, the files their commits changed and the links between
tickets, as a code map, a dependency view and a view around one ticket. Off until you enable it per workspace in
Workspace & addons (or `orch addon enable graph` in your own terminal). Once on, **Graph** appears in the menu under
**Addons**.

The page is part of core; this addon is only its switch. While it is off, `/graph`, `/graph.json` and
`/graph/related` answer 404. The `orch graph` and `orch related` commands work either way.

No settings, no binaries, no background fetches.
