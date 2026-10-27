# Hub mockup (spec for the "hub full" build)

`index.html` is the clickable mockup of the orch hub that the owner reviewed on 27 Oct 2026. It is the **spec** for the
hub-full build: every requirement note in it is a GitHub sub-issue (IDs `HUB-<area>-nn`, see the master epic `[HUB] Hub full`
and its 15 epics).

- Open it in a browser (a single file, no build). The top bar has "Show requirement notes", "Slow network" and "Hub offline".
- Each page shows its requirement notes; the **Requirements** page lists all of them (status: today / partly / new).
- The demo data (Acme AG, 6 workspaces, people, services, approvals …) is the seed data for the visual parity checks:
  a real page with this data is screenshot-compared with the same mockup page.
- Owner decision 27 Oct: all notes are needed. Building starts only after the owner's go (after run 12 and the owner test).

Product text never names a VPN vendor: say "private network (VPN)", "office network", "public address".
