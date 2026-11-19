# DarGlobal & Wasalt AI Property Chatbot

An AI assistant that answers questions about **DarGlobal** (projects, partners, press, investor information) and **Wasalt** (Saudi property listings, cities and districts), grounded in data scraped from both public websites. Every answer cites its source pages, and the bot says *"I don't have that information"* rather than guessing.

**Live demo:** https://darglobal-wasalt-ai-chatbot.onrender.com

## For reviewers

- **Try it:** open the live demo, with nothing to install. It runs on Render's free plan, which sleeps after 15 minutes without visitors, so **the first page load can take about a minute**; after that it responds normally.
- **Run it yourself:** create a free key at [openrouter.ai](https://openrouter.ai), put it in `.env` (see `.env.example`), then run `docker compose up --build` and open <http://localhost:7860>.
- **Check the build:** the [Actions tab](../../actions) shows the automated tests and Docker build for every push.
- **Good questions to try:** "What is DarGlobal's GDV?", "Who does DarGlobal partner with?", "Which districts in North Riyadh have villas for sale?", "What is the price of Wasalt listing #5907931?" (the price isn't in the data, so the bot says so), and a follow-up such as "Tell me about The Astera" then "When is handover?".

| Assessment step | How it is met |
|---|---|
| 1. Scrape public data | `app/scraper.py`: headless-browser crawl of 163 DarGlobal pages plus parsing of Wasalt's public XML sitemaps (62,658 listing URLs and 8,634 category pages) into 1,804 searchable records |
| 2. AI chatbot on the data | Retrieval-augmented generation: BM25 search over the scraped records, then an LLM answers only from the retrieved text, with source links |
| 3. Free OpenRouter model | The largest available free (`:free`) instruct model is picked automatically at startup, with a second free model as backup |
| 4. Docker | `Dockerfile` (non-root, health check), `docker-compose.yml`, separate scraper image |
| 5–6. Deployed with a working URL | Docker web service on Render, auto-deployed from this repository (link above) |

## Example questions

- What is DarGlobal's current GDV? *(US$23 billion, HY 2026, cited)*
- Who does DarGlobal partner with? *(all 11 brand partners, linked)*
- Tell me about The Astera in Ras Al Khaimah.
- Find 3-bedroom apartments for rent on Wasalt.
- Which districts in North Riyadh have villas for sale?
- Who is the CEO of Tesla? *(refuses: not in the data)*

## Architecture

```
                   ┌──────────────────────── offline (scraper image) ───────────────────────┐
 darglobal.co.uk ──► Playwright (headless Chromium, 4 pages in parallel)                     │
   sitemap.xml      → clean text → remove repeated boilerplate → ~800-char chunks           │
                                                                         ├─► app/data/scraped_data.json
 wasalt.sa/robots.txt → public XML sitemaps on cdn.wasalt.sa                                 │   (1,804 records,
   → parse listing & category URLs → per-type / per-city / per-bedroom summary records ──────┘    each with source URL)

                   ┌──────────────────────────── online (app image) ────────────────────────┐
 Browser ── POST /api/chat ──► FastAPI ──► BM25 search (top 8 chunks, max 2 per page)        │
                                  │          + abbreviation expansion (GDV, KSA, RAK, 3BR…)  │
                                  │          + partner directory for partner/brand questions │
                                  ├──► OpenRouter free model (primary → retry → fallback)    │
                                  │        strict prompt: answer only from context, cite URLs│
                                  └──► if no model is available: clearly labelled            │
                                       "AI unavailable" answer built from the top sources    │
                   └─────────────────────────────────────────────────────────────────────────┘
```

**Key design decisions**

- **BM25, not embeddings.** The corpus is small (1,804 chunks) and full of exact names (DG1, Astera, district names, listing IDs) where keyword matching is precise. It also needs no embedding API, GPU or extra model download, so the image stays small and starts in seconds. Abbreviation expansion covers the common vocabulary gaps (GDV ↔ gross development value, 3BR ↔ 3 bedrooms).
- **Grounding.** The system prompt allows only facts from the retrieved context, requires a source link for each statement, prefers the most recent figure when sources disagree, and requires a refusal when the answer isn't in the data.
- **Citation validation.** The server checks every link in the model's answer against the retrieved sources. Links that don't come from the retrieved pages are removed (the text is kept), and an answer with no valid citation gets the consulted source pages appended, so every factual answer stays traceable.
- **Follow-up questions.** The page sends the last 6 turns of the conversation. Short follow-ups ("and in Jeddah?", "when is handover?") are searched together with the previous question, and the model sees the conversation, while facts must still come from the retrieved context.
- **Model choice.** At startup the app reads OpenRouter's live model list, scores the free models (parameter count, instruct tuning, context length) and picks the strongest as primary and the next as fallback. Free models change often; this avoids hard-coding a model that later disappears.
- **Reliability.** Each question tries the primary model, retries it once (unless rate-limited), then tries the fallback model. If all fail, the user still gets a clearly labelled answer listing the most relevant scraped sources, rather than an error.
- **Daily free-model quota.** OpenRouter allows 50 free-model requests per day on accounts without credits, or 1,000 per day once $10 of credits have been added (free models still cost nothing). When OpenRouter reports the daily quota is used up, the app stops calling it for 30 minutes and tells visitors plainly that the AI quota has been reached, while still showing the relevant sources.
- **Cost and abuse protection.** Only free models can be used (a model id sent by the browser is ignored unless it is in the free list). Identical questions are cached for 6 hours to save free-tier quota, and each visitor is limited to 15 questions per minute.

## Data

The data was scraped on **18 November 2026**. Full details on method, coverage, limitations and terms of use are in [DATA_PROVENANCE.md](DATA_PROVENANCE.md).

| Source | Method | Result |
|---|---|---|
| DarGlobal | Headless Chromium (Playwright) over English pages from `sitemap.xml`: projects, partners, one-of-one, press, insights, about, FAQ, investor | 163 pages → 835 chunks |
| Wasalt | Public XML sitemaps listed in `robots.txt` (listing pages themselves are behind a Cloudflare challenge) | 62,658 listing URLs + 8,634 category URLs → 969 summary records (per type, per bedroom count, per city and per city/type/district) |

**Known limitations:** Wasalt prices and descriptions are not included (they are only on the protected listing pages); Wasalt listing URLs don't include the city, so individual listings are tagged "Saudi Arabia" while city and district questions are answered from category pages; DarGlobal blog posts and Arabic pages were not scraped; the data is a snapshot and is refreshed by re-running the scraper.

## Run locally

Requirements: Docker Desktop and an OpenRouter API key (free at openrouter.ai).

```bash
cp .env.example .env          # then put your key in .env: OPENROUTER_API_KEY=sk-or-v1-...
docker compose up --build -d
```

Open <http://localhost:7860>. Check status with `curl http://localhost:7860/api/health`.

Without Docker (Python 3.11+):

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 7860
```

## Refresh the scraped data

```bash
docker compose --profile scraper run --rm scraper
```

This takes about 4 minutes and **replaces** `app/data/scraped_data.json`. Restart the app afterwards.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -v
```

43 automated tests cover search quality (GDV, partners, bedrooms, projects, districts, listing IDs), the API (success, cache, fallback model, graceful degradation, daily-quota handling, input validation, rate limiting), citation validation, follow-up conversations, security (paid-model blocking, spoofed proxy headers) and the scraper's URL parsers. OpenRouter is mocked, so no key or network is needed. GitHub Actions (`.github/workflows/ci.yml`) runs the tests, builds the Docker image and smoke-tests the container on every push.

## Deploy

**Render** (live deployment). `render.yaml` is a ready Blueprint: in Render choose **New → Blueprint**, connect this GitHub repository and enter `OPENROUTER_API_KEY` when asked. Alternatively create a **Web Service** with runtime **Docker** from this repository and add `OPENROUTER_API_KEY` under **Environment**. Every push to `main` redeploys automatically. The free plan sleeps after 15 minutes without traffic; the first request then takes about a minute.

**Any Docker host** works the same way: build the `Dockerfile`, set `OPENROUTER_API_KEY`, and expose the port given in `PORT` (default 7860).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | none (required for AI answers) | OpenRouter key, injected at runtime and never stored in the image |
| `OPENROUTER_MODEL` | auto | Force a specific free model |
| `OPENROUTER_FALLBACK_MODEL` | auto | Force a specific fallback model |
| `RATE_LIMIT_PER_MINUTE` | 15 | Questions per visitor per minute |
| `TRUSTED_PROXY_HOPS` | 1 | Number of hosting proxies in front of the app; the visitor IP is read from that position at the right of `X-Forwarded-For` (0 = ignore the header) |
| `ANSWER_CACHE_TTL_SECONDS` | 21600 | How long identical answers are reused |
| `PORT` | 7860 | HTTP port |

## API

| Endpoint | Description |
|---|---|
| `GET /` | Chat interface |
| `POST /api/chat` | `{"message": "...", "history": [{"role": "user"/"assistant", "content": "..."}]}` → `{reply, model_used, citations, retrieved_chunks, ...}` |
| `GET /api/health` | Status, record count, selected models, whether a key is configured |
| `GET /api/knowledge` | The full scraped knowledge base |

## Project structure

```
app/main.py              FastAPI app: retrieval, prompt, model selection, fallback, cache, rate limit
app/scraper.py           DarGlobal (Playwright) and Wasalt (sitemap) scraper
app/static/index.html    Chat UI (Markdown rendering via vendored marked + DOMPurify)
app/data/scraped_data.json  Scraped knowledge base (1,804 records)
tests/test_app.py        Automated tests
Dockerfile               App image (non-root, health check)
Dockerfile.scraper       Scraper image (Playwright + Chromium)
docker-compose.yml       App service + optional scraper profile
render.yaml              Render Blueprint (alternative deployment)
DATA_PROVENANCE.md       Data method, coverage, limitations, terms of use
SECURITY.md              Secrets handling and protections
```

## Possible improvements

- Hybrid retrieval (BM25 + embeddings) and a re-ranker for paraphrased questions.
- Arabic support: scrape the Arabic pages and add Arabic tokenisation.
- Scheduled re-scraping (for example a nightly GitHub Action) with change detection.
- Wasalt prices and listing details through an authorised Wasalt API or data feed.
- Persistent conversation sessions (currently the page keeps the conversation and sends the last 6 turns).
