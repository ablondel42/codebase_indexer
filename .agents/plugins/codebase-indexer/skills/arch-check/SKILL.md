---
name: arch-check
description: Run token-lean Hexagonal architecture, Ruff, Pyright, and optional targeted pytest verification via ./cbi check or the isolated arch-auditor subagent.
---

# Architecture & Hygiene Check (`/arch-check`)

Use this skill only when explicitly requested by the user or required by an approved implementation plan gate.

## Options
1. **Direct Fast Gate** (single summary turn):
   Run `./cbi check` (or `./cbi check --tests <test_file>` if targeted pytest verification was requested).
2. **Context-Isolated Audit** (recommended after multi-file edits or plan review gates):
   Invoke the `arch-auditor` subagent with instructions to run `./cbi check` (plus any requested `--tests <targets>`) and return its `<25`-line `PASS` / `FAIL` digest so raw lint, type-check, and pytest output stays out of the main conversation context.
