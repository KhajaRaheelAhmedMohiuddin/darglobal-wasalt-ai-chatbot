# Data provenance, coverage and limitations

**Scrape date:** 18 November 2026
**Output:** `app/data/scraped_data.json` (1,804 records, each with its source URL)
**Produced by:** `app/scraper.py` (`docker compose --profile scraper run --rm scraper`). The file in this repository is the unmodified output of that command; no records were written or edited by hand.

## 1. DarGlobal (darglobal.co.uk)

**Method**

1. Read `https://darglobal.co.uk/sitemap.xml` (about 480 URLs).
2. Keep English pages in these sections: `projects`, top-level project pages (for example `/the-astera`, `/dg1`, `/trump-tower-jeddah`), `partners`, `one-of-one`, `press`, `insights`, `about`, `why-invest`, `investor`, `faq`. Project pages are scraped first.
3. Load each page in headless Chromium (Playwright), 4 pages at a time. The site only renders its content after JavaScript runs (it uses Imperva/Incapsula), so a plain HTTP client receives an empty page.
4. Strip scripts, navigation, header and footer; split the text into chunks of about 800 characters on sentence boundaries.
5. Remove repeated site boilerplate: any chunk that appears on more than 3 pages (contact forms, menus) and exact duplicates.
6. Tag each chunk with a location inferred from the page (for example "Al Marjan Island, Ras Al Khaimah, UAE").

**Coverage**

| Section | Chunks |
|---|---:|
| Press releases | 345 |
| Projects (index and project pages) | 277 |
| Insights | 88 |
| Brand partners | 75 |
| One-of-one residences | 25 |
| FAQ, investor, why-invest, about | 25 |
| **Total (163 pages)** | **835** |

**Not included:** about 265 blog posts (general marketing articles), Arabic pages, and careers, payment, legal and thank-you pages.

## 2. Wasalt (wasalt.sa)

**Why sitemaps:** Wasalt's HTML pages are protected by a Cloudflare challenge that blocks automated browsers. This project does **not** attempt to bypass that protection. Instead it uses the public XML sitemaps that Wasalt's own `robots.txt` lists for crawlers:

| Sitemap | URLs | Used for |
|---|---:|---|
| `cdn.wasalt.sa/sitemap/product_sitemap_en_sa.xml.gz` | 60,000 | Sale and rental listings |
| `cdn.wasalt.sa/sitemap/rental_pdp_sitemap_en_sa.xml.gz` | 2,669 | Daily rentals (chalets, apartments, studios) |
| `cdn.wasalt.sa/sitemap/category_sitemap_en_sa.xml.gz` | 8,634 | Cities, districts and property types |
| `cdn.wasalt.sa/sitemap/static_sitemap_en_sa.xml.gz` | 9 | Static pages |

(The files are served as plain XML despite the `.gz` name; the scraper detects either format.)

**What is extracted:** listing URLs encode the property type, sale/rent, size, bedrooms, street facing, street width and listing ID, for example `.../property/sale/land-500-sqm-facing-west-on-15m-width-street-5865930`. Category URLs encode property type, sale/rent, city and district, for example `.../villas-for-sale-in-north-riyadh-al-narjis`.

**What is stored:** summary records, not a copy of the marketplace:

- counts per property type and transaction (for example "Apartment for rent: 11,482 listings"), with median size and bedroom breakdown;
- per-bedroom groups (for example "3-bedroom apartments for rent") with up to 25–35 sample listing links each;
- per-city records listing the districts and property types Wasalt covers, with category-page links.
- per city and property type, the exact districts that have listings (for example "villas for sale in North Riyadh: Al Aqeeq, Al Arid, … Hitteen").

In total: 969 Wasalt records.

**Data quality notes**

- Listing URLs drop the decimal point in sizes (`apartment-14483-sqm` means 144.83 sqm). For apartments, floors and rooms, values over 1,000 sqm are divided by 100; implausible values are discarded. Medians are reported rather than averages to limit the effect of remaining errors.
- The product sitemap contains exactly 60,000 URLs, which suggests a size cap; Wasalt likely has more active listings than this.

**Not available:** prices, descriptions, photos, agent details, the city of an individual listing, auction details, and Wasalt's corporate pages. All of these are only on the Cloudflare-protected pages.

## 3. Terms of use and responsible collection

- Both websites' Terms of Use protect their content and restrict copying, republication and commercial use. This dataset was collected solely to complete a job assessment set by DarGlobal and is not intended for republication or commercial use.
- Collection respected technical access controls: no CAPTCHA or Cloudflare bypass, no logins, no personal data (no agent names or phone numbers), and a single, modest pass (at most 4 DarGlobal pages loading at a time; 4 sitemap files for Wasalt).
- Wasalt data is stored as aggregate summaries and sample links, not as a mirror of listings. Every record links back to its source so users are sent to the original website.
- For production use, Wasalt data should come through an authorised API or data feed, which would also provide prices and complete listing details.

## 4. Freshness

The data is a snapshot from the scrape date above. A missing result means "not in this snapshot", not "not available on the live website". To refresh, re-run the scraper (about 4 minutes); it replaces the data file completely.
