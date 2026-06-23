"""
Hybrid_migrator_xml.py
────────────────────
WordPress → WordPress full migrator for:
  https://sites.biochem.umass.edu/vierlinglab

OUTPUT: vierling_export.xml  (WXR format — native WordPress eXtended RSS)
        images/              (every image downloaded locally)
        failed_urls.txt      (posts that failed, if any)
        failed_images.txt    (images that failed, if any)

IMPORT: WordPress Admin → Tools → Import → WordPress → Upload vierling_export.xml
        The built-in importer handles everything: posts, content, dates,
        slugs, categories, tags, authors, featured images, custom fields.

STRATEGY:
  1. WP REST API  (/wp-json/wp/v2/posts)  — preferred, clean JSON
  2. HTML scraper (monthly archives)      — automatic fallback

IMAGE SAFETY:
  Every image (featured + inline) is downloaded to images/<slug>/.
  The WXR encodes the original remote URL so WordPress can re-attach
  the featured image on import. Inline images inside content keep
  their original URLs so WordPress fetches them during import too.
  Local copies are your permanent offline backup if the old site dies.
"""

import os
import re
import sys
import time
import hashlib
import requests
import textwrap
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape as xml_escape
from bs4 import BeautifulSoup
from tqdm import tqdm

# ─── CONFIG ──────────────────────────────────────────────────────────────────

BASE          = "https://sites.biochem.umass.edu/vierlinglab"
NEW_SITE_URL  = "https://yournewsite.com"          # ← change before importing
API_BASE      = f"{BASE}/wp-json/wp/v2"
OUTPUT_XML    = "vierling_export.xml"
IMAGES_DIR    = Path("images")
DELAY         = 0.4
TIMEOUT       = 20
YEAR_START    = 2011
YEAR_END      = 2026
MAX_RETRIES   = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tiff"}

# ─── SETUP ───────────────────────────────────────────────────────────────────

IMAGES_DIR.mkdir(exist_ok=True)
failed_images: list[str] = []
failed_urls:   list[str] = []

# ─── HTTP HELPERS ─────────────────────────────────────────────────────────────

def get_with_retry(url, params=None, stream=False):
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(
                url, params=params, headers=HEADERS,
                timeout=TIMEOUT, stream=stream
            )
            return r
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"  ⚠ Attempt {attempt+1} failed ({url}): {e}. Retry in {wait}s…")
            time.sleep(wait)
    print(f"  ✗ Gave up on {url}")
    return None


def normalise_url(url: str) -> str:
    return url.strip().rstrip("/").replace("http://", "https://")

# ─── IMAGE DOWNLOAD ──────────────────────────────────────────────────────────

def safe_filename(url: str) -> str:
    parsed   = urlparse(url)
    basename = os.path.basename(parsed.path)
    name, ext = os.path.splitext(basename)
    if not ext or ext.lower() not in IMAGE_EXTS:
        ext = ".jpg"
    short_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"{name}_{short_hash}{ext}"


def download_image(url: str, dest_folder: Path) -> str:
    """Download image → dest_folder. Returns local path or '' on failure."""
    if not url or not url.startswith("http"):
        return ""

    filename  = safe_filename(url)
    dest_path = dest_folder / filename

    if dest_path.exists():
        return str(dest_path)

    r = get_with_retry(url, stream=True)
    if r is None or r.status_code != 200:
        failed_images.append(url)
        return ""

    # Correct extension from Content-Type
    ct  = r.headers.get("Content-Type", "")
    ext = dest_path.suffix
    if   "jpeg" in ct or "jpg" in ct: ext = ".jpg"
    elif "png"  in ct:                ext = ".png"
    elif "gif"  in ct:                ext = ".gif"
    elif "webp" in ct:                ext = ".webp"
    elif "svg"  in ct:                ext = ".svg"

    dest_path = dest_folder / (dest_path.stem + ext)

    try:
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return str(dest_path)
    except OSError as e:
        print(f"  ✗ Write failed {dest_path}: {e}")
        failed_images.append(url)
        return ""


def download_content_images(content_html: str, slug: str, base_url: str = "") -> str:
    """Download every <img> in content HTML. Returns HTML unchanged (original URLs kept for WXR)."""
    if not content_html:
        return content_html

    folder = IMAGES_DIR / slug
    folder.mkdir(exist_ok=True)

    soup = BeautifulSoup(content_html, "lxml")
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif not src.startswith("http"):
            src = urljoin(base_url or BASE, src)
        # Download for local backup — do NOT rewrite src in WXR
        # WordPress importer will fetch from the original URL on import
        download_image(src, folder)

    return content_html   # return original HTML with original URLs intact

# ─── XML / WXR HELPERS ───────────────────────────────────────────────────────

def cdata(text: str) -> str:
    """Wrap text in CDATA, escaping any ]]> sequences inside."""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def wxr_date(iso: str) -> tuple[str, str]:
    """
    Convert ISO 8601 date string to two WXR formats:
      pub_date  → "Mon, 15 Jun 2020 10:30:00 +0000"
      post_date → "2020-06-15 10:30:00"
    """
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc)

    pub  = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    post = dt.strftime("%Y-%m-%d %H:%M:%S")
    return pub, post


def collect_terms(posts: list[dict]) -> tuple[list, list]:
    """Gather unique categories and tags across all posts."""
    cats = {}
    tags = {}
    for p in posts:
        for c in p.get("categories_list", []):
            slug = re.sub(r"[^a-z0-9-]", "-", c.lower()).strip("-")
            cats[slug] = c
        for t in p.get("tags_list", []):
            slug = re.sub(r"[^a-z0-9-]", "-", t.lower()).strip("-")
            tags[slug] = t
    return list(cats.items()), list(tags.items())


def collect_authors(posts: list[dict]) -> list[str]:
    seen = set()
    out  = []
    for p in posts:
        a = p.get("post_author", "").strip()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out or ["admin"]

# ─── WXR BUILDER ─────────────────────────────────────────────────────────────

def build_wxr(posts: list[dict], site_url: str = NEW_SITE_URL) -> str:
    """Assemble the full WXR XML string from a list of post dicts."""

    now      = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    cats, tags = collect_terms(posts)
    authors  = collect_authors(posts)

    lines = []
    a = lines.append   # shorthand

    # ── RSS / channel header ──────────────────────────────────────────────────
    a('<?xml version="1.0" encoding="UTF-8" ?>')
    a('<!-- Generated by vierling_migrator.py -->')
    a('<rss version="2.0"')
    a('  xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"')
    a('  xmlns:content="http://purl.org/rss/1.0/modules/content/"')
    a('  xmlns:wfw="http://wellformedweb.org/CommentAPI/"')
    a('  xmlns:dc="http://purl.org/dc/elements/1.1/"')
    a('  xmlns:wp="http://wordpress.org/export/1.2/">')
    a('<channel>')
    a(f'  <title>{cdata("Vierling Lab")}</title>')
    a(f'  <link>{site_url}</link>')
    a(f'  <description>{cdata("Vierling Lab – UMass Biochemistry")}</description>')
    a(f'  <pubDate>{now}</pubDate>')
    a(f'  <language>en-US</language>')
    a(f'  <wp:wxr_version>1.2</wp:wxr_version>')
    a(f'  <wp:base_site_url>{site_url}</wp:base_site_url>')
    a(f'  <wp:base_blog_url>{site_url}</wp:base_blog_url>')

    # ── Authors ───────────────────────────────────────────────────────────────
    for i, author in enumerate(authors, start=1):
        login = re.sub(r"[^a-z0-9_]", "_", author.lower())[:60]
        a(f'  <wp:author>')
        a(f'    <wp:author_id>{i}</wp:author_id>')
        a(f'    <wp:author_login>{cdata(login)}</wp:author_login>')
        a(f'    <wp:author_email>{cdata(login + "@placeholder.local")}</wp:author_email>')
        a(f'    <wp:author_display_name>{cdata(author)}</wp:author_display_name>')
        a(f'    <wp:author_first_name>{cdata("")}</wp:author_first_name>')
        a(f'    <wp:author_last_name>{cdata("")}</wp:author_last_name>')
        a(f'  </wp:author>')

    # ── Categories ────────────────────────────────────────────────────────────
    for slug, name in cats:
        a(f'  <wp:category>')
        a(f'    <wp:term_id></wp:term_id>')
        a(f'    <wp:category_nicename>{cdata(slug)}</wp:category_nicename>')
        a(f'    <wp:category_parent>{cdata("")}</wp:category_parent>')
        a(f'    <wp:cat_name>{cdata(name)}</wp:cat_name>')
        a(f'  </wp:category>')

    # ── Tags ──────────────────────────────────────────────────────────────────
    for slug, name in tags:
        a(f'  <wp:tag>')
        a(f'    <wp:term_id></wp:term_id>')
        a(f'    <wp:tag_slug>{cdata(slug)}</wp:tag_slug>')
        a(f'    <wp:tag_name>{cdata(name)}</wp:tag_name>')
        a(f'  </wp:tag>')

    # ── Posts ─────────────────────────────────────────────────────────────────
    author_index = {a: i+1 for i, a in enumerate(authors)}

    for post_id, p in enumerate(posts, start=1):
        pub_date, post_date = wxr_date(p.get("post_date", ""))
        title    = p.get("post_title", "Untitled")
        slug     = p.get("post_name",  "post")
        content  = p.get("post_content", "")
        excerpt  = p.get("post_excerpt", "")
        status   = p.get("post_status",  "publish")
        author   = p.get("post_author",  authors[0])
        feat_url = p.get("featured_image_url", "")
        orig_url = p.get("original_url", "")

        author_login = re.sub(r"[^a-z0-9_]", "_", author.lower())[:60]
        post_url     = f"{site_url}/?p={post_id}"

        a(f'  <item>')
        a(f'    <title>{cdata(title)}</title>')
        a(f'    <link>{post_url}</link>')
        a(f'    <pubDate>{pub_date}</pubDate>')
        a(f'    <dc:creator>{cdata(author_login)}</dc:creator>')
        a(f'    <guid isPermaLink="false">{post_url}</guid>')
        a(f'    <description></description>')
        a(f'    <content:encoded>{cdata(content)}</content:encoded>')
        a(f'    <excerpt:encoded>{cdata(excerpt)}</excerpt:encoded>')
        a(f'    <wp:post_id>{post_id}</wp:post_id>')
        a(f'    <wp:post_date>{cdata(post_date)}</wp:post_date>')
        a(f'    <wp:post_date_gmt>{cdata(post_date)}</wp:post_date_gmt>')
        a(f'    <wp:comment_status>{cdata("closed")}</wp:comment_status>')
        a(f'    <wp:ping_status>{cdata("closed")}</wp:ping_status>')
        a(f'    <wp:post_name>{cdata(slug)}</wp:post_name>')
        a(f'    <wp:status>{cdata(status)}</wp:status>')
        a(f'    <wp:post_parent>0</wp:post_parent>')
        a(f'    <wp:menu_order>0</wp:menu_order>')
        a(f'    <wp:post_type>{cdata("post")}</wp:post_type>')
        a(f'    <wp:post_password></wp:post_password>')
        a(f'    <wp:is_sticky>0</wp:is_sticky>')

        # Original URL as custom field — useful for redirect mapping
        if orig_url:
            a(f'    <wp:postmeta>')
            a(f'      <wp:meta_key>{cdata("_original_url")}</wp:meta_key>')
            a(f'      <wp:meta_value>{cdata(orig_url)}</wp:meta_value>')
            a(f'    </wp:postmeta>')

        # Featured image URL as _thumbnail_url custom field
        # WordPress importer reads this and attaches the media on import
        if feat_url:
            a(f'    <wp:postmeta>')
            a(f'      <wp:meta_key>{cdata("_thumbnail_url")}</wp:meta_key>')
            a(f'      <wp:meta_value>{cdata(feat_url)}</wp:meta_value>')
            a(f'    </wp:postmeta>')

        # Categories
        for cat_slug, cat_name in [
            (re.sub(r"[^a-z0-9-]", "-", c.lower()).strip("-"), c)
            for c in p.get("categories_list", [])
        ]:
            a(f'    <category domain="category" nicename="{xml_escape(cat_slug)}">'
              f'{cdata(cat_name)}</category>')

        # Tags
        for tag_slug, tag_name in [
            (re.sub(r"[^a-z0-9-]", "-", t.lower()).strip("-"), t)
            for t in p.get("tags_list", [])
        ]:
            a(f'    <category domain="post_tag" nicename="{xml_escape(tag_slug)}">'
              f'{cdata(tag_name)}</category>')

        a(f'  </item>')

    a('</channel>')
    a('</rss>')

    return "\n".join(lines)

# ─── STRATEGY 1: WP REST API ─────────────────────────────────────────────────

def fetch_via_api() -> list[dict] | None:
    print("\n🔌 Trying WP REST API…")

    probe = get_with_retry(f"{API_BASE}/posts", params={"per_page": 1})
    if probe is None or probe.status_code != 200:
        print(f"  API not reachable (status: {getattr(probe, 'status_code', 'N/A')})")
        return None
    try:
        probe.json()
    except Exception:
        print("  API returned non-JSON — falling back to scraper.")
        return None

    total_pages = int(probe.headers.get("X-WP-TotalPages", 1))
    total_posts = int(probe.headers.get("X-WP-Total", 0) or 0)
    print(f"  ✓ API live — {total_posts} posts across {total_pages} pages")

    raw = []
    for page in tqdm(range(1, total_pages + 1), desc="API pages"):
        r = get_with_retry(
            f"{API_BASE}/posts",
            params={"per_page": 100, "page": page, "_embed": 1, "status": "publish"}
        )
        if r is None or r.status_code != 200:
            print(f"  ⚠ Skipping page {page}")
            continue
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        raw.extend(batch)
        time.sleep(DELAY)

    print(f"  Fetched {len(raw)} raw posts")
    return [parse_api_post(p) for p in raw]


def parse_api_post(p: dict) -> dict:
    slug = p.get("slug", "untitled")

    # Featured image
    featured_image_url = ""
    try:
        media = p["_embedded"]["wp:featuredmedia"][0]
        sizes = media.get("media_details", {}).get("sizes", {})
        for size in ("full", "large", "medium_large", "medium"):
            if size in sizes:
                featured_image_url = sizes[size]["source_url"]
                break
        if not featured_image_url:
            featured_image_url = media.get("source_url", "")
    except (KeyError, IndexError, TypeError):
        pass

    # Download featured image locally (backup)
    folder = IMAGES_DIR / slug
    folder.mkdir(exist_ok=True)
    download_image(featured_image_url, folder)

    # Content — download inline images locally, keep original URLs in XML
    content_raw = p.get("content", {}).get("rendered", "")
    download_content_images(content_raw, slug)

    # Categories and tags
    categories, tags = [], []
    try:
        for term_group in p["_embedded"].get("wp:term", []):
            for term in term_group:
                if term.get("taxonomy") == "category" and term["name"] != "Uncategorized":
                    categories.append(term["name"])
                elif term.get("taxonomy") == "post_tag":
                    tags.append(term["name"])
    except (KeyError, TypeError):
        pass

    # Author
    author = ""
    try:
        author = p["_embedded"]["author"][0].get("name", "")
    except (KeyError, IndexError, TypeError):
        pass

    return {
        "post_title":        p.get("title", {}).get("rendered", ""),
        "post_content":      content_raw,
        "post_excerpt":      p.get("excerpt", {}).get("rendered", ""),
        "post_date":         p.get("date", ""),
        "post_status":       p.get("status", "publish"),
        "post_name":         slug,
        "post_author":       author,
        "featured_image_url": featured_image_url,
        "categories_list":   categories,
        "tags_list":         tags,
        "original_url":      normalise_url(p.get("link", "")),
    }

# ─── STRATEGY 2: HTML SCRAPER ────────────────────────────────────────────────

def fetch_via_scraper() -> list[dict]:
    print("\n🕸  Falling back to HTML scraper…")

    archive_urls = [
        f"{BASE}/{y}/{m:02d}/"
        for y in range(YEAR_START, YEAR_END + 1)
        for m in range(1, 13)
    ]

    seen, post_urls = set(), []

    print("  Pass 1: scanning archives…")
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
            ) or article.find("a")
            if not link_tag:
                continue
            href = link_tag.get("href", "").strip()
            if href and href not in seen:
                seen.add(href)
                post_urls.append(href)
        time.sleep(DELAY)

    print(f"  Found {len(post_urls)} post URLs")

    posts = []
    print("  Pass 2: scraping posts…")
    for url in tqdm(post_urls, desc="Posts"):
        r = get_with_retry(url)
        if r is None or r.status_code != 200:
            failed_urls.append(url)
            continue
        post = scrape_post_page(r.text, url)
        if post:
            posts.append(post)
        time.sleep(DELAY)

    return posts


def scrape_post_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    slug = url.rstrip("/").split("/")[-1]

    title_tag = soup.select_one(".entry-title") or soup.find("h1") or soup.find("title")
    title     = title_tag.get_text(strip=True) if title_tag else ""

    post_date = ""
    time_tag  = soup.find("time")
    if time_tag:
        post_date = time_tag.get("datetime", time_tag.get_text(strip=True))

    content_el = (
        soup.select_one(".entry-content") or
        soup.select_one(".post-content")  or
        soup.select_one("article")
    )
    content_html = str(content_el) if content_el else ""

    excerpt = ""
    if content_el:
        p_tag = content_el.find("p")
        if p_tag:
            excerpt = p_tag.get_text(strip=True)[:300]

    # Featured image: og:image is most reliable
    featured_image_url = ""
    og = soup.find("meta", property="og:image")
    if og:
        featured_image_url = og.get("content", "")
    elif content_el:
        img = content_el.find("img")
        if img:
            featured_image_url = img.get("src", "")

    # Download everything locally
    folder = IMAGES_DIR / slug
    folder.mkdir(exist_ok=True)
    download_image(featured_image_url, folder)
    download_content_images(content_html, slug, base_url=url)

    categories, tags = [], []
    for a in soup.select(".cat-links a, .entry-categories a"):
        categories.append(a.get_text(strip=True))
    for a in soup.select(".tags-links a, .entry-tags a"):
        tags.append(a.get_text(strip=True))

    author = ""
    author_tag = soup.select_one(".author.vcard a, .entry-author a, [rel='author']")
    if author_tag:
        author = author_tag.get_text(strip=True)

    return {
        "post_title":         title,
        "post_content":       content_html,
        "post_excerpt":       excerpt,
        "post_date":          post_date,
        "post_status":        "publish",
        "post_name":          slug,
        "post_author":        author,
        "featured_image_url": featured_image_url,
        "categories_list":    categories,
        "tags_list":          tags,
        "original_url":       normalise_url(url),
    }

# ─── DEDUP + SORT ─────────────────────────────────────────────────────────────

def clean_posts(posts: list[dict]) -> list[dict]:
    seen  = set()
    clean = []
    for p in posts:
        slug = p.get("post_name", "")
        if slug and slug not in seen:
            seen.add(slug)
            clean.append(p)
    # Sort chronologically
    clean.sort(key=lambda p: p.get("post_date", ""))
    return clean

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("🚀 Vierling Lab WXR Migrator")
    print(f"   Source : {BASE}")
    print(f"   Output : {OUTPUT_XML}")
    print(f"   Images : {IMAGES_DIR}/\n")

    posts = fetch_via_api()
    if not posts:
        posts = fetch_via_scraper()

    if not posts:
        print("\n✗ No posts found. Check URL and network.")
        sys.exit(1)

    posts = clean_posts(posts)
    print(f"\n  {len(posts)} unique posts ready")

    # Build WXR XML
    print("  Building WXR XML…")
    wxr = build_wxr(posts)

    with open(OUTPUT_XML, "w", encoding="utf-8") as f:
        f.write(wxr)

    # Write failure logs
    if failed_urls:
        with open("failed_urls.txt", "w") as f:
            f.write("\n".join(failed_urls))
        print(f"  ⚠ {len(failed_urls)} posts failed → failed_urls.txt")

    if failed_images:
        with open("failed_images.txt", "w") as f:
            f.write("\n".join(failed_images))
        print(f"  ⚠ {len(failed_images)} images failed → failed_images.txt")

    total_imgs = sum(1 for _ in IMAGES_DIR.rglob("*") if _.is_file())
    xml_size   = Path(OUTPUT_XML).stat().st_size / 1024

    print(f"\n✅ Done!")
    print(f"   WXR file  : {OUTPUT_XML}  ({xml_size:.1f} KB)")
    print(f"   Posts     : {len(posts)}")
    print(f"   Images    : {total_imgs} files in {IMAGES_DIR}/")
    print()
    print("── How to import ─────────────────────────────────────────────")
    print("  1. WordPress Admin → Tools → Import → WordPress")
    print("  2. Upload vierling_export.xml")
    print("  3. Map authors → assign to existing user or create new")
    print("  4. Check 'Download and import file attachments'")
    print("  5. Click 'Submit'")
    print("──────────────────────────────────────────────────────────────")
    print()
    print("── First 3 posts ──────────────────────────────────────────────")
    for p in posts[:3]:
        date  = str(p.get("post_date", ""))[:10]
        title = str(p.get("post_title", ""))[:65]
        img   = "✓" if p.get("featured_image_url") else "✗"
        cats  = ", ".join(p.get("categories_list", []))
        print(f"  [{date}] {title}")
        print(f"    slug={p['post_name']}  img={img}  cats={cats or 'none'}")


if __name__ == "__main__":
    main()
