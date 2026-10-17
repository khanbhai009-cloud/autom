# Pinteresto v3 — Current System Design

> **Updated:** 17 October 2026
> **Purpose:** Describe the behavior implemented in the current codebase, including the regular pin pipeline and the Trend feature. This document is not a promise that every external service is configured; Google, AI, Firebase, image-hosting, and Make.com access depends on the environment secrets and account permissions.

---

## Contents

1. [System at a glance](#1-system-at-a-glance)
2. [Architecture](#2-architecture)
3. [Startup and scheduling](#3-startup-and-scheduling)
4. [Mastermind graph and strategy selection](#4-mastermind-graph-and-strategy-selection)
5. [Pin creation and publishing](#5-pin-creation-and-publishing)
6. [Trend feature](#6-trend-feature)
7. [Regular Vision Feeder](#7-regular-vision-feeder)
8. [Data storage](#8-data-storage)
9. [HTTP interface](#9-http-interface)
10. [Integrations and configuration](#10-integrations-and-configuration)
11. [Failures and fallbacks](#11-failures-and-fallbacks)
12. [Security and operational notes](#12-security-and-operational-notes)
13. [Code map and inactive paths](#13-code-map-and-inactive-paths)

---

## 1. System at a glance

Pinteresto is a Python web service that manages two Pinterest publishing accounts. A FastAPI dashboard provides status and manual controls. APScheduler starts the regular publishing cycles. Each cycle runs a LangGraph workflow that loads board and analytics context, asks the CMO to create a pin strategy, selects a board, and calls the publishing agent.

The current scheduled publishing path is **visual-pin focused**:

- The CMO supplies a style, image prompt, title, description, tags, alt text, niche, and aspect ratio.
- An image-generation service creates the image. ImgBB hosts it before it is sent to Pinterest.
- If blog publishing is enabled and Firebase is configured, an inline blog workflow may research products, write an article, and publish it to Firestore.
- Make.com receives the completed pin payload and performs the Pinterest-side posting.

The Trend feature adds a separate way to collect visual references and steer the CMO:

- Users add trends, keywords, and reference images from `/trends-ui`.
- Trend images are queued in trend-specific Google Drive folders.
- A separate background feeder analyzes those images and saves prompt material in dedicated Google Sheets tabs.
- The CMO uses a trend only when it is switched on and has at least one usable analyzed prompt. Otherwise it uses the existing regular CMO path.

The regular Vision Feeder remains separate. It processes images in the existing regular Drive input folder and adds styles to `Prompts_Master`.

### Two Pinterest accounts

| Internal account key | Account purpose | Main niche group |
|---|---|---|
| `account_1` | Home, decor, lifestyle | `home`, `kitchen`, `cozy`, `gadgets`, `organize` |
| `account_2` | Tech, workspace, devices | `tech`, `budget`, `phone`, `smarthome`, `wfh` |

The account key determines which account-specific strategy, webhook, board data, and trigger path are used.

---

## 2. Architecture

```text
Browser
  ├── Main dashboard: /
  └── Trend dashboard: /trends-ui
          │
          ▼
FastAPI application (main.py, port 5000)
  ├── Dashboard and operations endpoints
  ├── Trend API (/trends/*)
  └── Application lifespan
       ├── APScheduler: 20 daily pin jobs + watchdog
       ├── Regular Vision Feeder task
       └── Trend Feeder task

Scheduled/manual publishing cycle
  └── LangGraph Mastermind (mastermind/graph.py)
       ├── Local board loader
       ├── Analytics loader
       ├── CMO strategy (regular style or Trend strategy)
       ├── Board selector
       └── Agent executor
            └── agent.py publishing agent
                 ├── Image generation → ImgBB
                 ├── Optional inline blog → Firebase Firestore
                 └── Make.com webhook → Pinterest

Reference-image flows
  ├── Regular Drive input folder → regular Vision Feeder
  │       → Gemini Vision → Prompts_Master + Vision_Tracker
  └── Trend Drive folders → Trend Feeder
          → Gemini Vision → Trend_Prompts

Google Sheets
  ├── Regular prompts, trackers, analytics, and approved-deal data
  └── Trend_Settings and Trend_Prompts
```

### Main boundaries

- **FastAPI** serves the dashboards and receives manual actions.
- **APScheduler** starts publishing jobs in the running application process.
- **LangGraph** coordinates the strategy and publishing steps.
- **Google Sheets** stores prompts, rotation trackers, analytics, and trend data.
- **Google Drive** stores regular and trend reference images.
- **Local JSON** stores board configuration used by the Mastermind loader.
- **Make.com** performs the final Pinterest posting integration.
- **Firebase Firestore** stores optional blog posts.
- **ImgBB** hosts generated images so the Pinterest webhook receives a public image URL.

---

## 3. Startup and scheduling

### Application startup

The FastAPI lifespan in `main.py` performs these operations:

1. Attempts to initialize the regular Google Sheets tabs through `sheets.init_sheets()`.
2. Attempts to initialize `Trend_Settings` and `Trend_Prompts`.
3. Registers a midnight daily scheduling job and creates the next batch of pin jobs.
4. Starts APScheduler and a watchdog that checks for missing pending pin jobs every 30 minutes.
5. Starts the regular Vision Feeder loop.
6. Starts the Trend Feeder loop. The Trend Feeder waits 60 seconds before its first pass.

Sheet initialization errors are logged as warnings so the web application can still start. Features that need Sheets or Drive will then return errors or fall back according to their own code paths.

### Daily pin schedule

`schedule_daily_pins()` registers 20 one-time jobs:

- 10 jobs for `account_1` and 10 for `account_2`.
- Account jobs are interleaved.
- The first job is scheduled 5–15 minutes after scheduling.
- The remaining jobs are spaced by randomized gaps of 60–72 minutes.
- The schedule is rebuilt by a midnight cron job and can also be rebuilt by the watchdog if it finds no pending `pin_` jobs.
- The scheduler is configured with the `America/New_York` timezone.

Each job calls `mastermind_scheduled_job()` with an account-specific trigger string. The trigger lets the graph run only the requested account. Manual “run both” actions use a trigger that runs both accounts sequentially.

> **Timing note:** Code comments mention an India-time posting window, but the current scheduler uses `America/New_York` and creates jobs relative to the time `schedule_daily_pins()` runs. There is no explicit India 3 PM–11 AM window enforced by the scheduling function.

### Concurrency and persistence

- `mastermind_scheduled_job()` skips a cycle if another Mastermind cycle is already marked as running.
- APScheduler uses its in-process job store; scheduled jobs are not persisted to an external database.
- A process restart therefore reconstructs a fresh schedule rather than restoring the previous job list.
- The main application state and several counters are also in memory.

---

## 4. Mastermind graph and strategy selection

The active graph is assembled in `mastermind/graph.py`:

```text
START
  → firebase_loader
  → data_intelligence
  → cmo_mastermind
  → board_selector
  → agent_executor
  → END
```

### Node 0 — Board loader

Despite the historical `node_firebase.py` filename, the current loader reads local board data through `tools.local_boards` for both accounts. It puts the board records into the graph state. It does not load Pinterest boards from Firestore in this path.

### Node 1 — Data intelligence

`mastermind/node_data.py` reads up to seven days of analytics independently for each account:

- `account_1` → `Analytics_Log`
- `account_2` → `Analytics_logs2`

If a tab returns no rows or cannot be read, the node supplies a zero-valued “stagnant” analytics profile and marks the cycle as having used a fallback. The graph continues.

### Node 2 — CMO strategy

`mastermind/node_cmo.py` prepares a separate strategy for each account that is active for the current trigger.

The CMO first tries the Trend path. If no usable Trend strategy is returned—or the Trend path raises an error—it continues through the regular style-rotation path.

#### Regular style path

1. Read and advance the account’s style rotation using `Style_Tracker`.
2. Load the corresponding prompt rows from `Prompts_Master`.
3. Rotate between prompt rows for the selected style using `Prompt_Tracker`.
4. Use the prompt row’s label, description, image prompt, niche affinity, and tags as the creative source.
5. Ask the configured CMO LLM to create the pin copy and expand the image prompt.
6. Validate the response and attach the standard strategy fields.

The normal strategy includes the pin type, visual style, niche, vibe, title, description, tags, alt text, visual prompt, and aspect ratio. The CMO also supplies board-matching keywords when available.

#### Trend strategy path

See [Trend strategy selection](#trend-strategy-selection) for the full decision process. The regular style and prompt trackers are not advanced when a Trend strategy is successfully returned.

### Node 3 — Board selector

`mastermind/node_board_selector.py` extracts matching signals from the strategy—board keywords, tags, niche, visual style, and description—and asks `tools.board_selector` to choose a board for the active account.

The selected `board_id` and `board_name` are inserted into that account’s strategy. If matching fails, the strategy is retained with no selected board; the webhook integration can use its configured niche/default behavior.

### Node 4 — Agent executor

The executor checks the trigger and calls `run_agent()` for the required account or accounts. If both accounts are requested, they run one after the other. It records each account’s publish status and the most recent posted image URL in the graph state.

### Graph state

The graph state carries the cycle trigger, per-account analytics, board data, Trend keyword data, per-account CMO strategies, per-account publish results, fallback status, and optional blog fields. Nodes return updates to this shared state rather than creating independent scheduled pipelines.

---

## 5. Pin creation and publishing

The scheduled CMO path produces visual pins (`VIRAL_PIN`). `agent.py` hosts the execution agent and the `publish_next_pin()` tool that performs the actual image and posting work.

```text
Account trigger + CMO strategy
  → run_agent()
  → publish_next_pin()
  → generate_pin_image()
  → upload generated bytes to ImgBB
  → optional inline blog workflow
  → Make.com webhook for the selected Pinterest account
  → success/failure returned to the graph and dashboard state
```

### Publishing steps

1. **Read the CMO strategy.** The publishing tool uses the strategy passed into the agent: visual prompt, ratio, title, description, tags, alt text, niche, and selected board.
2. **Generate the image.** `tools/image_creator.py` adds randomized camera angle, lighting, composition, and seasonal modifiers to the prompt, then tries the configured image providers in order.
3. **Host the image.** Successful image bytes must be uploaded to ImgBB. If no image URL is produced, the pin is not posted.
4. **Optionally write a blog.** If `BLOG_ENABLED` is not `false` and Firebase credentials are present, `agent.py` runs the inline blog chain before the pin is sent:

   ```text
   node_blog_trigger
     → node_product_researcher
     → node_blog_writer
     → node_firebase_publisher
   ```

   The researcher can use product-search and affiliate-link integrations to support the article. A blog failure is caught; it does not stop the Pinterest pin. If the blog is skipped or fails, the pin is sent without a blog destination URL.

5. **Send to Make.com.** The selected account determines which webhook is used. The payload includes the hosted image URL, copy, tags, alt text, niche, board information, and optional blog URL.
6. **Return the result.** The agent reports success only when the webhook reports success. The current visual-pin publishing path does not mark a product row as posted.

### Product-related code and the active scheduled path

Product stock analysis and product fetching functions remain in `agent.py`, and product data can be managed through dashboard operations. They are not registered as tools in the active execution agent. Scheduled pins therefore do not currently require a pending product row or use an affiliate product image as an image-generation fallback. Product research is used inside the optional blog workflow, not as the scheduled pin’s primary publishing path.

---

## 6. Trend feature

### User flow

The mobile-friendly Trend dashboard is served at `/trends-ui`. It has three main areas:

1. **Trends:** Add a trend, edit its keywords, and switch it on or off.
2. **Queue images:** Choose `Regular` or a trend and upload reference images.
3. **Feeder status:** See how many Trend prompts have been processed today and the daily limit.

The dashboard uploads files in batches of up to five. The API accepts image files up to 15 MB each.

### Add and manage a trend

When a user adds a trend:

1. The API checks that the name is non-empty, is not `Regular`, and does not already exist (case-insensitive).
2. The app tries to create a Drive folder under `TREND_DRIVE_PARENT_ID`. It prefers user OAuth; if that fails or is unavailable, it tries the service-account Drive client.
3. The trend is written to `Trend_Settings` with `active = FALSE`, the folder ID (if created), and the comma-separated keywords.
4. If folder creation fails, the trend can still be saved, but it will not accept Trend uploads until a Drive folder is available.

The switch is a manual on/off control. There are no start-date or end-date rules.

### Image upload

The upload API is `POST /trends/upload`:

- `target=Regular` sends images to the existing regular Vision Feeder input folder.
- A trend target sends images to that trend’s Drive folder.
- Upload uses Google user OAuth credentials. If those credentials are missing, the API returns an explicit configuration error.
- The upload endpoint stores the file in Drive and returns the number queued and any per-file failures. It does not call Gemini Vision during upload.

This keeps uploads quick and separates ingestion from analysis.

### Trend Feeder

`tools/trend_feeder.py` runs as an asyncio background task independent of scheduled posting:

1. Wait 60 seconds after application startup.
2. Read the trend settings and visit configured trend folders, with active trends ordered first.
3. List image files from Drive using the service-account Drive client.
4. Compare Drive file IDs against `Trend_Prompts.source_file` to avoid reprocessing already-saved images.
5. Download each new image temporarily and reuse `tools.visions_ai.analyze_image()` to extract visual prompt data.
6. Append a row to `Trend_Prompts`; the original Drive image is left in place.
7. Wait 30 seconds after a successful image, or 60 seconds after a processing error.
8. Run again after 30 seconds when it processed images, after five minutes when there was no work, or after one hour when the daily cap has been reached.

The Trend Feeder limit is **25 analyzed images per calendar day**. The regular Vision Feeder has its own separate limit of **10 images per day**.

> **Toggle behavior:** The feeder processes configured trends even when their `active` switch is off; it only prioritizes active trends first. The switch controls whether the CMO may use a Trend for a pin, not whether its reference images are analyzed.

### Trend strategy selection

For each account, the Trend CMO:

1. Reads active trends from `Trend_Settings`.
2. Looks for analyzed prompt rows for each active trend.
3. Prefers prompt rows whose `account` matches the publishing account; if none match, it may use any prompt row for that trend.
4. Rotates between active trends and their prompt rows.
5. Builds the visual prompt from the analyzed row and selects keywords supplied by the user.
6. Tries the CMO LLMs for the title, description, and copy. If both calls fail, it can build a basic strategy directly from the analyzed row.
7. Enforces trend keyword usage in the title/description and adds CamelCase trend tags and board keywords.

If no active trend has a usable prompt row, `call_trend_cmo()` returns no strategy and the regular CMO style flow runs. If Trend handling raises an error, `node_cmo.py` logs a warning and also continues with the regular flow.

### Trend data tables

`sheets/trends.py` creates and manages two dedicated tabs. It does not write Trend prompts into the regular `Prompts_Master` tab.

**`Trend_Settings`**

| Column | Meaning |
|---|---|
| `trend` | User-facing trend name |
| `active` | Manual `TRUE` / `FALSE` switch |
| `drive_folder_id` | Drive folder receiving that trend’s reference images |
| `keywords` | Comma-separated Pinterest keywords supplied by the user |

**`Trend_Prompts`**

| Column | Meaning |
|---|---|
| `trend` | Trend associated with the reference image |
| `style_key` | Visual style identifier from image analysis |
| `account` | Account classification from image analysis |
| `label` | Human-readable visual label |
| `description` | Extracted aesthetic description |
| `t2i_base` | Base text-to-image prompt |
| `niche_affinity` | Suggested niche(s) |
| `source_file` | Google Drive file ID used for duplicate detection |
| `added_on` | Date the prompt row was saved; used for the daily limit |

The Trend prompt store has a 60-second in-process cache. Settings changes and new prompt writes invalidate the relevant cache.

---

## 7. Regular Vision Feeder

The regular Vision Feeder is implemented in `tools/visions_ai.py` and runs alongside the Trend Feeder:

```text
Regular Drive input folder
  → list image files
  → skip/track processed files using Vision_Tracker
  → download image
  → Gemini Vision analysis
  → append style data to Prompts_Master
  → log to Vision_Tracker
  → move image to the regular Processed folder
```

It processes up to **10 images per day**. It scans the existing configured input folder, updates in-memory dashboard statistics, and checks its stop flag between images. The loop sleeps between scans and sleeps for a day after the daily limit is reached.

`/api/vision/stop` and `/api/vision/start` set the stop flag in `tools.visions_ai`. The Trend Feeder also checks that same flag before processing, so these controls pause/resume both image-analysis paths.

The regular and Trend feeders share the image classification and Gemini analysis functions, but use separate Drive folders, storage tabs, tracking, and daily caps.

---

## 8. Data storage

### Google Sheets

The regular Sheets layer is in `sheets/`; credentials come from `GOOGLE_CREDS_JSON`, and the spreadsheet is selected by `SPREADSHEET_ID`.

| Tab | Purpose |
|---|---|
| `Approved Deals` | Product/approved-deal rows used by product operations |
| `Prompts_Master` | Regular visual prompt library used by the regular CMO |
| `Style_Tracker` | Per-account regular style rotation |
| `Prompt_Tracker` | Prompt-row and Trend prompt rotation state |
| `Vision_Tracker` | Audit/tracking data for the regular Vision Feeder |
| `Analytics_Log` | Account 1 analytics input |
| `Analytics_logs2` | Account 2 analytics input |
| `Trend_Settings` | Trend names, switches, Drive folders, and user keywords |
| `Trend_Prompts` | Analyzed Trend reference-image prompts |

Regular Sheets access uses the shared Sheets layer and service-account credentials. The Trend data layer also uses the service account, with its own client and short-lived caches.

### Google Drive

- The regular Vision Feeder reads from its configured regular input folder and moves processed images to its configured regular Processed folder.
- The Trend Feeder reads trend-specific folders and leaves analyzed images there.
- Trend image uploads use user OAuth. The feeder uses the service account to list and download images.
- For the service account to read the Trend parent folder and its children, Drive sharing/permissions must be set up as described in `docs/trend-drive-setup.md`.

### Local board configuration

The Mastermind loader reads boards through `tools.local_boards` from the local board configuration. Board selection runs per account and adds the chosen board to the strategy before publishing.

### Firebase Firestore

Firebase is used by the optional blog publishing path. Blog posts are written by `tools/firebase_publisher.py`; the publishing agent catches blog errors so they do not block the pin.

### Process-local state and caches

Scheduler jobs, the main dashboard state, feeder status, and several caches live in the current process. They are not a shared/distributed state store. Google Sheets and Drive hold the persistent prompt/reference data.

---

## 9. HTTP interface

### Pages

| Route | Purpose |
|---|---|
| `GET /` | Main application dashboard |
| `GET /trends-ui` | Trend management and image upload dashboard |

### Trend API

| Method and route | Purpose |
|---|---|
| `GET /trends` | List trends and prompt counts |
| `GET /trends/feeder-status` | Return today’s analyzed prompt count and daily limit |
| `POST /trends/add` | Add a trend and attempt Drive folder creation |
| `POST /trends/toggle` | Set a trend’s manual active switch |
| `POST /trends/keywords` | Update user-provided keywords |
| `POST /trends/upload` | Upload regular or Trend reference images to Drive |

### Main dashboard and operations API

The main dashboard routes in `main.py` include:

- **Status and queues:** `/api/stats`, `/api/next-pins`, `/api/mastermind/stats`, `/api/products`, `/api/logs`, `/api/ping`
- **Mastermind controls:** `/api/mastermind/run`, `/api/mastermind/run-account1`, `/api/mastermind/run-account2`, `/api/mastermind/stop`
- **Account controls:** `/api/run-account1`, `/api/run-account2`, `/api/stop`
- **Blog controls:** `/api/blog/stats`, `/api/blog/run-account1`, `/api/blog/run-account2`, `/api/blog/run-both`
- **Vision controls:** `/api/vision/stats`, `/api/vision/stop`, `/api/vision/start`
- **Pin controls:** `/api/force-pin/account1`, `/api/force-pin/account2`, `/api/force-pin/both`
- **Product operations:** `/api/fetch-products`, `/api/fill-niches`
- **AI chat:** `/api/chat`, `/api/cmo-chat`
- **Boards:** `/api/boards/save`, `/api/boards/status`, `/api/firebase/upload-boards`, `/api/firebase/boards-status`

These controls call the application’s background jobs or data helpers. They do not create separate scheduled publishing pipelines.

---

## 10. Integrations and configuration

The application reads integration credentials from environment variables / Replit Secrets. Do not put secret values in this document or in source control.

| Configuration | Used for |
|---|---|
| `GOOGLE_CREDS_JSON`, `SPREADSHEET_ID` | Google Sheets and service-account Drive access |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` | Trend/regular image uploads and OAuth folder creation |
| `TREND_DRIVE_PARENT_ID` | Parent folder where new Trend folders are created |
| `GEMINI_API_KEY`, `GEMINI_API_KEY_2` | CMO/Vision and other Gemini-backed content paths |
| `GROQ_API_KEY`, `CEREBRAS_API_KEY` | Text-generation fallbacks and agent calls |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | Primary image generation |
| `HF_TOKEN` | Hugging Face image-generation fallback |
| `IMGBB_API_KEY` | Hosting generated images |
| `MAKE_WEBHOOK_URL`, `MAKE_WEBHOOK_URL_2` | Pinterest posting for the two accounts |
| `FIREBASE_CREDS_JSON`, `BLOG_BASE_URL`, `BLOG_ENABLED` | Optional blog publishing |
| `RAPIDAPI_KEY`, `RAPIDAPI_KEY2`, `APIFY_API_KEY`, `APIFY_ACTOR_ID` | Product research used by blog/product operations |
| `TAVILY_API_KEY` | Present in configuration for search-related code; not required for the core scheduled pin path |

### AI and publishing services

| Service | Current role |
|---|---|
| Gemini | Regular/Trend strategy generation and image-reference analysis |
| Groq | CMO and agent/text fallback paths |
| Cerebras | Shared LLM fallback and auxiliary text/vision paths |
| Cloudflare Workers AI | Primary text-to-image provider; current configured model is `@cf/black-forest-labs/flux-2-klein-4b` |
| Hugging Face | Secondary image-generation provider |
| Pollinations | Last-resort image-generation provider |
| ImgBB | Public hosting for generated pin images |
| Make.com | Receives account-specific posting payloads and handles Pinterest posting |
| Google Sheets / Drive | Prompt, analytics, tracker, and image-reference storage |
| Firebase Firestore | Optional blog-post storage |
| RapidAPI / Apify | Product discovery for blog/product operations |

The image generator supports `9:16` and `1:1` output dimensions. It validates generated image bytes, tries providers in order, and returns an ImgBB URL only after successful hosting.

---

## 11. Failures and fallbacks

| Area | Current behavior |
|---|---|
| Sheets initialization at startup | Logs a warning; the application can still start, but Sheets-backed features may fail |
| Analytics missing/unavailable | Supplies a zero-valued stagnant profile and continues the graph |
| Trend unavailable or errors | Logs a warning and falls back to the regular CMO strategy |
| Active Trend has no analyzed prompt | Skips Trend strategy and uses the regular CMO strategy |
| Trend CMO LLM calls fail | Builds a basic strategy from the analyzed Trend row; if Trend handling itself fails, regular flow is used |
| Regular CMO Gemini fails | Tries the configured Groq CMO fallback |
| Regular CMO fallback fails | Builds from prompt-sheet style data where possible; hardcoded account strategy is the outer emergency fallback |
| Board selection fails | Keeps the strategy and omits the selected board; downstream configured fallback behavior may apply |
| Cloudflare image generation fails | Tries Hugging Face, then Pollinations |
| All image providers fail | Returns no image URL; the pin is not posted |
| ImgBB upload fails | No hosted URL is returned; the pin is not posted |
| Blog generation/publishing fails | Logs the failure and continues to attempt the Pinterest post without a blog URL |
| Make.com webhook fails | The account’s pin operation returns a failure result |
| Trend image processing fails | Logs the error, waits, and may retry in a later pass; source-file deduplication is recorded when a prompt row is successfully saved |

---

## 12. Security and operational notes

- The current FastAPI application has no authentication middleware protecting the dashboard or API routes. The app also enables permissive CORS (`allow_origins=["*"]`). Treat the dashboard and its administrative endpoints as public if the app is reachable publicly; they should be protected before public use.
- Google OAuth upload credentials are separate from service-account read credentials. Configure both roles and Drive permissions using `docs/trend-drive-setup.md`.
- Google OAuth consent/testing configuration can affect refresh-token lifetime. Reauthorize or publish the consent screen as required by the Google project setup.
- The Trend feeder processes inactive trends too; turning a trend off prevents CMO selection but does not stop its image analysis, which still counts toward the daily Trend limit.
- The regular Vision Feeder and Trend Feeder have independent daily limits and run as separate background tasks.
- APScheduler and application state are process-local. A restart interrupts current in-memory jobs and counters; startup constructs a new daily schedule.
- Analytics are read from Sheets. The current Mastermind path does not itself fetch analytics directly from Pinterest.
- Secrets are expected to be configured outside the repository. Never add API keys, OAuth refresh tokens, or service-account JSON values to this document.

---

## 13. Code map and inactive paths

### Active runtime files

| File or directory | Responsibility |
|---|---|
| `main.py` | FastAPI app, dashboard endpoints, startup/shutdown, APScheduler, both feeder loops |
| `agent.py` | Execution agent, `publish_next_pin()`, optional inline blog trigger |
| `mastermind/graph.py` | Mastermind LangGraph wiring and graph entry point |
| `mastermind/node_firebase.py` | Current local-board loading node (historical filename) |
| `mastermind/node_data.py` | Per-account analytics loading |
| `mastermind/node_cmo.py` | Regular CMO strategy and Trend hook |
| `mastermind/trend_cmo.py` | Trend strategy selection and trend-keyword enforcement |
| `mastermind/node_board_selector.py` | Per-account board matching |
| `mastermind/node_blog_*.py` | Optional inline blog pipeline |
| `trend_routes.py` | Trend dashboard API |
| `static/trends.html` | Trend management UI |
| `sheets/` | Regular and Trend Google Sheets data access |
| `tools/trend_feeder.py` | Trend-folder background image analysis |
| `tools/visions_ai.py` | Regular Vision Feeder and shared Gemini image-analysis functions |
| `tools/drive_upload.py` | Google user-OAuth Drive uploads and folder creation |
| `tools/image_creator.py` | Image generation, fallback order, and ImgBB hosting |
| `tools/local_boards.py`, `tools/board_selector.py` | Board configuration and board selection |
| `tools/make_webhook.py` | Make.com Pinterest-posting request |

### Implemented but not part of the active scheduled path

- `pipeline/orchestrator.py` and its pipeline agents are an alternate orchestration path; the active scheduler uses the Mastermind graph.
- `mastermind/node_copy.py` and `mastermind/node_execute.py` are legacy modules not wired into the current Mastermind graph.
- `analyze_niche_stock()` and `fetch_aliexpress_products()` exist in `agent.py` but are not registered in the active agent tool list.
- Product sourcing and board-management endpoints may be used manually, but the scheduled visual-pin path does not run the legacy product-first pipeline.

---

*End of current system design.*
