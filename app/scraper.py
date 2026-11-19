r"""
Production Scraper for DarGlobal (https://darglobal.co.uk) and Wasalt (https://wasalt.sa).

Fixes included:
1. DarGlobal (Playwright headless Chromium):
   - max_pages = 250, scraping 4 pages in parallel with per-page progress logging ("[23/165] ok <url>").
   - /projects and top-level project pages prioritized first.
   - Includes top-level project pages: the-astera, dg1, w-residences, davinci-tower-by-pagani,
     urban-oasis-by-missoni, trump-tower-jeddah, trump-international-hotel-and-tower-dubai,
     d-villas-at-jge, tierra-viva, marea, les-vagues, neptune, the-mulliner, la-collina,
     aida-*, rayana/*, amaya/*, trump-plaza/*.
   - Boilerplate cleanup: strips contact/register forms ("How did you hear about us?",
     "Full Name Phone Email Select Project..."), project dropdown menus, and cookie banners;
     drops any chunk whose text appears on more than 3 pages; removes exact duplicate chunks.
2. Wasalt (Public XML Sitemaps in https://wasalt.sa/robots.txt):
   - Restores stripped decimal points in URL sizes (e.g., apartment-14483-sqm -> 144.83 sqm,
     apartment-6675-sqm -> 66.75 sqm): for apartments/floors/rooms, if size > 1000, divides by 100.
   - Accepts 3+ digits in the listing-ID regex (-(\d{3,10})$) and parses "dailyrental" / "for-booking"
     as "daily rent".
   - Eliminates "Saudi Arabia, Saudi Arabia" duplicate location labels.
   - Includes 35 sample listings per property type (plus bedroom-specific sub-groups),
     split into ~800-character chunks.
3. Clean replacement: Overwrites app/data/scraped_data.json on each run.
"""

import argparse
import asyncio
import fnmatch
import gzip
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper")

DARGLOBAL_SITEMAP_URL = "https://darglobal.co.uk/sitemap.xml"

DARGLOBAL_ALLOWED_SECTIONS = (
    "projects",
    "partners",
    "one-of-one",
    "insights",
    "press",
    "about",
    "why-invest",
    "investor",
    "faq",
)

DARGLOBAL_TOP_LEVEL_PROJECT_PATTERNS = (
    "the-astera",
    "the-astera/*",
    "dg1",
    "dg1/*",
    "w-residences",
    "w-residences/*",
    "davinci-tower-by-pagani",
    "davinci-tower-by-pagani/*",
    "urban-oasis-by-missoni",
    "urban-oasis-by-missoni/*",
    "trump-tower-jeddah",
    "trump-tower-jeddah/*",
    "trump-international-hotel-and-tower-dubai",
    "trump-international-hotel-and-tower-dubai/*",
    "d-villas-at-jge",
    "d-villas-at-jge/*",
    "tierra-viva",
    "tierra-viva/*",
    "marea",
    "marea/*",
    "les-vagues",
    "les-vagues/*",
    "neptune",
    "neptune/*",
    "the-mulliner",
    "the-mulliner/*",
    "la-collina",
    "la-collina/*",
    "aida",
    "aida-*",
    "aida/*",
    "rayana",
    "rayana/*",
    "amaya",
    "amaya/*",
    "trump-plaza",
    "trump-plaza/*",
)

NON_ENGLISH_PREFIXES = ("/ar/", "/ru/", "/zh/", "/es/", "/fr/", "/de/", "/tr/", "/fa/")

WASALT_ROBOTS_URL = "https://wasalt.sa/robots.txt"
WASALT_SITEMAPS = {
    "product_sale": "https://cdn.wasalt.sa/sitemap/product_sitemap_en_sa.xml.gz",
    "category": "https://cdn.wasalt.sa/sitemap/category_sitemap_en_sa.xml.gz",
    "rental_pdp": "https://cdn.wasalt.sa/sitemap/rental_pdp_sitemap_en_sa.xml.gz",
    "static": "https://cdn.wasalt.sa/sitemap/static_sitemap_en_sa.xml.gz",
}

KNOWN_KSA_CITIES = [
    "west-riyadh", "north-riyadh", "east-riyadh", "south-riyadh", "center-riyadh", "riyadh",
    "north-jeddah", "south-jeddah", "jeddah",
    "makkah-al-mukarramah", "makkah", "mecca",
    "al-madinah-al-munawwarah", "madinah", "medina",
    "dammam", "al-khobar", "khobar", "dhahran", "qatif", "al-ahsa", "hofuf", "jubail", "abqaiq",
    "abha", "khamis-mushait", "khamis-mushayt", "taif", "buraidah", "unaizah", "hail",
    "tabuk", "jazan", "najran", "yanbu", "al-kharj", "al-baha", "sakaka", "arar",
]

UNIT_TYPES_MAX_5000_SQM = {"apartment", "apartments", "floor", "floors", "room", "rooms", "studio", "duplex"}

# Boilerplate regex patterns stripped from DarGlobal pages before chunking
DARGLOBAL_BOILERPLATE_PATTERNS = [
    re.compile(
        r"Register Your Interest\s+Discover exclusive luxury real estate.*?(?:Register Interest|Privacy Policy)",
        re.I | re.S,
    ),
    re.compile(
        r"Please provide your details to register interest\..*?(?:Register Interest|Privacy Policy)",
        re.I | re.S,
    ),
    re.compile(
        r"Full Name\s+Phone\s+\+?\d*\s*Email\s+Select Project.*?(?:Register Interest|Privacy Policy)",
        re.I | re.S,
    ),
    re.compile(
        r"Select Project\s+AIDA\s+Padel Living Residences.*?(?:How did you hear about us\?|Register Interest)",
        re.I | re.S,
    ),
    re.compile(
        r"How did you hear about us\?\s*(?:Google)?\s*(?:Social Media)?.*?(?:Register Interest|Privacy Policy)",
        re.I | re.S,
    ),
    re.compile(
        r"Discover Luxury Properties\s+Luxury Villas in UAE.*?(?:ClickCease|©\s*\d{4}\s*DarGlobal|$)",
        re.I | re.S,
    ),
    re.compile(
        r"We value our privacy\s+We use cookies to enhance your browsing experience.*?(?:ACCEPT|ClickCease|$)",
        re.I | re.S,
    ),
    re.compile(
        r"yandex\s+yandex\s+linkedin\s+Darglobal\s+Register Interest.*?(?:English\s*\|\s*العربية)",
        re.I | re.S,
    ),
    re.compile(
        r"Building Communities\s+Real Estate developments\s+Asset Management\s+Collaborations\s+Golf\s+Hospitality\s+Commercial\s+NEWSROOM\s+Tokenization\s+Investor Relations\s+about us\s+contact us\s+Terms and Conditions",
        re.I | re.S,
    ),
]


def clean_darglobal_text(soup: BeautifulSoup) -> str:
    """Removes forms, selects, nav/footer elements, and repeated DarGlobal site boilerplate."""
    for tag in soup([
        "script", "style", "noscript", "nav", "footer", "header",
        "svg", "form", "select", "option", "button", "input", "textarea", "iframe"
    ]):
        tag.decompose()

    # Remove common modal/cookie/form containers by class or id keywords
    for el in list(soup.find_all(True)):
        if el.attrs is None:
            continue
        cls_id = " ".join(el.get("class", []) if isinstance(el.get("class"), list) else [str(el.get("class", ""))])
        cls_id += " " + str(el.get("id", ""))
        if any(k in cls_id.lower() for k in ("cookie", "register-interest", "modal", "popup", "newsletter", "footer", "navbar")):
            el.decompose()

    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    for pat in DARGLOBAL_BOILERPLATE_PATTERNS:
        text = pat.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_chunk_key(text: str) -> str:
    """Normalized key for deduplicating chunks across pages."""
    return re.sub(r"\s+", " ", text.strip()).lower()


def format_ksa_location(city_or_region: Optional[str]) -> str:
    """Formats a Saudi location cleanly and prevents 'Saudi Arabia, Saudi Arabia'."""
    if not city_or_region:
        return "Saudi Arabia"
    cleaned = re.sub(r"(?i)\b(saudi\s+arabia|ksa|undefined)\b", "", city_or_region)
    cleaned = re.sub(r"[,\s]+", " ", cleaned).strip(" ,-")
    if not cleaned:
        return "Saudi Arabia"
    return f"{cleaned}, Saudi Arabia"


def split_text_chunks(text: str, target_chars: int = 800) -> List[str]:
    """Splits text into chunks of ~800 characters along sentence or semicolon boundaries."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return []
    sentences = re.split(r"(?<=[.!?;])\s+", cleaned)
    chunks: List[str] = []
    current = ""
    for sent in sentences:
        if not sent:
            continue
        if len(current) + len(sent) + 1 <= target_chars:
            current = f"{current} {sent}".strip() if current else sent
        else:
            if current:
                chunks.append(current)
            if len(sent) <= target_chars:
                current = sent
            else:
                for i in range(0, len(sent), target_chars):
                    chunks.append(sent[i : i + target_chars].strip())
                current = ""
    if current:
        chunks.append(current)
    return chunks


def infer_darglobal_location(url: str, title: str, text: str) -> str:
    """Infers a specific city/country location for a DarGlobal page."""
    combined = f"{url} {title} {text[:500]}".lower()
    location_map = [
        (("ras al khaimah", "al marjan", "astera"), "Al Marjan Island, Ras Al Khaimah, UAE"),
        (("dubai", "pagani", "dg1", "jumeirah", "d-villas", "urban oasis", "w residences", "w-residences"), "Dubai, United Arab Emirates"),
        (("uae", "united arab emirates"), "Dubai & Ras Al Khaimah, UAE"),
        (("jeddah", "amaya", "padel living", "trump-plaza", "trump-tower-jeddah"), "Jeddah, Saudi Arabia"),
        (("riyadh", "wadi safar", "neptune", "mouawad", "rayana"), "Riyadh, Saudi Arabia"),
        (("saudi-arabia", "saudi arabia", "ksa"), "Riyadh & Jeddah, Saudi Arabia"),
        (("oman", "muscat", "aida"), "AIDA, Muscat, Oman"),
        (("qatar", "doha", "simaisma", "les vagues", "les-vagues", "lumaia"), "Doha & Simaisma, Qatar"),
        (("spain", "benahavis", "benahavís", "cortesin", "lamborghini", "tierra viva", "tierra-viva", "marea", "la-collina"), "Costa del Sol, Spain"),
        (("maldives",), "Maldives"),
        (("mulliner", "park lane", "albert hall", "hyde park", "properties-in-uk"), "London, United Kingdom"),
        (("greece", "athens"), "Athens, Greece"),
    ]
    for keywords, label in location_map:
        if any(k in combined for k in keywords):
            return label
    return "London, United Kingdom"


def is_allowed_darglobal_url(url: str) -> Tuple[bool, str, int]:
    """
    Checks if a DarGlobal URL is an allowed English page and returns:
    (is_allowed, category_name, priority_rank)
    Priority 0 = /projects/* and top-level project pages (scraped first)
    Priority 1 = investor, about, why-invest, faq, partners, one-of-one
    Priority 2 = press, insights
    """
    parsed = urlparse(url)
    if parsed.netloc and "darglobal.co.uk" not in parsed.netloc.lower():
        return False, "", 99
    path = parsed.path.lower().rstrip("/")
    if not path or path == "/":
        return False, "", 99
    if any(path == pref.rstrip("/") or path.startswith(pref) for pref in NON_ENGLISH_PREFIXES):
        return False, "", 99

    rel_path = path.lstrip("/")

    for pat in DARGLOBAL_TOP_LEVEL_PROJECT_PATTERNS:
        if fnmatch.fnmatch(rel_path, pat):
            return True, "projects", 0

    for sec in DARGLOBAL_ALLOWED_SECTIONS:
        if rel_path == sec or rel_path.startswith(f"{sec}/"):
            if sec == "projects":
                priority = 0
            elif sec in ("investor", "about", "why-invest", "faq", "partners", "one-of-one"):
                priority = 1
            else:
                priority = 2
            return True, sec, priority

    return False, "", 99


def extract_urls_from_xml(xml_text: str) -> List[str]:
    """Extracts all <loc> URLs from a sitemap or sitemapindex XML string."""
    urls = re.findall(r"<loc>\s*(https?://[^<\s]+)\s*</loc>", xml_text, flags=re.I)
    return [u.strip() for u in urls]


async def scrape_darglobal_playwright(max_pages: int = 250, concurrency: int = 4) -> List[Dict]:
    """
    Scrapes DarGlobal English pages from sitemap.xml using Playwright headless Chromium
    with 4 pages in parallel, logging progress per page ("[23/165] ok <url>"),
    stripping boilerplate, dropping chunks appearing on >3 pages, and removing exact duplicates.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright is not installed; skipping DarGlobal scrape.")
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-GB",
        )
        page = await context.new_page()

        logger.info("Fetching DarGlobal sitemap: %s", DARGLOBAL_SITEMAP_URL)
        sitemap_urls: List[str] = []
        try:
            await page.goto(DARGLOBAL_SITEMAP_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            content = await page.content()
            locs = extract_urls_from_xml(content)
            child_sitemaps = [u for u in locs if u.endswith(".xml")]
            if child_sitemaps:
                for sm_url in child_sitemaps[:10]:
                    try:
                        await page.goto(sm_url, wait_until="domcontentloaded", timeout=25000)
                        await page.wait_for_timeout(1200)
                        sitemap_urls.extend(extract_urls_from_xml(await page.content()))
                    except Exception as exc:
                        logger.warning("Failed child sitemap %s: %s", sm_url, exc)
            else:
                sitemap_urls.extend(locs)
        except Exception as exc:
            logger.warning("Could not load DarGlobal sitemap.xml directly (%s); using seed English URLs.", exc)
        finally:
            await page.close()

        seed_urls = [
            "https://darglobal.co.uk/projects",
            "https://darglobal.co.uk/projects/properties-in-uae",
            "https://darglobal.co.uk/projects/properties-in-saudi-arabia",
            "https://darglobal.co.uk/projects/properties-in-oman",
            "https://darglobal.co.uk/projects/properties-in-spain",
            "https://darglobal.co.uk/the-astera",
            "https://darglobal.co.uk/dg1",
            "https://darglobal.co.uk/w-residences",
            "https://darglobal.co.uk/davinci-tower-by-pagani",
            "https://darglobal.co.uk/urban-oasis-by-missoni",
            "https://darglobal.co.uk/trump-tower-jeddah",
            "https://darglobal.co.uk/trump-international-hotel-and-tower-dubai",
            "https://darglobal.co.uk/d-villas-at-jge",
            "https://darglobal.co.uk/tierra-viva",
            "https://darglobal.co.uk/marea",
            "https://darglobal.co.uk/les-vagues",
            "https://darglobal.co.uk/neptune",
            "https://darglobal.co.uk/the-mulliner",
            "https://darglobal.co.uk/la-collina",
            "https://darglobal.co.uk/about",
            "https://darglobal.co.uk/investor",
            "https://darglobal.co.uk/why-invest",
            "https://darglobal.co.uk/faq",
            "https://darglobal.co.uk/partners",
            "https://darglobal.co.uk/one-of-one/one-of-one-unlisted-by-pagani",
            "https://darglobal.co.uk/insights",
            "https://darglobal.co.uk/press",
        ]
        all_candidates = list(dict.fromkeys(sitemap_urls + seed_urls))

        prioritized_targets: List[Tuple[int, int, str, str]] = []
        for idx, u in enumerate(all_candidates):
            ok, sec, prio = is_allowed_darglobal_url(u)
            if ok:
                prioritized_targets.append((prio, idx, u, sec))

        prioritized_targets.sort(key=lambda x: (x[0], x[1]))
        selected_targets = [(u, sec) for _, _, u, sec in prioritized_targets[:max_pages]]
        total_pages = len(selected_targets)

        logger.info(
            "DarGlobal English target pages to scrape: %d (max %d, concurrency=%d)",
            total_pages,
            max_pages,
            concurrency,
        )

        sem = asyncio.Semaphore(concurrency)

        async def fetch_one_page(page_num: int, target_url: str, section: str) -> Optional[Dict]:
            async with sem:
                tab = await context.new_page()
                try:
                    await tab.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    await tab.wait_for_timeout(1800)
                    try:
                        await tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await tab.wait_for_timeout(700)
                    except Exception:
                        pass
                    html = await tab.content()
                    soup = BeautifulSoup(html, "html.parser")
                    title = soup.title.get_text(" ", strip=True) if soup.title else target_url
                    if "incapsula" in title.lower() or "access denied" in title.lower():
                        logger.warning("[%d/%d] blocked %s", page_num, total_pages, target_url)
                        return None
                    body_text = clean_darglobal_text(soup)
                    if len(body_text) < 100:
                        logger.info("[%d/%d] skip (short text) %s", page_num, total_pages, target_url)
                        return None
                    location = infer_darglobal_location(target_url, title, body_text)
                    chunks = split_text_chunks(body_text, target_chars=800)
                    logger.info("[%d/%d] ok %s (%d chunks)", page_num, total_pages, target_url, len(chunks))
                    return {
                        "url": target_url,
                        "section": section,
                        "title": title,
                        "location": location,
                        "chunks": chunks,
                    }
                except Exception as exc:
                    logger.warning("[%d/%d] error %s: %s", page_num, total_pages, target_url, exc)
                    return None
                finally:
                    await tab.close()

        tasks = [
            fetch_one_page(i, target_url, section)
            for i, (target_url, section) in enumerate(selected_targets, start=1)
        ]
        page_results = await asyncio.gather(*tasks)
        await browser.close()

    # Count how many distinct pages each chunk text appears on
    chunk_page_sets: Dict[str, Set[str]] = defaultdict(set)
    valid_pages = [pr for pr in page_results if pr is not None]
    for pr in valid_pages:
        for ch in pr["chunks"]:
            norm = normalize_chunk_key(ch)
            if norm:
                chunk_page_sets[norm].add(pr["url"])

    # Drop any chunk appearing on > 3 pages and remove exact duplicate chunks
    records: List[Dict] = []
    seen_chunks: Set[str] = set()
    dropped_repeated = 0
    dropped_dups = 0
    chunk_idx = 1

    for pr in valid_pages:
        kept_for_page: List[str] = []
        for ch in pr["chunks"]:
            norm = normalize_chunk_key(ch)
            if not norm:
                continue
            if len(chunk_page_sets[norm]) > 3:
                dropped_repeated += 1
                continue
            if norm in seen_chunks:
                dropped_dups += 1
                continue
            seen_chunks.add(norm)
            kept_for_page.append(ch)

        for idx, chunk in enumerate(kept_for_page, start=1):
            suffix = f" (Part {idx}/{len(kept_for_page)})" if len(kept_for_page) > 1 else ""
            records.append({
                "id": f"darglobal-{chunk_idx}",
                "source": "DarGlobal",
                "category": pr["section"],
                "location": pr["location"],
                "title": f"{pr['title']}{suffix}",
                "text": chunk,
                "url": pr["url"],
            })
            chunk_idx += 1

    logger.info(
        "Collected %d DarGlobal chunks via Playwright (dropped %d boilerplate chunks on >3 pages, %d exact duplicates).",
        len(records),
        dropped_repeated,
        dropped_dups,
    )
    return records


def fetch_wasalt_sitemap_xml(client: httpx.Client, url: str) -> str:
    """Fetches a Wasalt sitemap URL and handles plain XML even when named .xml.gz."""
    resp = client.get(url)
    resp.raise_for_status()
    raw = resp.content
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw).decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def normalize_wasalt_size_sqm(raw_size_str: Optional[str], prop_type: str) -> Optional[float]:
    """
    Normalizes size in SQM from Wasalt URL slugs where decimal points were removed
    (e.g. apartment-14483-sqm -> 144.83 sqm, apartment-6675-sqm -> 66.75 sqm).
    For apartments/floors/rooms, if size > 1000, divide by 100 instead of discarding it.
    """
    if not raw_size_str:
        return None
    try:
        val = float(raw_size_str)
    except ValueError:
        return None
    if val <= 0:
        return None

    pt_lower = prop_type.lower()
    is_unit = any(u in pt_lower for u in UNIT_TYPES_MAX_5000_SQM)

    if is_unit and val > 1000.0:
        val = val / 100.0

    if is_unit and val > 5000.0:
        return None

    return round(val, 2)


def parse_wasalt_listing_url(url: str, default_for: str = "sale") -> Optional[Dict]:
    """
    Extracts property_type, sale/rent/daily rent, size_sqm, bedrooms, street_facing,
    street_width_m, and listing_id (3+ digits) from a Wasalt listing URL.
    """
    decoded = unquote(url).strip()
    path = urlparse(decoded).path
    path_lower = path.lower()
    slug = path.rstrip("/").split("/")[-1].lower()
    if not slug:
        return None

    listing_id_match = re.search(r"-(\d{3,10})$", slug)
    if not listing_id_match:
        return None
    listing_id = listing_id_match.group(1)

    if "dailyrental" in path_lower or "daily-rental" in path_lower or "for-booking" in slug:
        sale_or_rent = "daily rent"
    elif "/property/sale/" in path_lower or "for-sale" in slug:
        sale_or_rent = "sale"
    elif "/property/rent/" in path_lower or "for-rent" in slug:
        sale_or_rent = "rent"
    else:
        sale_or_rent = default_for

    prop_match = re.match(r"^([a-z-]+?)-(?:\d|with-|facing-|for-)", slug)
    prop_type = prop_match.group(1).replace("-", " ").title() if prop_match else "Property"

    size_match = re.search(r"-(\d+(?:\.\d+)?)-sqm", slug)
    raw_size = size_match.group(1) if size_match else None
    size_sqm = normalize_wasalt_size_sqm(raw_size, prop_type)

    bed_match = re.search(r"with-(\d+)-bedroom", slug)
    bedrooms = bed_match.group(1) if bed_match else None

    facing_match = re.search(r"facing-([a-z-]+?)(?:-on-|-with-|-\d+$)", slug)
    facing = facing_match.group(1).replace("-", " ").title() if facing_match else None

    width_match = re.search(r"on-(\d+(?:\.\d+)?)m-width-street", slug)
    street_width_m = width_match.group(1) if width_match else None

    parts = [p for p in path.split("/") if p]
    raw_city = None
    if len(parts) >= 4 and parts[1].lower() == "property":
        seg = parts[2].lower()
        if seg not in ("sale", "rent", "dailyrental", "daily-rental", "undefined", "saudi arabia", "ksa"):
            raw_city = parts[2].title()

    location = format_ksa_location(raw_city)

    return {
        "listing_id": listing_id,
        "property_type": prop_type,
        "sale_or_rent": sale_or_rent,
        "size_sqm": size_sqm,
        "bedrooms": bedrooms,
        "street_facing": facing,
        "street_width_m": street_width_m,
        "location": location,
        "url": url,
    }


def parse_wasalt_category_url(url: str) -> Optional[Dict]:
    """
    Extracts property_type, sale/rent, city, and district out of a Wasalt category URL.
    """
    path = urlparse(unquote(url)).path.rstrip("/")
    slug = path.split("/")[-1].lower()
    m = re.match(r"^([a-z0-9-]+?)-for-(sale|rent)-in-([a-z0-9-]+)$", slug)
    if not m:
        return None
    raw_prop, sale_or_rent, loc_slug = m.groups()
    loc_slug = re.sub(r"-for-sar-[a-z0-9-]+$", "", loc_slug)

    matched_city = "Saudi Arabia"
    district = loc_slug.replace("-", " ").title()
    for city_slug in KNOWN_KSA_CITIES:
        if loc_slug == city_slug:
            matched_city = city_slug.replace("-", " ").title()
            district = "All Districts"
            break
        elif loc_slug.startswith(city_slug + "-"):
            matched_city = city_slug.replace("-", " ").title()
            district = loc_slug[len(city_slug) + 1 :].replace("-", " ").title()
            break

    return {
        "property_type": raw_prop.replace("-", " ").title(),
        "sale_or_rent": sale_or_rent,
        "city": matched_city,
        "district": district,
        "location": format_ksa_location(matched_city),
        "url": url,
    }


def format_sample_listing(s: Dict) -> str:
    desc = f"ID #{s['listing_id']} ({s['property_type']} for {s['sale_or_rent']}"
    if s.get("size_sqm") is not None:
        desc += f", {s['size_sqm']:g} SQM"
    if s.get("bedrooms"):
        desc += f", {s['bedrooms']} bedroom" + ("s" if s["bedrooms"] != "1" else "")
    if s.get("street_facing"):
        desc += f", facing {s['street_facing']}"
    if s.get("street_width_m"):
        desc += f" on {s['street_width_m']}m street"
    desc += f") {s['url']}"
    return desc


def scrape_wasalt_sitemaps(samples_per_type: int = 35) -> List[Dict]:
    """Scrapes Wasalt's public XML sitemaps and builds summary + 35 sample listing chunks per type."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/xml,text/xml,*/*;q=0.9",
    }
    records: List[Dict] = []
    listings: List[Dict] = []
    categories: List[Dict] = []
    static_urls: List[str] = []

    with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
        try:
            robots_resp = client.get(WASALT_ROBOTS_URL)
            logger.info("Checked %s (status %d)", WASALT_ROBOTS_URL, robots_resp.status_code)
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", WASALT_ROBOTS_URL, exc)

        for key, sm_url in WASALT_SITEMAPS.items():
            try:
                logger.info("Downloading Wasalt sitemap [%s]: %s", key, sm_url)
                xml_text = fetch_wasalt_sitemap_xml(client, sm_url)
                urls = extract_urls_from_xml(xml_text)
                logger.info("Extracted %d URLs from %s", len(urls), sm_url)

                if key in ("product_sale", "rental_pdp"):
                    default_mode = "rent" if key == "rental_pdp" else "sale"
                    for u in urls:
                        parsed = parse_wasalt_listing_url(u, default_for=default_mode)
                        if parsed:
                            listings.append(parsed)
                elif key == "category":
                    for u in urls:
                        parsed_cat = parse_wasalt_category_url(u)
                        if parsed_cat:
                            categories.append(parsed_cat)
                elif key == "static":
                    static_urls.extend(urls)
            except Exception as exc:
                logger.warning("Failed to fetch/parse Wasalt sitemap %s: %s", sm_url, exc)

    if listings:
        by_type_mode: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
        by_bed_type_mode: Dict[Tuple[str, str, str], List[Dict]] = defaultdict(list)
        for item in listings:
            by_type_mode[(item["property_type"], item["sale_or_rent"])].append(item)
            if item.get("bedrooms"):
                by_bed_type_mode[(item["property_type"], item["sale_or_rent"], item["bedrooms"])].append(item)

        overall_counts = ", ".join(
            f"{ptype} ({mode}): {len(items)} listings"
            for (ptype, mode), items in sorted(by_type_mode.items(), key=lambda x: -len(x[1]))
        )
        records.append({
            "id": "wasalt-listings-overview",
            "source": "Wasalt",
            "category": "product_sitemap & rental_pdp_sitemap",
            "location": "Saudi Arabia",
            "title": f"Wasalt Sitemap Inventory: {len(listings)} Parsed Property Listings by Type & Transaction",
            "text": (
                f"Scraped {len(listings)} total property listing URLs from Wasalt public sitemaps "
                f"({WASALT_SITEMAPS['product_sale']} and {WASALT_SITEMAPS['rental_pdp']}). "
                f"Breakdown by property type and transaction (sale, rent, daily rent): {overall_counts}."
            )[:800],
            "url": WASALT_SITEMAPS["product_sale"],
        })

        for (ptype, mode), items in sorted(by_type_mode.items(), key=lambda x: -len(x[1])):
            valid_sizes = [float(i["size_sqm"]) for i in items if i.get("size_sqm") is not None]
            beds = Counter(i["bedrooms"] for i in items if i.get("bedrooms"))
            facings = Counter(i["street_facing"] for i in items if i.get("street_facing"))
            widths = Counter(i["street_width_m"] for i in items if i.get("street_width_m"))

            stats_parts = [f"Wasalt {ptype} for {mode}: {len(items)} total listings."]
            if valid_sizes:
                med_size = median(valid_sizes)
                stats_parts.append(f"Median size: {med_size:g} SQM (across {len(valid_sizes)} listings with valid size).")
            if beds:
                top_beds = ", ".join(f"{b}-bedroom ({cnt})" for b, cnt in beds.most_common(6))
                stats_parts.append(f"Bedroom counts: {top_beds}.")
            if facings:
                top_fac = ", ".join(f"{f} ({cnt})" for f, cnt in facings.most_common(5))
                stats_parts.append(f"Street facings: {top_fac}.")
            if widths:
                top_w = ", ".join(f"{w}m ({cnt})" for w, cnt in widths.most_common(5))
                stats_parts.append(f"Street widths: {top_w}.")

            header_summary = " ".join(stats_parts)
            sample_strs = [format_sample_listing(s) for s in items[:samples_per_type]]
            full_text = header_summary + " Sample listings: " + " ; ".join(sample_strs)
            loc_label = format_ksa_location(items[0].get("location"))

            chunks = split_text_chunks(full_text, target_chars=800)
            for idx, ch in enumerate(chunks, start=1):
                records.append({
                    "id": f"wasalt-listing-{ptype.lower().replace(' ', '-')}-{mode.replace(' ', '-')}-{idx}",
                    "source": "Wasalt",
                    "category": f"{ptype} ({mode.title()})",
                    "location": loc_label,
                    "title": f"Wasalt {ptype} for {mode.title()} Summary & Sample Listings (Part {idx}/{len(chunks)})",
                    "text": ch,
                    "url": items[0]["url"],
                })

        for (ptype, mode, bed_cnt), items in sorted(by_bed_type_mode.items(), key=lambda x: -len(x[1])):
            if len(items) < 2:
                continue
            valid_sizes = [float(i["size_sqm"]) for i in items if i.get("size_sqm") is not None]
            med_str = f", median size {median(valid_sizes):g} SQM" if valid_sizes else ""
            sample_strs = [format_sample_listing(s) for s in items[:25]]
            bed_text = (
                f"Wasalt {bed_cnt}-bedroom {ptype}s for {mode}: {len(items)} listings found{med_str}. "
                f"Sample {bed_cnt}-bedroom {ptype.lower()} listings for {mode}: " + " ; ".join(sample_strs)
            )
            loc_label = format_ksa_location(items[0].get("location"))
            chunks = split_text_chunks(bed_text, target_chars=800)
            for idx, ch in enumerate(chunks, start=1):
                records.append({
                    "id": f"wasalt-bed-{bed_cnt}br-{ptype.lower().replace(' ', '-')}-{mode.replace(' ', '-')}-{idx}",
                    "source": "Wasalt",
                    "category": f"{bed_cnt} Bedroom {ptype} ({mode.title()})",
                    "location": loc_label,
                    "title": f"Wasalt {bed_cnt}-Bedroom {ptype}s for {mode.title()} ({len(items)} Listings, Part {idx}/{len(chunks)})",
                    "text": ch,
                    "url": items[0]["url"],
                })

    if categories:
        by_city: Dict[str, List[Dict]] = defaultdict(list)
        for c in categories:
            by_city[c["city"]].append(c)

        for city, cat_items in sorted(by_city.items(), key=lambda x: -len(x[1])):
            loc_label = format_ksa_location(city)
            districts = sorted({c["district"] for c in cat_items if c["district"] != "All Districts"})
            prop_types = Counter(f"{c['property_type']} ({c['sale_or_rent']})" for c in cat_items)
            type_summary = ", ".join(f"{k}: {v} category pages" for k, v in prop_types.most_common(8))
            sample_urls = " ; ".join(c["url"] for c in cat_items[:15])
            summary_text = (
                f"Wasalt Category Sitemap coverage for {loc_label}: {len(cat_items)} category URLs "
                f"across {len(districts)} districts. Property types covered: {type_summary}. "
                f"Districts in {city}: {', '.join(districts[:45])}. "
                f"Sample category URLs: {sample_urls}"
            )
            chunks = split_text_chunks(summary_text, target_chars=800)
            for idx, ch in enumerate(chunks, start=1):
                records.append({
                    "id": f"wasalt-city-{city.lower().replace(' ', '-')}-{idx}",
                    "source": "Wasalt",
                    "category": "category_sitemap_en_sa.xml.gz",
                    "location": loc_label,
                    "title": f"Wasalt {city} Districts & Property Categories (Part {idx}/{len(chunks)})",
                    "text": ch,
                    "url": cat_items[0]["url"],
                })

            # One record per (property type, sale/rent) in this city, naming the exact districts
            # that have a category page, e.g. "Villas for sale in North Riyadh: Al Aqeeq, Al Arid, ..."
            by_type: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
            for c in cat_items:
                by_type[(c["property_type"], c["sale_or_rent"])].append(c)
            for (ptype, mode), type_items in sorted(by_type.items()):
                type_districts = sorted({c["district"] for c in type_items if c["district"] != "All Districts"})
                if not type_districts:
                    continue
                city_level = [c["url"] for c in type_items if c["district"] == "All Districts"]
                main_url = city_level[0] if city_level else type_items[0]["url"]
                type_text = (
                    f"Wasalt has {ptype.lower()} for {mode} in {len(type_districts)} districts of {city}: "
                    f"{', '.join(type_districts)}. Each district has its own Wasalt category page, "
                    f"for example {type_items[0]['url']}"
                )
                type_chunks = split_text_chunks(type_text, target_chars=800)
                slug = f"{city}-{ptype}-{mode}".lower().replace(" ", "-")
                for idx, ch in enumerate(type_chunks, start=1):
                    suffix = f" (Part {idx}/{len(type_chunks)})" if len(type_chunks) > 1 else ""
                    records.append({
                        "id": f"wasalt-city-type-{slug}-{idx}",
                        "source": "Wasalt",
                        "category": "category_sitemap_en_sa.xml.gz",
                        "location": loc_label,
                        "title": f"Wasalt {ptype} for {mode.title()} in {city}: Districts{suffix}",
                        "text": ch,
                        "url": main_url,
                    })

    if static_urls:
        records.append({
            "id": "wasalt-static-sitemap",
            "source": "Wasalt",
            "category": "static_sitemap_en_sa.xml.gz",
            "location": "Riyadh, Saudi Arabia",
            "title": "Wasalt Static Sitemap Pages",
            "text": f"Wasalt static sitemap ({WASALT_SITEMAPS['static']}) includes {len(static_urls)} static portal URLs: " + ", ".join(static_urls[:25]),
            "url": WASALT_SITEMAPS["static"],
        })

    logger.info("Built %d Wasalt records from public XML sitemaps.", len(records))
    return records


async def run_full_scrape(output_path: Path) -> None:
    darglobal_records = await scrape_darglobal_playwright(max_pages=250, concurrency=4)
    wasalt_records = scrape_wasalt_sitemaps(samples_per_type=35)
    combined = darglobal_records + wasalt_records

    if not combined:
        raise RuntimeError(
            "Scraper returned 0 records from both DarGlobal and Wasalt. "
            "Check network connectivity and Playwright Chromium installation."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "Replaced %s with %d fresh records (%d DarGlobal, %d Wasalt) at %s",
        output_path,
        len(combined),
        len(darglobal_records),
        len(wasalt_records),
        datetime.now(timezone.utc).isoformat(),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape DarGlobal (Playwright) and Wasalt (XML Sitemaps)")
    parser.add_argument("--output", default="app/data/scraped_data.json", help="Output JSON file path")
    args = parser.parse_args()
    asyncio.run(run_full_scrape(Path(args.output)))
