"""
vierling_migrator.py
────────────────────
WordPress → WordPress post migrator for:
  https://sites.biochem.umass.edu/vierlinglab

Strategy:
  1. Try WP REST API (/wp-json/wp/v2/posts) — clean JSON, best data
  2. Fall back to HTML archive scraping if API is blocked/disabled

Image handling:
  - Downloads EVERY image referenced in post content + featured images
  - Saves them to ./images/<slug>/ locally
  - Rewrites all <img src="..."> in post_content to use local paths
  - CSV column featured_image_local points to the local file

Output files:
  vierling_posts.csv   — ready to import via WP All Import
  images/              — all downloaded images, organised by post slug
  failed_urls.txt      — any posts that couldn't be fetched (if any)
  failed_images.txt    — any images that couldn't be downloaded (if any)
"""

import csv
import os
import re
import time
import hashlib
import mimetypes
import requests
import pandas as pd
from pathlib import Path
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from tqdm import tqdm

# ─── CONFIG ──────────────────────────────────────────────────────────────────

BASE         = "https://sites.biochem.umass.edu/vierlinglab"
API_BASE     = f"{BASE}/wp-json/wp/v2"
OUTPUT_CSV   = "vierling_posts.csv"
IMAGES_DIR   = Path("images")
DELAY        = 0.4          # seconds between requests (be polite)
TIMEOUT      = 20           # seconds per request
YEAR_START   = 2011
YEAR_END     = 2025
MAX_RETRIES  = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Image extensions we bother saving
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tiff"}

# ─── SETUP ───────────────────────────────────────────────────────────────────

IMAGES_DIR.mkdir(exist_ok=True)
failed_images = []

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def get_with_retry(url, params=None, retries=MAX_RETRIES, stream=False):
    """GET with exponential backoff on failure."""
    for attempt in range(retries):
        try:
            r = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=TIMEOUT,
                stream=stream
            )
            return r
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"  ⚠ Attempt {attempt+1} failed for {url}: {e}. Retrying in {wait}s…")
            time.sleep(wait)
    print(f"  ✗ Giving up on {url} after {retries} attempts.")
    return None


def normalise_url(url):
    """Strip trailing slashes, enforce https."""
    return url.strip().rstrip("/").replace("http://", "https://")


def safe_filename(url):
    """
    Turn a URL into a safe local filename, preserving the original name
    where possible and appending a short hash to avoid collisions.
    """
    parsed   = urlparse(url)
    basename = os.path.basename(parsed.path)          # e.g. photo.jpg
    name, ext = os.path.splitext(basename)

    # If extension is missing or not an image ext, guess from content-type later
    if not ext or ext.lower() not in IMAGE_EXTS:
        ext = ".jpg"                                   # safe default

    # Short hash to avoid name collisions across posts
    short_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"{name}_{short_hash}{ext}"


def download_image(url, dest_folder: Path):
    """
    Download a single image to dest_folder.
    Returns the local file path (str) on success, or "" on failure.
    """
    if not url or not url.startswith("http"):
        return ""

    filename = safe_filename(url)
    dest_path = dest_folder / filename

    # Skip if already downloaded (re-run safe)
    if dest_path.exists():
        return str(dest_path)

    r = get_with_retry(url, stream=True)
    if r is None or r.status_code != 200:
        failed_images.append(url)
        return ""

    # Fix extension from Content-Type if needed
    content_type = r.headers.get("Content-Type", "")
    if "jpeg" in content_type or "jpg" in content_type:
        ext = ".jpg"
    elif "png" in content_type:
        ext = ".png"
    elif "gif" in content_type:
        ext = ".gif"
    elif "webp" in content_type:
        ext = ".webp"
    elif "svg" in content_type:
        ext = ".svg"
    else:
        ext = os.path.splitext(dest_path)[1] or ".jpg"

    # Re-apply correct extension
    dest_path = dest_folder / (dest_path.stem + ext)

    try:
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return str(dest_path)
    except OSError as e:
        print(f"  ✗ Could not write {dest_path}: {e}")
        failed_images.append(url)
        return ""


def download_all_images_in_content(content_html, slug, base_url=""):
    """
    Find every <img src> in the content HTML.
    Download each image to images/<slug>/.
    Rewrite the src to the local path.
    Returns the rewritten HTML.
    """
    if not content_html:
        return content_html

    post_img_dir = IMAGES_DIR / slug
    post_img_dir.mkdir(exist_ok=True)

    soup = BeautifulSoup(content_html, "lxml")
    imgs = soup.find_all("img")

    for img in imgs:
        src = img.get("src", "")
        if not src:
            continue

        # Make absolute if relative
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = urljoin(base_url or BASE, src)
        elif not src.startswith("http"):
            src = urljoin(base_url or BASE, src)

        local_path = download_image(src, post_img_dir)
        if local_path:
            img["src"] = local_path
            # Also fix srcset if present
            if img.get("srcset"):
                img["srcset"] = ""   # clear it — local paths don't need srcset

    return str(soup)


# ─── STRATEGY 1: WP REST API ─────────────────────────────────────────────────

def fetch_via_api():
    """
    Pull every published post via the WP REST API.
    Returns a list of dicts, or None if the API is unavailable.
    """
    print("\n🔌 Trying WP REST API…")

    probe = get_with_retry(f"{API_BASE}/posts", params={"per_page": 1})
    if probe is None or probe.status_code != 200:
        print("  API not reachable (status:", getattr(probe, "status_code", "N/A"), ")")
        return None

    try:
        probe.json()
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
                "_embed":   1,
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

    slug = p.get("slug", "untitled")

    # ── Featured image ────────────────────────────────────────────────────────
    featured_image_url = ""
    try:
        media  = p["_embedded"]["wp:featuredmedia"][0]
        sizes  = media.get("media_details", {}).get("sizes", {})
        if sizes:
            for size in ("full", "large", "medium_large", "medium"):
                if size in sizes:
                    featured_image_url = sizes[size]["source_url"]
                    break
        if not featured_image_url:
            featured_image_url = media.get("source_url", "")
    except (KeyError, IndexError, TypeError):
        pass

    # Download featured image
    post_img_dir = IMAGES_DIR / slug
    post_img_dir.mkdir(exist_ok=True)
    featured_image_local = download_image(featured_image_url, post_img_dir)

    # ── Content — download all inline images & rewrite srcs ──────────────────
    content_raw      = p.get("content", {}).get("rendered", "")
    content_rewritten = download_all_images_in_content(content_raw, slug)

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

    return {
        "post_title":            p.get("title", {}).get("rendered", ""),
        "post_content":          content_rewritten,         # HTML with local img srcs
        "post_excerpt":          p.get("excerpt", {}).get("rendered", ""),
        "post_date":             p.get("date", ""),
        "post_status":           p.get("status", "publish"),
        "post_name":             slug,
        "post_author":           author,
        "featured_image_url":    featured_image_url,        # original URL (backup)
        "featured_image_local":  featured_image_local,      # local file path
        "categories":            ", ".join(categories),
        "tags":                  ", ".join(tags),
        "original_url":          normalise_url(p.get("link", "")),
    }


# ─── STRATEGY 2: HTML SCRAPER (fallback) ─────────────────────────────────────

def fetch_via_scraper():
    """
    Crawl monthly archive pages and scrape each post.
    Returns a list of dicts.
    """
    print("\n🕸  Falling back to HTML scraper…")

    archive_urls = [
        f"{BASE}/{year}/{month:02d}/"
        for year in range(YEAR_START, YEAR_END + 1)
        for month in range(1, 13)
    ]

    seen_urls = set()
    post_urls = []

    print("  Pass 1: scanning archive pages…")
    for archive_url in tqdm(archive_urls, desc="Archives"):
        r = get_with_retry(archive_url)
        if r is None or r.status_code != 200:
            continue

        soup     = BeautifulSoup(r.text, "lxml")
        articles = soup.select("article")

        if not articles:
            continue

        for article in articles:
            link_tag = article.select_one(
                ".entry-title a, h1.entry-title a, h2.entry-title a, h1 a, h2 a, header a"
            )
            if not link_tag:
                link_tag = article.find("a")
            if not link_tag:
                continue

            href = link_tag.get("href", "").strip()
            if not href or href in seen_urls:
                continue

            seen_urls.add(href)
            post_urls.append(href)

        time.sleep(DELAY)

    print(f"  Found {len(post_urls)} unique post URLs")

    posts  = []
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
    slug = url.rstrip("/").split("/")[-1]

    # Title
    title_tag = (
        soup.select_one(".entry-title") or
        soup.find("h1") or
        soup.find("title")
    )
    title = title_tag.get_text(strip=True) if title_tag else ""

    # Date
    post_date = ""
    time_tag  = soup.find("time")
    if time_tag:
        post_date = time_tag.get("datetime", time_tag.get_text(strip=True))

    # Content
    content_el = (
        soup.select_one(".entry-content") or
        soup.select_one(".post-content") or
        soup.select_one("article")
    )
    content_html = str(content_el) if content_el else ""

    # Excerpt
    excerpt = ""
    if content_el:
        first_p = content_el.find("p")
        if first_p:
            excerpt = first_p.get_text(strip=True)[:300]

    # Featured image — og:image first, then first img in content
    featured_image_url = ""
    og = soup.find("meta", property="og:image")
    if og:
        featured_image_url = og.get("content", "")
    elif content_el:
        img = content_el.find("img")
        if img:
            featured_image_url = img.get("src", "")

    # Download featured image
    post_img_dir = IMAGES_DIR / slug
    post_img_dir.mkdir(exist_ok=True)
    featured_image_local = download_image(featured_image_url, post_img_dir)

    # Download all inline images & rewrite content srcs
    content_rewritten = download_all_images_in_content(content_html, slug, base_url=url)

    # Categories / tags
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

    return {
        "post_title":            title,
        "post_content":          content_rewritten,
        "post_excerpt":          excerpt,
        "post_date":             post_date,
        "post_status":           "publish",
        "post_name":             slug,
        "post_author":           author,
        "featured_image_url":    featured_image_url,
        "featured_image_local":  featured_image_local,
        "categories":            ", ".join(categories),
        "tags":                  ", ".join(tags),
        "original_url":          normalise_url(url),
    }


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("🚀 Vierling Lab Post Migrator  (with image download)")
    print(f"   Source : {BASE}")
    print(f"   Output : {OUTPUT_CSV}")
    print(f"   Images : {IMAGES_DIR}/\n")

    posts = fetch_via_api()
    if not posts:
        posts = fetch_via_scraper()

    if not posts:
        print("\n✗ No posts extracted. Check the site URL and your connection.")
        return

    df = pd.DataFrame(posts)

    # Deduplicate on slug
    before = len(df)
    df.drop_duplicates(subset=["post_name"], keep="first", inplace=True)
    dupes = before - len(df)
    if dupes:
        print(f"  Removed {dupes} duplicate slugs")

    # Sort chronologically
    df.sort_values("post_date", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Save CSV — QUOTE_ALL keeps HTML content safe
    df.to_csv(
        OUTPUT_CSV,
        index=False,
        quoting=csv.QUOTE_ALL,
        encoding="utf-8-sig"
    )

    # Log any failed image downloads
    if failed_images:
        with open("failed_images.txt", "w") as f:
            f.write("\n".join(failed_images))
        print(f"\n  ⚠ {len(failed_images)} images failed — saved to failed_images.txt")

    # Count downloaded images
    total_imgs = sum(1 for _ in IMAGES_DIR.rglob("*") if _.is_file())

    print(f"\n✅ Done!")
    print(f"   Posts saved   : {len(df)}  →  {OUTPUT_CSV}")
    print(f"   Images saved  : {total_imgs}  →  {IMAGES_DIR}/")
    print(f"   Columns       : {', '.join(df.columns)}")

    print("\n── Sample (first 3 posts) ──")
    for _, row in df.head(3).iterrows():
        date  = str(row["post_date"])[:10]
        title = str(row["post_title"])[:65]
        img   = "✓" if row["featured_image_local"] else "✗"
        print(f"  [{date}] {title}")
        print(f"    slug={row['post_name']}  featured_img={img}")


if __name__ == "__main__":
    main()
