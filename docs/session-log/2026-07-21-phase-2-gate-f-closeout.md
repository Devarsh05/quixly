# 2026-07-21 — Phase 2: Gate F closeout (live webhook verification)

**Fix landed as `f0b97bc` on `fix-webhook-topic-dispatch`** (PR-gated to `main`); this log
written on the same branch (docs-only).
**Goal:** close Gate F by proving, against the live dev store, that Shopify webhooks reach the
agent and persist — `products/update` (HMAC + DB write) and `app/uninstalled` (status flip).

---

## Gate F closed

Both halves proven against the live dev store (`quixly-ljymkoyb.myshopify.com`, shop 92):

- **`products/update`** — a real admin title edit on product 128 delivered over the reserved
  tunnel, passed HMAC, and updated `public.products` (title + `updated_at`).
- **`app/uninstalled`** — a real uninstall flipped `public.shops` id 92 `active → uninstalled`
  and invalidated the cached admin token; a subsequent real reinstall flipped it back and
  re-ingested (see Verification).

## Root cause: dispatch on the wrong topic form

The agent dispatched forwarded webhooks on the REST-header topic string (`products/update`), but
the app shell forwards Shopify's library-canonical form. `authenticate.webhook()` runs the topic
through `topicForStorage()` (`toUpperCase().replace(/\/|\./g,'_')`), so the shell sends
**`PRODUCTS_UPDATE`** / **`APP_UNINSTALLED`**, not `products/update` / `app/uninstalled`. Every
forwarded webhook fell through the `if/elif` to a **204 no-op** — the app returned **200** while
`public.products` / `public.shops` was never written. A green 200 masking zero DB effect.

The handler itself was correct: called directly with the REST form it wrote the row every time,
which is exactly why the prior REST-only unit tests passed green over the bug.

**Fix** (`agent/app/api/webhooks.py`): a `_canonical_topic()` helper mirrors `topicForStorage()`
and dispatch matches on `PRODUCTS_UPDATE` / `APP_UNINSTALLED`, still tolerating the REST form for
direct internal calls and redeliveries. Tests (`agent/tests/test_webhooks.py`) are parametrized
over **both** forms and now assert the **row was actually written** (title/`updated_at` changed,
status flipped) — reverting the dispatch fails the real-form cases while the REST-form cases still
pass. Committed as **`f0b97bc`** (104 passed, ruff clean).

## What actually consumed the session: dev-tunnel infrastructure

The handler bug was a two-line dispatch mismatch; the session went to three stacked
dev-infrastructure problems, each masking the next:

1. **Rotating tunnel.** Bare `npm run dev` (`shopify app dev`) spawns a new Cloudflare
   quick-tunnel URL every restart; with webhook URIs resolving against the last-released
   `application_url`, deliveries went to stale/other hosts and never reached local, and
   `shopify app dev` does not re-point webhook URIs on dev. Fixed by moving to a reserved ngrok
   static domain (`debating-persuaded-patrol.ngrok-free.dev`) via `--tunnel-url`, plus one
   `shopify app deploy` to release the config.
2. **CLI-proxy self-forward collision.** With `--tunnel-url=…:3000` the CLI proxy binds port 3000
   *and* forwards to its declared app port — which also defaulted to 3000. The app couldn't bind
   3000 (proxy had it) and bumped to 3001, so the proxy forwarded to **itself**: a ~31k-socket
   connection storm with instant-fail/hang responses. Fixed by pinning the app's dev port to
   **3001** in `app/shopify.web.toml` (proxy 3000 → app 3001).
3. **Orphaned node processes (Windows).** `Ctrl-C` on `shopify app dev` left `node` processes
   holding 3000/3001/3457 across restarts, re-triggering the collision. Cleared with
   `taskkill /IM node.exe /F` before relaunch; adopted as standard practice.

## Verification (end-to-end)

- Bogus-HMAC `POST` to the webhook route → **401 in 0.19s** (reaches the real handler; HMAC rejects).
- Real title edit on product 128 → **200**, `public.products` row updated (title + `updated_at`).
- Real uninstall → `app/uninstalled` **200**, `public.shops` id 92 `active → uninstalled`.
- Real reinstall (the actual `afterAuth` path, against a live stack): the OAuth grant via `/auth`
  created a fresh `shopify.Session`, flipped `public.shops` id 92 `uninstalled → active`, and
  `afterAuth → connectShop` enqueued a re-ingest the arq worker drained — new `ingest_runs`
  id **458** (after the uninstall), `complete` at **20/20**. (Note: reinstalling a
  `shopify app dev` app requires hitting `/auth` directly; the admin Preview/apps page does not
  re-trigger OAuth for an uninstalled embedded app.)
- Ingest coverage: **20 of 20** on both the pre-uninstall run (441) and the post-reinstall run
  (458) — no silent cap or filter.

## Config left local (not committed), per approved scope

- `app/shopify.app.toml` — personal ngrok `application_url` + `automatically_update_urls_on_dev=false`.
- `app/shopify.web.toml` — the dev port pin (3001). Left local; commit only if the team adopts
  `--tunnel-url=…:3000` as the shared dev path.

## Deferred / open going into Phase 3

- **Ingest 37 → 20 (historical).** ingest_run 101 (07-15) saw 37/37; runs 112, 441, and 458 saw
  20/20. Confirm the current seed is intentionally 20 and nothing is dropped between runs.
- Prior items still open: verbatim-snippet grounding; the webhook-refresh pool-coupling deadlock
  residual; the ingest prune path for deleted store products; the "Quixly" rename before Phase 5.
