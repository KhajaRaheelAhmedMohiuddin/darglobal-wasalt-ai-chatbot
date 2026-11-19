# Security

## Secrets

- The OpenRouter API key is read only from the `OPENROUTER_API_KEY` environment variable, which comes from a local `.env` file or the hosting platform's secret store (Render environment variables).
- `.env` is excluded by both `.gitignore` and `.dockerignore`, so it is never committed or copied into a Docker image. `.env.example` contains only a placeholder.
- The key is never sent to the browser. All model calls are made by the server.

## Protections in the app

| Risk | Protection |
|---|---|
| Someone uses the key to call paid models | The server only uses free (`:free`) models from OpenRouter's live list; any other model id sent by the browser is ignored and logged |
| Free-tier quota exhausted by repeated questions | Identical questions are answered from a 6-hour cache |
| Flooding / abuse | 15 questions per minute per visitor (HTTP 429 beyond that). The visitor IP is taken from the right-most, proxy-written entry of `X-Forwarded-For` (`TRUSTED_PROXY_HOPS`), so a client can't bypass the limit by sending a fake header |
| Oversized input | Questions are limited to 1,000 characters (HTTP 422 beyond that) |
| Script injection through model output | Answers are rendered with `marked` and sanitised with `DOMPurify`; links open in a new tab with `rel="noopener noreferrer"` |
| Prompt injection / off-topic use | The system prompt restricts answers to the retrieved scraped context and requires a refusal otherwise; conversation history is limited to user/assistant turns (no client-supplied system messages) |
| Invented sources | Every link in an answer is checked against the retrieved pages; unverifiable links are removed |
| Container breakout impact | The app container runs as an unprivileged user (UID 1000) |
| Model outage | Primary → retry → fallback model, then a clearly labelled sources-only answer instead of an error |

## Reporting

Please report security issues privately to the repository owner rather than opening a public issue.
