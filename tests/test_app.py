"""Automated tests for the DarGlobal & Wasalt chatbot.

Run from the project root:
    pip install -r requirements.txt -r requirements-dev.txt
    python -m pytest -v

No network access or API key is needed: OpenRouter calls are mocked.
"""

import os
import re
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import app.main as main
from app import scraper

FAKE_FREE_MODELS = [
    {"id": "big/model-a:free", "name": "A", "context_length": 100000, "param_b": 100.0, "score": 100.0},
    {"id": "mid/model-b:free", "name": "B", "context_length": 100000, "param_b": 30.0, "score": 50.0},
]


def reset_state():
    main._answer_cache.clear()
    main._rate_buckets.clear()
    main._quota_state["exhausted_until"] = 0.0
    main.AVAILABLE_FREE_MODELS = list(FAKE_FREE_MODELS)
    main.RESOLVED_PRIMARY_MODEL = "big/model-a:free"
    main.RESOLVED_FALLBACK_MODEL = "mid/model-b:free"


class TokenizerTests(unittest.TestCase):
    def test_gdv_is_expanded(self):
        tokens = main.tokenize("What is the GDV?")
        self.assertIn("gross", tokens)
        self.assertIn("development", tokens)

    def test_bedroom_count_is_kept(self):
        # "3" alone is too short to index, so "3 bedroom" must become a combined token
        self.assertIn("3bedroom", main.tokenize("3 bedroom apartment"))
        self.assertIn("3bedroom", main.tokenize("3BR flat"))
        self.assertIn("3bedroom", main.tokenize("apartment-with-3-bedrooms"))

    def test_partner_synonyms(self):
        self.assertIn("partnership", main.tokenize("partners"))


class RetrievalTests(unittest.TestCase):
    def test_knowledge_base_is_loaded(self):
        self.assertGreater(len(main.load_kb()), 1000)

    def test_gdv_question_finds_23bn_figure(self):
        chunks = main.retrieve_bm25_chunks("What is DarGlobal's GDV?")
        text = " ".join(c["text"] for c in chunks)
        self.assertRegex(text, r"23(\.0)?\s*(billion|bln|bn)|\$23")

    def test_partner_question_gets_directory_first(self):
        chunks = main.retrieve_bm25_chunks("Who does DarGlobal partner with?")
        self.assertEqual(chunks[0]["id"], "darglobal-partner-directory")
        for brand in ("Aston Martin", "Lamborghini", "Missoni", "Pagani", "The Trump Organization"):
            self.assertIn(brand, chunks[0]["text"])

    def test_bedroom_question_returns_matching_bedrooms(self):
        chunks = main.retrieve_bm25_chunks("Find 3 bedroom apartments for rent on Wasalt")
        self.assertTrue(all(re.search(r"\b3[- ]Bedroom", c["title"]) for c in chunks[:3]), [c["title"] for c in chunks[:3]])

    def test_district_question_lists_districts(self):
        chunks = main.retrieve_bm25_chunks("Which districts in North Riyadh have villas for sale?")
        top = " ".join(c["text"] for c in chunks[:3])
        for district in ("Al Narjis", "Al Malqa", "Hitteen"):
            self.assertIn(district, top)

    def test_project_question(self):
        chunks = main.retrieve_bm25_chunks("Tell me about DG1 in Business Bay")
        self.assertTrue(any(c["url"].endswith("/dg1") for c in chunks))

    def test_listing_id_is_matched_exactly(self):
        chunks = main.retrieve_bm25_chunks("What is the price of Wasalt listing #5907931?")
        self.assertIn("5907931", chunks[0]["text"])
        self.assertEqual(chunks[0]["source"], "Wasalt")

    def test_max_two_chunks_per_page(self):
        for q in ("DarGlobal GDV", "villas for sale in Riyadh", "Trump Tower Jeddah"):
            urls = [c["url"] for c in main.retrieve_bm25_chunks(q)]
            self.assertTrue(all(urls.count(u) <= 2 for u in urls), q)

    def test_every_chunk_has_a_source_url(self):
        for c in main.load_kb():
            self.assertTrue(c.get("url", "").startswith("https://"), c.get("id"))


class SafetyTests(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_paid_model_requests_are_ignored(self):
        self.assertEqual(main.resolve_requested_model("openai/gpt-5"), "big/model-a:free")
        self.assertEqual(main.resolve_requested_model("anthropic/claude-opus"), "big/model-a:free")

    def test_free_model_requests_are_allowed(self):
        self.assertEqual(main.resolve_requested_model("mid/model-b:free"), "mid/model-b:free")

    def test_rate_limiter(self):
        ip = "203.0.113.9"
        results = [main.rate_limited(ip, now=1000.0 + i) for i in range(main.RATE_LIMIT_PER_MINUTE + 1)]
        self.assertFalse(any(results[:-1]))
        self.assertTrue(results[-1])
        self.assertFalse(main.rate_limited(ip, now=1000.0 + 120))  # window has passed

    def test_answer_cache(self):
        key = main.cache_key("  What is   the GDV? ", "m")
        self.assertEqual(key, main.cache_key("what is the gdv?", "m"))
        main.cache_put(key, {"reply": "x"})
        self.assertEqual(main.cache_get(key), {"reply": "x"})


class ApiTests(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.client = TestClient(main.app)
        env = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        env.start()
        self.addCleanup(env.stop)

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(r.json()["records_loaded"], 1000)

    def test_index_and_vendored_assets(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.head("/").status_code, 200)  # hosting health probes use HEAD
        self.assertEqual(self.client.head("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/static/vendor/marked.min.js").status_code, 200)
        self.assertEqual(self.client.get("/static/vendor/purify.min.js").status_code, 200)

    def test_chat_success_and_cache(self):
        fake = mock.AsyncMock(return_value={"reply": "GDV is US$23 billion [src](https://darglobal.co.uk)", "model_used": "big/model-a:free"})
        with mock.patch.object(main, "call_openrouter_once", fake):
            r1 = self.client.post("/api/chat", json={"message": "What is DarGlobal's GDV?"})
            r2 = self.client.post("/api/chat", json={"message": "what is darglobal's gdv?"})
        self.assertEqual(r1.status_code, 200)
        self.assertIn("23 billion", r1.json()["reply"])
        self.assertTrue(r1.json()["retrieved_chunks"])
        self.assertTrue(r2.json().get("cached"))
        self.assertEqual(fake.await_count, 1)  # second identical question served from cache

    def test_chat_uses_fallback_model(self):
        calls = []

        async def flaky(client, key, model, system_prompt, message, **kwargs):
            calls.append(model)
            if model == "big/model-a:free":
                raise main.HTTPException(status_code=503, detail="down")
            return {"reply": "ok", "model_used": model}

        with mock.patch.object(main, "call_openrouter_once", flaky):
            r = self.client.post("/api/chat", json={"message": "Tell me about The Astera"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["model_used"], "mid/model-b:free")
        self.assertEqual(calls[-1], "mid/model-b:free")

    def test_chat_degrades_gracefully_when_all_models_fail(self):
        async def down(*args, **kwargs):
            raise main.HTTPException(status_code=429, detail="rate limited")

        with mock.patch.object(main, "call_openrouter_once", down):
            r = self.client.post("/api/chat", json={"message": "Tell me about The Astera"})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["degraded"])
        self.assertIn("temporarily unavailable", body["reply"])
        self.assertIn("https://", body["reply"])

    def test_chat_without_key_degrades_gracefully(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            r = self.client.post("/api/chat", json={"message": "Tell me about DG1"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["degraded"])

    def test_daily_quota_detection(self):
        import asyncio
        import httpx

        def handler(request):
            return httpx.Response(429, text='{"error":{"message":"Rate limit exceeded: free-models-per-day"}}')

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(main.HTTPException):
                    await main.call_openrouter_once(client, "k", "big/model-a:free", "sys", "hi")

        asyncio.run(run())
        self.assertGreater(main._quota_state["exhausted_until"], 0)

    def test_no_model_calls_while_quota_paused(self):
        import time as _time

        main._quota_state["exhausted_until"] = _time.time() + 600
        fake = mock.AsyncMock(return_value={"reply": "ok", "model_used": "x"})
        with mock.patch.object(main, "call_openrouter_once", fake):
            r = self.client.post("/api/chat", json={"message": "Tell me about DG1"})
        self.assertEqual(fake.await_count, 0)
        self.assertTrue(r.json()["degraded"])
        self.assertIn("quota", r.json()["reply"])

    def test_input_validation(self):
        self.assertEqual(self.client.post("/api/chat", json={"message": ""}).status_code, 422)
        self.assertEqual(self.client.post("/api/chat", json={"message": "x" * 1001}).status_code, 422)

    def test_rate_limit_returns_429(self):
        fake = mock.AsyncMock(return_value={"reply": "ok", "model_used": "big/model-a:free"})
        with mock.patch.object(main, "call_openrouter_once", fake):
            codes = [
                self.client.post("/api/chat", json={"message": f"question {i}"}).status_code
                for i in range(main.RATE_LIMIT_PER_MINUTE + 1)
            ]
        self.assertEqual(codes[-1], 429)


class CitationTests(unittest.TestCase):
    CHUNKS = [
        {"url": "https://darglobal.co.uk/dg1", "title": "DG1", "text": "DG1 is in Business Bay."},
        {"url": "https://wasalt.sa/en/villas-for-sale-in-riyadh", "title": "Villas",
         "text": "Sample: https://wasalt.sa/en/property/sale/villa-300-sqm-5841662 ; more"},
    ]

    def test_keeps_retrieved_and_in_text_links(self):
        reply = "DG1 [page](https://darglobal.co.uk/dg1/) and [villa](https://wasalt.sa/en/property/sale/villa-300-sqm-5841662)"
        cleaned, info = main.validate_citations(reply, self.CHUNKS)
        self.assertEqual(cleaned, reply)
        self.assertEqual((info["verified"], info["removed"]), (2, 0))

    def test_removes_invented_links(self):
        reply = "DG1 is great [made up](https://example.com/fake) [real](https://darglobal.co.uk/dg1)"
        cleaned, info = main.validate_citations(reply, self.CHUNKS)
        self.assertNotIn("example.com", cleaned)
        self.assertIn("made up", cleaned)
        self.assertEqual((info["verified"], info["removed"]), (1, 1))

    def test_uncited_answer_gets_sources_appended(self):
        cleaned, info = main.validate_citations("DG1 is in Business Bay.", self.CHUNKS)
        self.assertTrue(info["sources_appended"])
        self.assertIn("(https://darglobal.co.uk/dg1)", cleaned)

    def test_refusal_is_left_alone(self):
        cleaned, info = main.validate_citations("I don't have that information.", self.CHUNKS)
        self.assertEqual(cleaned, "I don't have that information.")
        self.assertFalse(info["sources_appended"])


class ConversationTests(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.client = TestClient(main.app)
        env = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        env.start()
        self.addCleanup(env.stop)

    def test_short_follow_up_uses_previous_question_for_search(self):
        history = [main.HistoryTurn(role="user", content="Tell me about The Astera"),
                   main.HistoryTurn(role="assistant", content="It is in Ras Al Khaimah.")]
        self.assertIn("Astera", main.retrieval_query("When is handover?", history))
        self.assertEqual(main.retrieval_query("What is DarGlobal's GDV and revenue growth in 2026 results?", history),
                         "What is DarGlobal's GDV and revenue growth in 2026 results?")

    def test_history_is_sent_to_model_and_not_cached(self):
        fake = mock.AsyncMock(return_value={"reply": "Handover is December 2028 [src](https://darglobal.co.uk/the-astera)", "model_used": "big/model-a:free"})
        body = {"message": "When is handover?", "history": [
            {"role": "user", "content": "Tell me about The Astera"},
            {"role": "assistant", "content": "It is on Al Marjan Island."}]}
        with mock.patch.object(main, "call_openrouter_once", fake):
            r1 = self.client.post("/api/chat", json=body)
            r2 = self.client.post("/api/chat", json=body)
        self.assertEqual(r1.status_code, 200)
        sent_history = fake.await_args.kwargs["history"]
        self.assertEqual([m["role"] for m in sent_history], ["user", "assistant"])
        self.assertTrue(any("astera" in c["url"] for c in r1.json()["retrieved_chunks"]))
        self.assertFalse(r2.json().get("cached", False))
        self.assertEqual(fake.await_count, 2)

    def test_invalid_history_role_rejected(self):
        r = self.client.post("/api/chat", json={"message": "hi", "history": [{"role": "system", "content": "x"}]})
        self.assertEqual(r.status_code, 422)


class ClientIpTests(unittest.TestCase):
    def _req(self, xff=None, host="10.0.0.5"):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return main.Request({"type": "http", "headers": headers, "client": (host, 1234)})

    def test_spoofed_left_entries_are_ignored(self):
        self.assertEqual(main.client_ip(self._req("1.2.3.4, 203.0.113.7")), "203.0.113.7")
        self.assertEqual(main.client_ip(self._req("9.9.9.9, 8.8.8.8, 203.0.113.7")), "203.0.113.7")

    def test_without_header_uses_socket_address(self):
        self.assertEqual(main.client_ip(self._req()), "10.0.0.5")

    def test_changing_fake_header_does_not_reset_limit(self):
        reset_state()
        ips = [main.client_ip(self._req(f"{i}.{i}.{i}.{i}, 203.0.113.7")) for i in range(main.RATE_LIMIT_PER_MINUTE + 1)]
        results = [main.rate_limited(ip, now=5000.0) for ip in ips]
        self.assertTrue(results[-1])


class ScraperParserTests(unittest.TestCase):
    def test_listing_url_with_stripped_decimal(self):
        rec = scraper.parse_wasalt_listing_url("https://wasalt.sa/en/property/rent/apartment-14483-sqm-with-3-bedrooms-5883217")
        self.assertEqual(rec["property_type"], "Apartment")
        self.assertEqual(rec["sale_or_rent"], "rent")
        self.assertEqual(rec["bedrooms"], "3")
        self.assertAlmostEqual(rec["size_sqm"], 144.83)

    def test_land_listing(self):
        rec = scraper.parse_wasalt_listing_url("https://wasalt.sa/en/property/sale/land-500-sqm-facing-west-on-15m-width-street-5865930")
        self.assertEqual((rec["property_type"], rec["size_sqm"], rec["street_facing"], rec["street_width_m"]), ("Land", 500.0, "West", "15"))

    def test_daily_rental_with_short_id(self):
        rec = scraper.parse_wasalt_listing_url("https://wasalt.sa/en/dailyrental/chalet-for-booking-3579")
        self.assertEqual((rec["listing_id"], rec["sale_or_rent"]), ("3579", "daily rent"))

    def test_category_url(self):
        rec = scraper.parse_wasalt_category_url("https://wasalt.sa/en/villas-for-sale-in-north-riyadh-al-narjis")
        self.assertEqual((rec["city"], rec["district"], rec["sale_or_rent"]), ("North Riyadh", "Al Narjis", "sale"))

    def test_darglobal_url_filter(self):
        self.assertTrue(scraper.is_allowed_darglobal_url("https://darglobal.co.uk/partners/pagani")[0])
        self.assertTrue(scraper.is_allowed_darglobal_url("https://darglobal.co.uk/dg1")[0])
        self.assertFalse(scraper.is_allowed_darglobal_url("https://darglobal.co.uk/ar/partners/pagani")[0])
        self.assertFalse(scraper.is_allowed_darglobal_url("https://darglobal.co.uk/careers")[0])

    def test_chunks_respect_size_limit(self):
        chunks = scraper.split_text_chunks("Luxury homes in Dubai. " * 300)
        self.assertTrue(all(len(c) <= 800 for c in chunks))


class UiSyncTests(unittest.TestCase):
    def test_ui_code_copies_are_current(self):
        import importlib.util
        from pathlib import Path

        script = Path(__file__).resolve().parent.parent / "scripts" / "sync_ui_code.py"
        spec = importlib.util.spec_from_file_location("sync_ui_code", script)
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)
        embedded = sync.embedded_code_files(sync.INDEX.read_text(encoding="utf-8"))
        self.assertEqual(embedded, sync.current_code_files(), "Run: python scripts/sync_ui_code.py")


if __name__ == "__main__":
    unittest.main()
