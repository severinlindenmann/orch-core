---
title: Architecture overview
documents:
  - src/acme/ingest/**
  - "acme-energy-data:sql/*.sql"
---
# Architecture

The ingest service loads meter readings into bronze tables. DEMO-3 moved the loader to Lakeflow; see also DEMO-0007.

Code: [loader](https://github.com/acme/ticket-orch-demo/blob/main/src/acme/ingest/loader.py)
