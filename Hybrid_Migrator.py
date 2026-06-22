"""
vierling_migrator.py
────────────────────
WordPress → WordPress post migrator for:
  https://sites.biochem.umass.edu/vierlinglab

Strategy:
  1. Try WP REST API (/wp-json/wp/v2/posts) — clean JSON, best data
  2. Fall back to HTML archive scraping if API is blocked/disabled

Output:  vierling_posts.csv   (ready to import via WP All Import or similar)
Columns: post_title, post_content, post_excerpt, post_date, post_status,
         post_name (slug), featured_image_url, categories, tags
"""

import csv
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm

# ─── CONFIG ──────────────────────────────────────────────────────────────────

BASE        = "https://sites.biochem.umass.edu/vierlinglab"
API_BASE    = f"{BASE}/wp-json/wp/v2"
OUTPUT_CSV  = "vierling_posts.csv"
DELAY       = 0.4          # seconds between requests (be polite)
TIMEOUT     = 20           # seconds per request
YEAR_START  = 2011
YEAR_END    = 2025
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def get_with_retry(url, params=None, retries=MAX_RETRIES):
    """GET with exponential backoff on failure."""
    for attempt in range(retries):
        try:
            r = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=TIMEOUT
            )
            return r
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"  ⚠ Attempt {attempt+1} failed for {url}: {e}. Retrying in {wait}s…")
            time.sleep(wait)
    print(f"  ✗ Giving up on {url} after {retries} attempts.")
    return None


def normalise_url(url):
    """Ensure URL has no trailing double-slashes and uses https."""
    return url.strip().rstrip("/").replace("http://", "https://")


# ─── STRATEGY 1: WP REST API ─────────────────────────────────────────────────

def fetch_via_api():
    """
    Pull every published post via the WP REST API.
    Returns a list of dicts, or None if the API is unavailable.
    """
    print("\n🔌 Trying WP REST API…")

    # Quick probe
    probe = get_with_retry(f"{API_BASE}/posts", params={"per_page": 1})
    if probe is None or probe.status_code != 200:
        print("  API not reachable (status:", getattr(probe, "status_code", "N/A"), ")")
        return None

    try:
        probe.json()  # make sure it's actually JSON
    except Exception:
        print("  API returned non-JSON — falling back to scraper.")
        return None

    total_pages = int(probe.headers.get("X-WP-TotalPages", 1))
    total_posts = int(probe.headers.get("X-WP-Total", "?") or 0)
    print(f"  ✓ API live — {total_posts} posts across {total_pages} pages")

    raw = []
    for page in tqdm(range(1, total_pages + 1), desc="API pages"):
        r = get_with_retry(
            f"{API_BASE}/posts",
            params={
                "per_page": 100,
                "page":     page,
                "_embed":   1,           # pulls featured image + terms inline
                "status":   "publish",
            }
        )
        if r is None or r.status_code != 200:
            print(f"  ⚠ Skipping page {page} (status {getattr(r,'status_code','N/A')})")
            continue
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        raw.extend(batch)
        time.sleep(DELAY)

    print(f"  Fetched {len(raw)} raw posts from API")
    return [parse_api_post(p) for p in raw]


def parse_api_post(p):
    """Extract every useful field from a single WP REST post object."""

    # ── Featured image ────────────────────────────────────────────────────────
    featured_image = ""
    try:
        media = p["_embedded"]["wp:featuredmedia"][0]
        # Prefer full size, fall back to any available size
        sizes = media.get("media_details", {}).get("sizes", {})
        if sizes:
            for size in ("full", "large", "medium_large", "medium"):
                if size in sizes:
                    featured_image = sizes[size]["source_url"]
                    break
        if not featured_image:
            featured_image = media.get("source_url", "")
    except (KeyError, IndexError, TypeError):
        pass

    # ── Categories & tags ─────────────────────────────────────────────────────
    categories, tags = [], []
    try:
        for term_group in p["_embedded"].get("wp:term", []):
            for term in term_group:
                if term.get("taxonomy") == "category":
                    if term["name"] != "Uncategorized":
                        categories.append(term["name"])
                elif term.get("taxonomy") == "post_tag":
                    tags.append(term["name"])
    except (KeyError, TypeError):
        pass

    # ── Author ────────────────────────────────────────────────────────────────
    author = ""
    try:
        author = p["_embedded"]["author"][0].get("name", "")
    except (KeyError, IndexError, TypeError):
        pass

    # ── Content: strip <!-- wp:... --> block comments for clean HTML ──────────
    content_raw = p.get("content", {}).get("rendered", "")

    return {
        "post_title":         p.get("title", {}).get("rendered", ""),
        "post_content":       content_raw,
        "post_excerpt":       p.get("excerpt", {}).get("rendered", ""),
        "post_date":          p.get("date", ""),          # "2024-06-15T10:30:00"
        "post_status":        p.get("status", "publish"),
        "post_name":          p.get("slug", ""),          # URL slug
        "post_author":        author,
        "featured_image_url": featured_image,
        "categories":         ", ".join(categories),
        "tags":               ", ".join(tags),
        "original_url":       normalise_url(p.get("link", "")),
    }


# ─── STRATEGY 2: HTML SCRAPER (fallback) ─────────────────────────────────────

def fetch_via_scraper():
    """
    Crawl monthly archive pages and scrape each post.
    Returns a list of dicts.
    """
    print("\n🕸  Falling back to HTML scraper…")

    # Build every month URL from YEAR_START to YEAR_END
    archive_urls = [
        f"{BASE}/{year}/{month:02d}/"
        for year in range(YEAR_START, YEAR_END + 1)
        for month in range(1, 13)
    ]

    seen_urls  = set()
    post_urls  = []

    # ── Pass 1: collect post URLs from archive pages ──────────────────────────
    print("  Pass 1: scanning archive pages…")
    for archive_url in tqdm(archive_urls, desc="Archives"):
        r = get_with_retry(archive_url)
        if r is None or r.status_code != 200:
            continue

        soup = BeautifulSoup(r.text, "lxml")
        articles = soup.select("article")

        if not articles:                          # empty month — skip quietly
            continue

        for article in articles:
            # Target the post permalink specifically (in the heading)
            link_tag = article.select_one(
                ".entry-title a, h1.entry-title a, h2.entry-title a, "
                "h1 a, h2 a, header a"
            )
            if not link_tag:
                link_tag = article.find("a")      # last resort

            if not link_tag:
                continue

            href = link_tag.get("href", "").strip()
            if not href or href in seen_urls:
                continue

            seen_urls.add(href)
            post_urls.append(href)

        time.sleep(DELAY)

    print(f"  Found {len(post_urls)} unique post URLs")

    # ── Pass 2: scrape each post page ─────────────────────────────────────────
    posts = []
    failed = []

    print("  Pass 2: scraping individual posts…")
    for url in tqdm(post_urls, desc="Posts"):
        r = get_with_retry(url)
        if r is None or r.status_code != 200:
            failed.append(url)
            continue

        post = scrape_post_page(r.text, url)
        if post:
            posts.append(post)

        time.sleep(DELAY)

    if failed:
        with open("failed_urls.txt", "w") as f:
            f.write("\n".join(failed))
        print(f"  ⚠ {len(failed)} posts failed — saved to failed_urls.txt")

    return posts


def scrape_post_page(html, url):
    """Parse a single post HTML page into a dict."""
    soup = BeautifulSoup(html, "lxml")

    # Title
    title_tag = (
        soup.select_one(".entry-title") or
        soup.find("h1") or
        soup.find("title")
    )
    title = title_tag.get_text(strip=True) if title_tag else ""

    # Date
    post_date = ""
    time_tag = soup.find("time")
    if time_tag:
        post_date = time_tag.get("datetime", time_tag.get_text(strip=True))

    # Content HTML
    content_el = (
        soup.select_one(".entry-content") or
        soup.select_one(".post-content") or
        soup.select_one("article")
    )
    content_html = str(content_el) if content_el else ""

    # Excerpt — first <p> inside content
    excerpt = ""
    if content_el:
        first_p = content_el.find("p")
        if first_p:
            excerpt = first_p.get_text(strip=True)[:300]

    # Featured image — og:image is the most reliable source
    featured_image = ""
    og = soup.find("meta", property="og:image")
    if og:
        featured_image = og.get("content", "")
    elif content_el:
        img = content_el.find("img")
        if img:
            featured_image = img.get("src", "")

    # Categories / tags from post meta
    categories, tags = [], []
    for a in soup.select(".cat-links a, .entry-categories a"):
        categories.append(a.get_text(strip=True))
    for a in soup.select(".tags-links a, .entry-tags a"):
        tags.append(a.get_text(strip=True))

    # Author
    author = ""
    author_tag = soup.select_one(".author.vcard a, .entry-author a, [rel='author']")
    if author_tag:
        author = author_tag.get_text(strip=True)

    # Slug from URL
    slug = url.rstrip("/").split("/")[-1]

    return {
        "post_title":         title,
        "post_content":       content_html,
        "post_excerpt":       excerpt,
        "post_date":          post_date,
        "post_status":        "publish",
        "post_name":          slug,
        "post_author":        author,
        "featured_image_url": featured_image,
        "categories":         ", ".join(categories),
        "tags":               ", ".join(tags),
        "original_url":       normalise_url(url),
    }


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print(f"🚀 Vierling Lab Post Migrator")
    print(f"   Source: {BASE}")
    print(f"   Output: {OUTPUT_CSV}\n")

    # Try API first; fall back to scraper
    posts = fetch_via_api()

    if not posts:
        posts = fetch_via_scraper()

    if not posts:
        print("\n✗ No posts extracted. Check the site URL and your connection.")
        return

    df = pd.DataFrame(posts)

    # Deduplicate on slug (most stable unique key)
    before = len(df)
    df.drop_duplicates(subset=["post_name"], keep="first", inplace=True)
    dupes = before - len(df)
    if dupes:
        print(f"  Removed {dupes} duplicate slugs")

    # Sort chronologically
    df.sort_values("post_date", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Save — quoting=QUOTE_ALL ensures content HTML doesn't break CSV parsing
    df.to_csv(
        OUTPUT_CSV,
        index=False,
        quoting=csv.QUOTE_ALL,
        encoding="utf-8-sig"        # BOM so Excel opens it cleanly too
    )

    print(f"\n✅ Saved {len(df)} posts → {OUTPUT_CSV}")
    print(f"   Columns: {', '.join(df.columns)}")

    # Quick sanity preview
    print("\n── First 3 posts ──")
    for _, row in df.head(3).iterrows():
        print(f"  [{row['post_date'][:10]}] {row['post_title'][:70]}")
        print(f"    slug={row['post_name']}  image={'✓' if row['featured_image_url'] else '✗'}")


if __name__ == "__main__":
    main()
