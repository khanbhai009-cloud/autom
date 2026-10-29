# Pinteresto v4 — System Design

> **Updated:** 30 October 2026
> **Purpose:** Describe the behavior actually implemented in this codebase today.
> External services (Google, AI providers, Make.com) depend on environment
> secrets; where a secret is missing the affected layer degrades gracefully
> and the pipeline keeps running.

---

## Contents

1. [System at a glance](#1-system-at-a-glance)
2. [Architecture](#2-architecture)
3. [Startup and scheduling](#3-startup-and-scheduling)
4. [Mastermind graph](#4-mastermind-graph)
5. [Pin creation and publishing](#5-pin-creation-and-publishing)
6. [Product Router](#6-product-router)
7. [Image pipeline](#7-image-pipeline)
8. [Trend feature](#8-trend-feature)
9. [Data — Google Sheets](#9-data--google-sheets)
10. [Configuration](#10-configuration)
11. [Retired systems](#11-retired-systems)

---

## 1. System at a glance

Pinteresto is a fully autonomous Pinterest marketing system. It runs two
accounts — **Account 1: HomeDecor** ("The Cozy Canvas") and **Account 2:
Tech** — and publishes **20 pins/day** (10 per account) with zero human
input. Every pin is an original AI image with SEO copy; when a pin matches
one of the store's digital products, the pin carries that product's
Gumroad URL. Direction comes from **Pinterest Trends**, never from account
analytics (see §11).

## 2. Architecture

```
FastAPI (main.py, :7860)
├─ APScheduler (America/New_York) ── daily plan (persisted in Sheets)
├─ Mastermind graph (LangGraph)
│    firebase_loader → cmo_mastermind → board_selector
│    → product_router → agent_executor
├─ Product/Kits API (product_routes.py) ── Android app + web UI
├─ Trends API (trend_routes.py) + /trends-ui
└─ Background loops: vision feeder (Drive), trend feeder, pin watchdog
```

Publishing goes through **Make.com webhooks** (not the Pinterest API).
All state lives in **Google Sheets** — there is no other database.

## 3. Startup and scheduling

- On startup: sheet tabs auto-create (`sheets/setup.py`), the scheduler
  starts, background loops launch.
- `schedule_daily_pins()` builds **20 slots** with random **60–72 min**
  gaps, interleaved A1/A2, first pin 5–15 min after plan time.
- The plan is **persisted** in the `Schedule_State` sheet tab. A restart or
  deploy *resumes* the saved plan (past slots skipped, future slots at
  their planned times) — restarts no longer cause posting bursts. A fresh
  plan is forced only by the midnight (New York) cron.
- The watchdog (30 min) re-registers the day's plan if no pin jobs exist.
- A `mastermind_running` lock prevents overlapping cycles in-process.
- Scheduler timezone is **America/New_York by design** (US audience).
  IST appears only as a *display* conversion in API responses and logs.

## 4. Mastermind graph

| Node | Job |
|---|---|
| `firebase_loader` | Loads boards + active trend keywords for both accounts |
| `cmo_mastermind` | Strategy + copy for each account (below) |
| `board_selector` | Confirms the board (Groq-assisted, strict niche match) |
| `product_router` | Picks the pin's product destination (§6) |
| `agent_executor` | Runs the agent per account, respecting the trigger |

### CMO (per account)

1. **Trend path first** — if an active trend fits, trend CMO builds the pin.
2. **Row rotation** — otherwise the *next row* of `Prompts_Master` for the
   account becomes the pin (sheet order, wrap-around; pointer in
   `Prompt_Tracker` as `{account}__row`). The row's style_key is only an
   identifier; its label/description/tags/niche feed the copy.
3. **LLM chain** — Gemini → Groq `gpt-oss-120b` *thinking* (4096-token
   budget, `reasoning_effort=medium`; on a JSON-validation failure it
   retries once without strict JSON mode) → sheet-direct build.
   Only a *real* 429 escalates to the hardcoded fallback — error
   classification checks the status code, not substrings.
4. **Titles are SEO-first** — the prompt bans clickbait patterns; a
   *title guard* then replaces any clickbait or repeated (last 40) title
   with a keyword title built from the row's own label/tags/niche.
5. Qwen is **not** in the CMO — it is reserved for task execution
   (agent executor, `GROQ_MODEL`).

## 5. Pin creation and publishing

`agent.publish_next_pin`: image → destination → Make webhook post.

- Image: `tools/image_creator.generate_pin_image` (§7). If it returns
  `None` (all layers down, or the vision gate rejected twice), the pin is
  skipped — nothing posts.
- Destination: the Product Router's URL, or **none** (clean pin). There is
  no blog/link fallback (§11).
- The executor injects the router's destination into the strategy, so the
  copy and the link always belong to the same decision.

## 6. Product Router

`mastermind/product_router.py` + `node_product_router.py`:

- Products are rows of the **`Products`** sheet tab
  (`product_id | name | url | price | description | keywords | cta | active`,
  15-min cache). Adding a product = adding a row. A built-in 2-kit registry
  is only a fallback if Sheets is unreachable.
- Scoring: weighted keyword rules first (garden vs decor vs traffic/none).
  Clear winner ≥ threshold (0.70) → routed by rules. Ambiguous → **LLM
  judge** (Groq `gpt-oss-20b` → Gemini) reading the products' own sheet
  descriptions. Below threshold → **no link** (never forced).
- HomeDecor only; the Tech account never gets a product link.
- Every decision is appended to **`Product_Router_Log`**.
- The complete bundle is an upsell on Gumroad, never a router destination.
- Kits are managed from the Pins app / `GET|POST /api/kits`,
  `POST /api/kits/toggle`.

## 7. Image pipeline

`tools/image_creator.py`, layers in order: **Cloudflare FLUX.2 klein**
(1080×1920 native) → **Replicate FLUX schnell** (if token set) →
HuggingFace (legacy, fast-bails) → **Pollinations**. Result is hosted on
ImgBB.

Around generation:

- **Prompt diet** — the base prompt is trimmed to its first ~10 clauses /
  ~430 chars before the model sees it. Small models blend attributes of
  nearby nouns; object-dense prompts produced hybrid furniture
  (bed+sofa). Diet keeps subject + hero furniture, cuts the crowd.
- **Variety/Mood engine v5** — 14 moods (lighting + story props + season),
  scene-aware, anti-repeat history. Applied at the same choke point, so
  CMO and sheet-fallback prompts get it equally.
- **Quality tail** — prompts are capped at 1400 chars with *tail-safe*
  truncation: the "4K… razor-sharp focus…" tail can never be cut off
  (an earlier 750-char end-truncation silently removed it and softened
  every image).
- **Vision gate** — after generation, a vision judge checks for hybrid
  furniture, melted geometry, chaotic rooms, distorted people. Fail →
  one retry with an even tighter diet prompt. Fail again → the pin is
  skipped. Judges form a **chain** — Gemini key 1 → Gemini key 2 (separate
  quota) → Groq Vision (llama-4-scout, a different provider, so a Gemini
  peak-hour outage doesn't blind the gate) — and the gate is **fail-open**
  only if *every* judge is down: an outage never blocks posting.
  Switch: `IMAGE_VISION_GATE`.

## 8. Trend feature

Trends are managed in `/trends-ui` or the Android app (name + Pinterest
keywords, ON/OFF). An ON trend feeds the CMO trend path; boards for new
trends are auto-created on the account and remembered in Sheets. A weekly
Trends *picker* (choosing what to chase next) is designed but not built.

## 9. Data — Google Sheets

One spreadsheet ("Pinterest automation") is the database:

| Tab | Holds |
|---|---|
| `Prompts_Master` | 372 prompt rows (the pin queue) |
| `Products` | Router's product registry |
| `Product_Router_Log` | Every router decision |
| `Style_Tracker` / `Prompt_Tracker` | Rotation counters + row pointer |
| `Schedule_State` | Today's persisted pin plan |
| `Trend_Settings` / `Trend_Prompts` | Trends |
| `Vision_Tracker` | Vision feeder progress |

## 10. Configuration

All on/off switches live in **one place**: the top of `config.py`
("SAARE ON/OFF SWITCHES") — `PRODUCT_ROUTER_ENABLED`, `ROUTER_LLM_ENABLED`,
`PRODUCT_ROUTER_THRESHOLD`, `PRODUCT_ROUTER_UTM` (off = clean URLs),
`IMAGE_MOOD_ENGINE`, `IMAGE_VISION_GATE`. Defaults are production-correct;
Render env vars override. Secrets are env-only, never in code or docs.

## 11. Retired systems

- **Blog pipeline** — cancelled by the owner (29 Oct 2026). Pins sell
  digital products; no blog destination, no link guard. Legacy blog
  endpoints remain for manual use only.
- **Analytics node** — removed (30 Oct 2026). Pinterest analytics cannot
  be fed into this system, and the CMO never used the rows. Direction
  comes from Trends.
- **Firebase as database** — the system runs on Sheets. (Board metadata
  still loads through the legacy "firebase boards" module name.)
