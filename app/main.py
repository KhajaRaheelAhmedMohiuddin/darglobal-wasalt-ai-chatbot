import json
import logging
import os
import re
import time
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("darglobal_wasalt_chatbot")

DATA_PATH = Path(__file__).parent / "data" / "scraped_data.json"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# Dynamic free model state populated from https://openrouter.ai/api/v1/models at startup
AVAILABLE_FREE_MODELS: List[Dict[str, Any]] = []
RESOLVED_PRIMARY_MODEL: str = ""
RESOLVED_FALLBACK_MODEL: str = ""

ABBREVIATION_EXPANSIONS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bgdv\b", re.I), "gdv gross development value"),
    (re.compile(r"\bgross\s+development\s+value\b", re.I), "gross development value gdv"),
    (re.compile(r"\bksa\b", re.I), "ksa saudi arabia"),
    (re.compile(r"\bsaudi\s+arabia\b", re.I), "saudi arabia ksa"),
    (re.compile(r"\brak\b", re.I), "rak ras al khaimah"),
    (re.compile(r"\bras\s+al\s+khaimah\b", re.I), "ras al khaimah rak"),
    (re.compile(r"\b(\d+)\s*-?\s*br\b", re.I), r"\1 br \1 bedroom \1 bedrooms \1bedroom"),
    (re.compile(r"\b(\d+)[\s-]*bed(?:room)?s?\b", re.I), r"\1 bedroom bedrooms \1bedroom"),
    (re.compile(r"\bbr\b", re.I), "br bedroom bedrooms"),
    (re.compile(r"\bbedrooms?\b", re.I), "bedroom bedrooms br"),
    (re.compile(r"\bsq\.?\s*m\b|\bsqm\b|\bm2\b", re.I), "sqm square meters"),
    (re.compile(r"\bsquare\s+met(?:er|re)s?\b", re.I), "square meters sqm"),
    (re.compile(r"\buae\b", re.I), "uae united arab emirates"),
    (re.compile(r"\bunited\s+arab\s+emirates\b", re.I), "united arab emirates uae"),
    (re.compile(r"\buk\b", re.I), "uk united kingdom"),
    (re.compile(r"\bunited\s+kingdom\b", re.I), "united kingdom uk"),
    (re.compile(r"\blse\b", re.I), "lse london stock exchange"),
    (re.compile(r"\bjge\b", re.I), "jge jumeirah golf estates"),
    (re.compile(r"\broi\b", re.I), "roi return on investment"),
    (re.compile(r"\b(?:partner|partners|partnership|partnerships|partnered)\b", re.I), "partner partners partnership"),
    (re.compile(r"\b(?:brand|brands|branded)\b", re.I), "brand brands branded"),
    (re.compile(r"\b(?:collaboration|collaborations|collaborate|collaborates)\b", re.I), "collaboration partner partnership"),
    (re.compile(r"\broe\b", re.I), "roe return on ego"),
    (re.compile(r"\brol\b", re.I), "rol return on legacy"),
]


def expand_abbreviations(text: str) -> str:
    """Expands real estate abbreviations (GDV, KSA, RAK, BR, sqm, etc.) before BM25 tokenization."""
    expanded = text
    for pattern, replacement in ABBREVIATION_EXPANSIONS:
        expanded = pattern.sub(replacement, expanded)
    return expanded


def tokenize(text: str) -> List[str]:
    expanded = expand_abbreviations(text.lower())
    return [tok for tok in re.split(r"\W+", expanded) if len(tok) > 1]


def score_free_model(model_obj: Dict[str, Any]) -> Tuple[float, float, int]:
    """
    Scores an OpenRouter :free model so large instruct models rank at the top,
    while small models (<7B), guards, or random routers rank lower.
    """
    model_id = str(model_obj.get("id", "")).lower()
    name = str(model_obj.get("name", "")).lower()
    combined = f"{model_id} {name}"
    ctx_len = int(model_obj.get("context_length") or 0)

    if any(bad in combined for bad in ("guard", "moderation", "embed", "rerank", "audio")):
        return (-1000.0, 0.0, ctx_len)

    # Extract parameter count in billions (e.g., 70b, 72b, 32b, 27b, 405b)
    param_matches = re.findall(r"(\d+(?:\.\d+)?)\s*b\b", combined)
    param_b = max((float(x) for x in param_matches), default=10.0)

    score = param_b
    if any(k in combined for k in ("instruct", "-it", "chat")):
        score += 35.0
    if any(fam in combined for fam in ("llama-3", "qwen", "gemma", "mistral", "nemotron", "deepseek", "solar")):
        score += 20.0
    if param_b < 7.0:
        score -= 120.0
    score += min(ctx_len / 16384.0, 15.0)
    return (round(score, 2), param_b, ctx_len)


async def refresh_openrouter_free_models() -> None:
    """Loads active :free models from https://openrouter.ai/api/v1/models and selects primary + fallback large instruct models."""
    global AVAILABLE_FREE_MODELS, RESOLVED_PRIMARY_MODEL, RESOLVED_FALLBACK_MODEL

    env_primary = os.getenv("OPENROUTER_MODEL", "").strip()
    env_fallback = os.getenv("OPENROUTER_FALLBACK_MODEL", "").strip()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            payload = resp.json()
            all_models = payload.get("data") or []
    except Exception as exc:
        logger.warning("Could not fetch live models from %s: %s", OPENROUTER_MODELS_URL, exc)
        all_models = []

    free_candidates: List[Dict[str, Any]] = []
    for m in all_models:
        mid = str(m.get("id", ""))
        if not mid.endswith(":free"):
            continue
        sc, param_b, ctx_len = score_free_model(m)
        if sc < -500:
            continue
        free_candidates.append({
            "id": mid,
            "name": m.get("name", mid),
            "context_length": ctx_len,
            "param_b": param_b,
            "score": sc,
        })

    free_candidates.sort(key=lambda x: (x["score"], x["param_b"], x["context_length"]), reverse=True)
    AVAILABLE_FREE_MODELS = free_candidates

    live_ids = [m["id"] for m in free_candidates]
    if env_primary and env_primary != "openrouter/free" and (not live_ids or env_primary in live_ids):
        RESOLVED_PRIMARY_MODEL = env_primary
    elif live_ids:
        RESOLVED_PRIMARY_MODEL = live_ids[0]
    else:
        RESOLVED_PRIMARY_MODEL = env_primary or "openrouter/free"

    if env_fallback and env_fallback != RESOLVED_PRIMARY_MODEL and (not live_ids or env_fallback in live_ids):
        RESOLVED_FALLBACK_MODEL = env_fallback
    else:
        secondary = [mid for mid in live_ids if mid != RESOLVED_PRIMARY_MODEL]
        RESOLVED_FALLBACK_MODEL = secondary[0] if secondary else "openrouter/free"

    logger.info(
        "Discovered %d active :free models from OpenRouter. Primary='%s', Fallback='%s'",
        len(AVAILABLE_FREE_MODELS),
        RESOLVED_PRIMARY_MODEL,
        RESOLVED_FALLBACK_MODEL,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await refresh_openrouter_free_models()
    yield


app = FastAPI(
    title="DarGlobal & Wasalt AI Property Intelligence Chatbot",
    description="BM25 RAG Chatbot grounded in scraped DarGlobal & Wasalt data and OpenRouter Free Models",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def load_kb() -> List[Dict[str, Any]]:
    if not DATA_PATH.exists():
        return []
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.error("Failed to read %s: %s", DATA_PATH, exc)
        return []


def url_path_words(url: str) -> str:
    """Turns a URL path like /partners/aston-martin into searchable words ("partners aston martin")."""
    path = re.sub(r"^https?://[^/]+", "", url or "")
    return re.sub(r"[/_\-.]+", " ", path)


URL_MATCH_BOOST = 1.3
SOURCE_MATCH_BOOST = 1.3
URL_BOOST_STOPWORDS = {
    "darglobal", "dar", "global", "wasalt", "en", "co", "uk", "sa", "www", "https", "http",
    "the", "and", "for", "in", "of", "with", "to", "is", "what", "which", "who", "does", "do",
}


def build_partner_directory(kb: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Builds one summary record listing every DarGlobal brand partner page that was scraped
    (https://darglobal.co.uk/partners/<brand>), so broad "who are your partners" questions
    retrieve the full list instead of a single partner page."""
    partners: Dict[str, str] = {}
    for item in kb:
        m = re.match(r"^https?://(?:www\.)?darglobal\.co\.uk/partners/([^/?#]+)$", item.get("url", ""))
        if m and m.group(1) not in partners:
            name = m.group(1).replace("-", " ").title().replace("The Trump", "The Trump")
            partners[name] = item["url"]
    if not partners:
        return []
    listing = "; ".join(f"{name} ({url})" for name, url in sorted(partners.items()))
    return [{
        "id": "darglobal-partner-directory",
        "source": "DarGlobal",
        "category": "partners",
        "location": "Global",
        "title": "DarGlobal Brand Partners Directory (all partner pages)",
        "text": (
            f"DarGlobal lists {len(partners)} brand partners and collaborations on its partners pages: "
            f"{listing}. Ask about any partner for details of the branded projects."
        ),
        "url": "https://darglobal.co.uk/partners",
    }]


PARTNER_QUERY_RE = re.compile(r"\b(partner|partners|partnership|partnerships|partnered|brand|brands|collaborat\w*)\b", re.I)


_BM25_CACHE: Dict[str, Any] = {"mtime": None, "kb": [], "bm25": None}


def get_bm25_index() -> Tuple[List[Dict[str, Any]], Optional[BM25Okapi]]:
    """Builds the BM25 index once and rebuilds only when scraped_data.json changes."""
    mtime = DATA_PATH.stat().st_mtime if DATA_PATH.exists() else None
    if _BM25_CACHE["bm25"] is not None and _BM25_CACHE["mtime"] == mtime:
        return _BM25_CACHE["kb"], _BM25_CACHE["bm25"]
    kb = load_kb()
    if not kb:
        return [], None
    kb = kb + build_partner_directory(kb)
    corpus_tokens = [
        tokenize(
            f"{item.get('title', '')} {item.get('source', '')} "
            f"{item.get('category', '')} {item.get('location', '')} "
            f"{url_path_words(item.get('url', ''))} "
            f"{item.get('text', item.get('summary', ''))}"
        )
        for item in kb
    ]
    bm25 = BM25Okapi(corpus_tokens)
    _BM25_CACHE.update({"mtime": mtime, "kb": kb, "bm25": bm25})
    return kb, bm25


def retrieve_bm25_chunks(query: str, top_k: int = 8, max_per_url: int = 2) -> List[Dict[str, Any]]:
    """Expands abbreviations, ranks chunks with BM25Okapi and returns the top chunks,
    keeping at most `max_per_url` chunks per page so answers draw on several sources."""
    kb, bm25 = get_bm25_index()
    if not kb or bm25 is None:
        return []
    query_tokens = tokenize(query)
    if not query_tokens:
        return kb[:top_k]

    scores = list(bm25.get_scores(query_tokens))
    # Boost pages whose URL path contains a query word (e.g. "partners" -> /partners/missoni)
    url_query_tokens = set(query_tokens) - URL_BOOST_STOPWORDS
    if url_query_tokens:
        for idx, item in enumerate(kb):
            path_tokens = set(tokenize(url_path_words(item.get("url", ""))))
            if path_tokens & url_query_tokens:
                scores[idx] *= URL_MATCH_BOOST
    # Questions naming only one website: prefer that website's records
    q_lower = query.lower()
    mentions_wasalt = "wasalt" in q_lower
    mentions_darglobal = "darglobal" in q_lower or "dar global" in q_lower
    if mentions_wasalt != mentions_darglobal:
        preferred = "Wasalt" if mentions_wasalt else "DarGlobal"
        for idx, item in enumerate(kb):
            if item.get("source") == preferred:
                scores[idx] *= SOURCE_MATCH_BOOST

    # Exact identifiers (listing IDs, reference numbers): records containing them go first
    id_tokens = [t for t in query_tokens if t.isdigit() and len(t) >= 4]
    if id_tokens:
        top = max(scores) if scores else 0.0
        for idx, item in enumerate(kb):
            text = f"{item.get('text', '')} {item.get('url', '')}"
            if any(re.search(rf"(?<!\d){t}(?!\d)", text) for t in id_tokens):
                scores[idx] += top + 1.0

    ranked_indices = sorted(range(len(kb)), key=lambda idx: scores[idx], reverse=True)

    results: List[Dict[str, Any]] = []
    per_url: Dict[str, int] = {}
    # Broad partner/brand questions: always include the partner directory record first
    if PARTNER_QUERY_RE.search(query):
        for idx, item in enumerate(kb):
            if item.get("id") == "darglobal-partner-directory":
                chunk = dict(item)
                chunk["bm25_score"] = round(float(scores[idx]), 4)
                results.append(chunk)
                per_url[item["url"]] = 1
                break
    for idx in ranked_indices:
        if kb[idx].get("id") == "darglobal-partner-directory" and results and results[0].get("id") == "darglobal-partner-directory":
            continue
        if len(results) >= top_k or scores[idx] <= 0:
            break
        url = kb[idx].get("url", "")
        if per_url.get(url, 0) >= max_per_url:
            continue
        per_url[url] = per_url.get(url, 0) + 1
        chunk = dict(kb[idx])
        chunk["bm25_score"] = round(float(scores[idx]), 4)
        results.append(chunk)
    return results


MAX_HISTORY_TURNS = 6


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    model: Optional[str] = Field(default=None, max_length=200)
    # Previous turns of this conversation (oldest first), so follow-ups like "tell me more about it" work
    history: List[HistoryTurn] = Field(default_factory=list, max_length=20)


def retrieval_query(message: str, history: List[HistoryTurn]) -> str:
    """Short follow-up questions ("and in Jeddah?", "tell me more about it") carry little meaning on
    their own, so the previous user question is added to the search query."""
    previous = [t.content for t in history if t.role == "user"]
    if previous and len(tokenize(message)) <= 8:
        return f"{previous[-1]} {message}"
    return message


# ---------------------------------------------------------------------------
# Citation validation
# ---------------------------------------------------------------------------
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
REFUSAL_TEXT = "i don't have that information"


def _normalize_url(url: str) -> str:
    return url.split("#")[0].rstrip("/").rstrip(".,;").lower()


def validate_citations(reply: str, chunks: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Checks every Markdown link in the model's answer against the retrieved sources.

    - Links to retrieved source pages, or to URLs that appear inside retrieved text (e.g. Wasalt
      listing links), are kept.
    - Any other link is unverifiable: the link is removed and only its text is kept.
    - If an answer that isn't a refusal ends up with no verified citation, the retrieved source
      pages are appended so every factual answer remains traceable.
    """
    allowed = {_normalize_url(c.get("url", "")) for c in chunks if c.get("url")}
    for c in chunks:
        for found in re.findall(r"https?://[^\s;,)]+", c.get("text", "")):
            allowed.add(_normalize_url(found))

    verified, removed = 0, []

    def check(match: "re.Match") -> str:
        nonlocal verified
        text, url = match.group(1), match.group(2)
        if _normalize_url(url) in allowed:
            verified += 1
            return match.group(0)
        removed.append(url)
        return text

    cleaned = _MD_LINK_RE.sub(check, reply)
    is_refusal = cleaned.strip().lower().startswith(REFUSAL_TEXT)
    sources_appended = False
    if verified == 0 and not is_refusal and chunks:
        seen, refs = set(), []
        for c in chunks:
            if c.get("url") and c["url"] not in seen:
                seen.add(c["url"])
                refs.append(f"[{c.get('title', 'Source')}]({c['url']})")
            if len(refs) == 3:
                break
        cleaned = cleaned.rstrip() + "\n\n**Sources consulted:** " + " · ".join(refs)
        sources_appended = True
    if removed:
        logger.warning("Removed %d unverifiable citation(s): %s", len(removed), removed[:5])
    return cleaned, {"verified": verified, "removed": len(removed), "sources_appended": sources_appended}


# ---------------------------------------------------------------------------
# Abuse protection and quota saving
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "15"))
CACHE_TTL_SECONDS = int(os.getenv("ANSWER_CACHE_TTL_SECONDS", str(6 * 3600)))
CACHE_MAX_ENTRIES = 500

_rate_buckets: Dict[str, deque] = defaultdict(deque)
# Set when OpenRouter reports the account's daily free-model quota is used up. While set, the app
# answers in retrieval-only mode instead of making calls that are certain to fail.
QUOTA_COOLDOWN_SECONDS = 30 * 60
_quota_state: Dict[str, float] = {"exhausted_until": 0.0}
_answer_cache: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()


TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))


def client_ip(request: Request) -> str:
    """Visitor IP for rate limiting.

    X-Forwarded-For is "client, proxy1, proxy2, ...": each proxy APPENDS the address it received the
    request from, so only the right-most entries are written by infrastructure we trust. A visitor can
    put anything in the left-most entries, so those are never used. With TRUSTED_PROXY_HOPS=1 (one
    hosting proxy in front of the app, e.g. Hugging Face or Render) the last entry is the real client.
    Set TRUSTED_PROXY_HOPS=0 to ignore the header entirely (app exposed directly).
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    if TRUSTED_PROXY_HOPS > 0 and len(hops) >= TRUSTED_PROXY_HOPS:
        return hops[-TRUSTED_PROXY_HOPS]
    return request.client.host if request.client else "unknown"


def rate_limited(ip: str, now: Optional[float] = None) -> bool:
    """Sliding one-minute window per client IP."""
    now = time.time() if now is None else now
    bucket = _rate_buckets[ip]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_PER_MINUTE:
        return True
    bucket.append(now)
    return False


def cache_key(message: str, model: str) -> str:
    normalized = re.sub(r"\s+", " ", message.strip().lower())
    return f"{model}::{normalized}"


def cache_get(key: str) -> Optional[Dict[str, Any]]:
    hit = _answer_cache.get(key)
    if not hit:
        return None
    stored_at, value = hit
    if time.time() - stored_at > CACHE_TTL_SECONDS:
        _answer_cache.pop(key, None)
        return None
    _answer_cache.move_to_end(key)
    return value


def cache_put(key: str, value: Dict[str, Any]) -> None:
    _answer_cache[key] = (time.time(), value)
    _answer_cache.move_to_end(key)
    while len(_answer_cache) > CACHE_MAX_ENTRIES:
        _answer_cache.popitem(last=False)


def resolve_requested_model(requested: Optional[str]) -> str:
    """Only free OpenRouter models may be used. Any other model id sent by the browser is ignored,
    so nobody can make the server spend credits on paid models with the owner's API key."""
    default = RESOLVED_PRIMARY_MODEL or "openrouter/free"
    if not requested:
        return default
    requested = requested.strip()
    allowed = {m["id"] for m in AVAILABLE_FREE_MODELS}
    if requested in allowed or (not allowed and requested.endswith(":free")):
        return requested
    logger.warning("Ignoring non-free or unknown model requested by client: %r", requested)
    return default


def finalize(res: Dict[str, Any], chunks: List[Dict[str, Any]], attempts: int) -> Dict[str, Any]:
    reply, citations = validate_citations(res["reply"], chunks)
    return {**res, "reply": reply, "citations": citations, "retrieved_chunks": chunks, "attempts": attempts}


def retrieval_only_answer(chunks: List[Dict[str, Any]], reason: str) -> str:
    """Clearly labelled answer used when no AI model is available (quota exhausted, outage, no key)."""
    if not chunks:
        return f"The AI model is temporarily unavailable ({reason}), and no matching records were found in the scraped data."
    lines = [
        f"**The AI model is temporarily unavailable** ({reason}). "
        "Here are the most relevant passages from the scraped data instead:",
        "",
    ]
    # Show only reasonably relevant sources (at least 60% of the best match's score)
    best = max((c.get("bm25_score", 0.0) for c in chunks), default=0.0)
    relevant = [c for c in chunks if c.get("bm25_score", 0.0) >= 0.6 * best] or chunks[:1]
    for c in relevant[:5]:
        snippet = re.sub(r"\s+", " ", c.get("text", "")).strip()
        if len(snippet) > 280:
            snippet = snippet[:280].rsplit(" ", 1)[0] + "…"
        lines.append(f"- **{c.get('title', 'Source')}** — {snippet} [Source]({c.get('url')})")
    return "\n".join(lines)


async def call_openrouter_once(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://darglobal.co.uk",
        "X-Title": "DarGlobal & Wasalt AI Chatbot",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *(history or []),
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.1,
    }
    resp = await client.post(OPENROUTER_CHAT_URL, headers=headers, json=payload)
    if resp.status_code != 200:
        detail = resp.text[:300]
        if resp.status_code == 429 and "per-day" in resp.text:
            _quota_state["exhausted_until"] = time.time() + QUOTA_COOLDOWN_SECONDS
            logger.error("OpenRouter daily free-model quota is used up; pausing AI calls for %d minutes.", QUOTA_COOLDOWN_SECONDS // 60)
        logger.error("OpenRouter HTTP %d for model '%s': %s", resp.status_code, model, detail)
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"OpenRouter returned HTTP {resp.status_code} for model '{model}': {detail}",
        )
    data = resp.json()
    if "error" in data:
        err_obj = data["error"]
        code = int(err_obj.get("code", 502)) if str(err_obj.get("code", "")).isdigit() else 502
        msg = err_obj.get("message", str(err_obj))
        logger.error("OpenRouter API error payload (code %s) for model '%s': %s", code, model, msg)
        raise HTTPException(status_code=code, detail=f"OpenRouter error ({code}) on '{model}': {msg}")

    choices = data.get("choices") or []
    if not choices or not choices[0].get("message", {}).get("content"):
        logger.error("OpenRouter returned empty choices for model '%s'", model)
        raise HTTPException(status_code=502, detail=f"OpenRouter returned empty response for model '{model}'.")

    return {
        "reply": choices[0]["message"]["content"],
        "model_used": data.get("model", model),
    }


@app.get("/api/health")
async def health():
    kb = load_kb()
    has_key = bool(os.getenv("OPENROUTER_API_KEY", "").strip())
    return {
        "status": "ok",
        "records_loaded": len(kb),
        "retrieval": "rank_bm25 (top 8 chunks, max 2 per page, abbreviation expansion)",
        "openrouter_key_configured": has_key,
        "ai_quota_paused": time.time() < _quota_state["exhausted_until"],
        "primary_model": RESOLVED_PRIMARY_MODEL,
        "fallback_model": RESOLVED_FALLBACK_MODEL,
        "free_models_discovered": len(AVAILABLE_FREE_MODELS),
    }


@app.get("/api/models")
async def get_models():
    if not AVAILABLE_FREE_MODELS:
        await refresh_openrouter_free_models()
    return {
        "primary_model": RESOLVED_PRIMARY_MODEL,
        "fallback_model": RESOLVED_FALLBACK_MODEL,
        "models": AVAILABLE_FREE_MODELS,
    }


@app.get("/api/knowledge")
async def get_knowledge():
    return {"records": load_kb()}


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest, request: Request):
    if rate_limited(client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail=f"Too many questions in a short time (limit {RATE_LIMIT_PER_MINUTE} per minute). Please wait a moment and try again.",
        )
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    kb = load_kb()
    if not kb:
        logger.error("scraped_data.json is empty (0 records). Scraper has not been run yet.")
        raise HTTPException(
            status_code=503,
            detail="HTTP 503: app/data/scraped_data.json is empty (0 records). Run `docker compose --profile scraper run --rm scraper` first to populate the knowledge base.",
        )

    if not AVAILABLE_FREE_MODELS:
        await refresh_openrouter_free_models()

    history = req.history[-MAX_HISTORY_TURNS:]
    history_msgs = [{"role": t.role, "content": t.content[:2000]} for t in history]
    chunks = retrieve_bm25_chunks(retrieval_query(req.message, history))

    if not api_key:
        logger.error("OPENROUTER_API_KEY is not set; answering in retrieval-only mode.")
        return {
            "reply": retrieval_only_answer(chunks, "the server has no OpenRouter API key configured"),
            "model_used": "retrieval-only (no AI model)",
            "degraded": True,
            "retrieved_chunks": chunks,
        }
    context_text = "\n\n".join(
        f"[Chunk #{i+1} | Source: {c.get('source')} | Title: {c.get('title')} | "
        f"Location: {c.get('location')} | BM25: {c.get('bm25_score', 0.0)} | URL: {c.get('url')}]\n"
        f"{c.get('text', '')}"
        for i, c in enumerate(chunks)
    )

    system_prompt = (
        "You are a property intelligence assistant for DarGlobal and Wasalt.\n"
        "STRICT RULES:\n"
        "1. Answer ONLY using the facts explicitly present in the RETRIEVED CONTEXT below.\n"
        "2. Cite the exact source URLs in markdown format [Source Title](URL) for every statement you make.\n"
        "3. If the retrieved context does not contain the information needed to answer the question, "
        "say strictly: \"I don't have that information\" and do not guess or extrapolate.\n"
        "4. When sources give different figures for the same thing (e.g. GDV, revenue, number of projects), "
        "lead with the MOST RECENT figure, judged by the dates in the text (e.g. HY 2026 is newer than FY 2025, "
        "which is newer than August 2025). Mention older figures only briefly as history, with their dates.\n"
        "5. When asked for a list (partners, projects, locations), include every relevant item found in the context.\n"
        "6. Earlier messages in the conversation are only for understanding follow-up questions; "
        "facts must still come from the RETRIEVED CONTEXT. Only cite URLs that appear in the context.\n\n"
        f"RETRIEVED CONTEXT (Top BM25 Chunks):\n{context_text}"
    )

    primary_model = resolve_requested_model(req.model)
    # Only stand-alone questions are cached: a follow-up's answer depends on the conversation
    key = cache_key(req.message, primary_model) if not history else None
    cached = cache_get(key) if key else None
    if cached:
        return {**cached, "cached": True}

    if time.time() < _quota_state["exhausted_until"]:
        return {
            "reply": retrieval_only_answer(chunks, "today's free AI quota has been used up; it resets daily"),
            "model_used": "retrieval-only (daily AI quota reached)",
            "degraded": True,
            "retrieved_chunks": chunks,
        }
    # Select a large instruct fallback model from live discovered :free models distinct from primary_model
    candidates = [m["id"] for m in AVAILABLE_FREE_MODELS if m["id"] != primary_model]
    if RESOLVED_FALLBACK_MODEL and RESOLVED_FALLBACK_MODEL != primary_model:
        fallback_model = RESOLVED_FALLBACK_MODEL
    elif candidates:
        fallback_model = candidates[0]
    else:
        fallback_model = "openrouter/free"

    errors: List[str] = []
    last_status = 502

    async with httpx.AsyncClient(timeout=35.0) as client:
        # Attempt 1: Primary model
        try:
            res = await call_openrouter_once(client, api_key, primary_model, system_prompt, req.message, history=history_msgs)
            result = finalize(res, chunks, attempts=1)
            if key:
                cache_put(key, result)
            return result
        except HTTPException as exc1:
            last_status = exc1.status_code
            errors.append(f"Attempt 1 ({primary_model}) failed [HTTP {exc1.status_code}]: {exc1.detail}")
            logger.warning("Primary model '%s' failed on attempt 1 (HTTP %d). Retrying once...", primary_model, exc1.status_code)
        except Exception as exc1:
            errors.append(f"Attempt 1 ({primary_model}) network error: {exc1}")
            logger.warning("Primary model '%s' network error on attempt 1: %s. Retrying once...", primary_model, exc1)

        # Attempt 2: Retry primary model once (skipped when rate-limited: retrying immediately won't help)
        try:
            if last_status == 429:
                raise HTTPException(status_code=429, detail="Skipped retry after rate limit")
            res = await call_openrouter_once(client, api_key, primary_model, system_prompt, req.message, history=history_msgs)
            result = finalize(res, chunks, attempts=2)
            if key:
                cache_put(key, result)
            return result
        except HTTPException as exc2:
            last_status = exc2.status_code
            errors.append(f"Retry ({primary_model}) failed [HTTP {exc2.status_code}]: {exc2.detail}")
            logger.warning("Retry on '%s' failed (HTTP %d). Falling back to '%s'...", primary_model, exc2.status_code, fallback_model)
        except Exception as exc2:
            errors.append(f"Retry ({primary_model}) network error: {exc2}")
            logger.warning("Retry on '%s' network error: %s. Falling back to '%s'...", primary_model, exc2, fallback_model)

        # Attempt 3: Second large instruct free model fallback
        # (skipped when the account-wide daily quota is used up: every free model would fail)
        try:
            if time.time() < _quota_state["exhausted_until"]:
                raise HTTPException(status_code=429, detail="Daily free-model quota used up")
            res = await call_openrouter_once(client, api_key, fallback_model, system_prompt, req.message, history=history_msgs)
            result = finalize(res, chunks, attempts=3)
            result["fallback_notice"] = f"Primary model '{primary_model}' failed; answered using fallback model '{fallback_model}'."
            if key:
                cache_put(key, result)
            return result
        except HTTPException as exc3:
            last_status = exc3.status_code
            errors.append(f"Fallback ({fallback_model}) failed [HTTP {exc3.status_code}]: {exc3.detail}")
            logger.error("All OpenRouter attempts failed: %s", " | ".join(errors))
        except Exception as exc3:
            errors.append(f"Fallback ({fallback_model}) network error: {exc3}")
            logger.error("All OpenRouter attempts failed: %s", " | ".join(errors))

    if time.time() < _quota_state["exhausted_until"]:
        reason = "today's free AI quota has been used up; it resets daily"
    elif last_status == 429:
        reason = "free-model rate limit reached; please try again in a minute"
    else:
        reason = f"OpenRouter error HTTP {last_status}"
    return {
        "reply": retrieval_only_answer(chunks, reason),
        "model_used": "retrieval-only (AI unavailable)",
        "degraded": True,
        "errors": errors,
        "retrieved_chunks": chunks,
    }


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
