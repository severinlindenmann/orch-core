---
id: DEMO-0003
title: Cost view
type: feature
priority: high
size: s
status: in-progress
created: 2026-09-03T09:00Z
updated: 2026-09-03T09:00Z
external:
- key: FIN-12
  url: https://jira.example.com/browse/FIN-12
repos:
- pipelines
- elsewhere
branches:
  pipelines: feature/DEMO-0003-cost
  elsewhere: x
worktrees: {}
prs:
- repo: pipelines
  url: https://github.com/acme/pipelines/pull/7
parent: DEMO-0002
due: '2026-10-30'
blocked_by:
- DEMO-0001
- DEMO-0099
follow_ups: []
labels:
- Needs Review!
gates:
  requirements:
    approved: 2026-09-03T12:00Z
    via: human
    hash: sha256:abc
  plan:
    approved: null
    via: null
    hash: null
  verify:
    verdict: null
    at: null
    via: null
questions:
- id: Q1
  text: Which month?
  why: Seeds differ.
  type: single
  blocking: true
  options:
  - key: jan
    label: January
    cost: null
  - key: feb
    label: February
    cost: null
  recommended: jan
  asked: 2026-09-03T10:00Z
- id: Q2
  text: Use the API?
  type: confirm
  blocking: false
  answer: 'yes'
  answered: 2026-09-03T11:00Z
  options:
  - key: 'yes'
    label: 'Yes'
  - key: 'no'
    label: 'No'
claim:
  session: null
  harness: null
  at: null
sessions: []
artifacts:
- name: after.png
  kind: screenshot
  sha256: e1c58e396f17a33c4d4be764b44df068d6d84ba7a442906884c886b80ac07363
  size: 24
  ac: 1
  label: After
- name: sub/out.log
  kind: receipt
  sha256: 1eef57a46547bb733838e27741c428a371e0c7d37124c9dec3e7fad74b03f01d
  size: 10
  task: T2
- name: gone.csv
  kind: dataset
  sha256: 2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881
  size: 1
- url: https://ci.example.com/run/1
  kind: build
  label: CI
---

# DEMO-0003 — Cost view

## Context

See ![after](artifact:after.png) and ![gone](artifact:gone.csv).

```text
## not a heading
```

## Requirements

- Show cost by month.

## Acceptance criteria

- [ ] Loads in 5 s.
- [x] Totals match the invoice
  within 1 percent.


## Out of scope

Forecasts.

## Plan

1. View.
2. Dashboard.

## Tasks

- [x] T1 Create the view
  - verify: cmd: pytest -q
  - ref: ac:1
- [/] T2 Build the dashboard
  - owner: human
  - ref: ac:2
  - note: working
- [ ] T3 Share it
  - needs: T2


## Current state

Dashboard half done.

## Verification

- AC1: ![after](artifact:after.png)

## Log

- 2026-09-03T09:00Z [you] created

## Findings

A finding.
