---
title: Pinteresto
emoji: 🚀
colorFrom: blue
colorTo: purple
sdk: docker
pinned: false
---

<div align="center">

# 📌 PINTERESTO
### Autonomous Pinterest Marketing System — Multi-Agent AI

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-FF6B35?style=for-the-badge)](https://langchain-ai.github.io/langgraph)
[![Gemini](https://img.shields.io/badge/Gemini-4285F4?style=for-the-badge&logo=google)](https://deepmind.google/gemini)
[![Groq](https://img.shields.io/badge/Groq-F55036?style=for-the-badge&logo=groq)](https://groq.com)

> **20 Pinterest pins per day. 2 accounts. Zero human input.**
> An autonomous system that picks the next prompt from a 370+ prompt library,
> writes SEO copy, generates and quality-checks images, routes each pin to the
> right digital product, and posts — all on its own.

</div>

---

## ✨ What it does

- **Row rotation engine** — every pin is the *next row* of a 372-row Google Sheets
  prompt library (Account 1: 323 home-decor rows, Account 2: 49 tech rows).
  No repeats until the whole library has cycled.
- **CMO brain** — Gemini first, Groq `gpt-oss-120b` (thinking) as fallback,
  sheet-direct build as last resort. Writes Pinterest-SEO titles (keyword-first,
  clickbait patterns banned + an anti-repeat title guard), descriptions, tags.
- **Product Router (V4)** — at pin creation, decides which Gumroad product fits
  the pin (rules → LLM judge, threshold 0.70) and attaches its URL. No match →
  the pin posts clean. Every decision is logged to Sheets.
- **Image pipeline** — Cloudflare FLUX.2 klein (primary) → Replicate → HF →
  Pollinations. A *variety/mood engine* gives every image a fresh look; a
  *prompt diet* keeps prompts small enough for the model to follow; a
  *vision gate* (Gemini Vision) rejects hybrid/deformed images before posting.
- **Scheduler** — 20 pins/day, 60–72 min gaps, interleaved accounts, on
  America/New_York time (US audience). The daily plan persists in Sheets, so
  restarts/deploys resume instead of bursting.
- **Trends system** — Pinterest Trends keywords managed from the built-in UI /
  Android app; trend pins get their own boards automatically.

Full architecture: **[SYSTEM_DESIGN.MD](SYSTEM_DESIGN.MD)**

## 🧱 Stack

| Layer | Tech |
|---|---|
| Runtime | Python 3.11, FastAPI, LangGraph, APScheduler |
| Brains | Gemini (primary), Groq gpt-oss-120b thinking (fallback), Groq qwen (task execution only) |
| Database | **Google Sheets** (prompts, products, trackers, plans, logs) |
| Images | Cloudflare Workers AI FLUX.2 klein → Replicate schnell → Pollinations; ImgBB hosting |
| Publishing | Make.com webhooks → Pinterest |
| Products | Gumroad (The Cozy Canvas store) |
| Control | Pins Android app + built-in web UI (`/trends-ui`) |

## ⚙️ Configuration

All on/off switches live in ONE place: the top of `config.py`
(**"SAARE ON/OFF SWITCHES"** section) — router, LLM judge, mood engine,
vision gate, thresholds. Defaults are the correct production settings;
every switch can be overridden from Render environment variables.

## 🚀 Run

```bash
pip install -r requirements.txt
python main.py        # FastAPI on :7860 (Render uses the Dockerfile)
```

Secrets come from environment variables (`GOOGLE_CREDS_JSON`,
`SPREADSHEET_ID`, `GEMINI_API_KEY(S)`, `GROQ_API_KEY`, Cloudflare/ImgBB
keys, Make.com webhooks). Deployed on Render.

---

*Built by Finisher — AI automation developer. The system sells dreams first:
digital decor/garden kits on Gumroad, routed pin-by-pin by the Product Router.*
