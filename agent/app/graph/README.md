# `app/graph/` — LangGraph nodes

Per CLAUDE.md conventions: **one file per graph node.** No nodes are implemented
in Phase 0 (scaffold) — this directory is intentionally empty of logic.

Planned nodes (see PRD §6), added in later phases:

- `interrogator.py` — generates/loads the buyer-intent query panel per category
- `engine_runner.py` — fans out each query across engines; returns raw answers + citations
- `extractor.py` — parses recommended brands/products, order, and cited sources
- `share_of_model.py` — computes recommendation rate vs. named competitors
- `shopping_agent_simulator.py` — Browserbase browser agent recording parse/selection failures
- `diagnostician.py` — maps competitor wins + failures to specific data gaps
- `optimizer.py` — **grounded** fixes only (never fabricate attributes; before/after diff + source)
- `approval_gate.py` — human review; nothing publishes without it (hard rule)
- `publisher.py` — writes approved changes via Shopify Admin API; re-reads to confirm parse
- `verifier.py` — re-runs the panel, computes uplift delta, updates precedent memory
