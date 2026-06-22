import requests
import pandas as pd
from tqdm import tqdm

BASE = "https://sites.biochem.umass.edu/vierlinglab"
API  = f"{BASE}/wp-json/wp/v2"

headers = {"User-Agent": "Mozilla/5.0"}

def get_all_posts():
    posts = []
    page = 1

    while True:
        r = requests.get(
            f"{API}/posts",
            params={"per_page": 100, "page": page, "_embed": 1},
            headers=headers,
            timeout=15
        )

        # WP returns 400 when you go past the last page
        if r.status_code != 200:
            break

        batch = r.json()

        if not batch:
            break

        posts.extend(batch)
        
        # Check if there are more pages
        total_pages = int(r.headers.get("X-WP-TotalPages", 1))
        print(f"Page {page}/{total_pages} — {len(posts)} posts so far")
        
        if page >= total_pages:
            break
            
        page += 1

    return posts

def extract_featured_image(post):
    """Pull featured image from the _embed data."""
    try:
        media = post["_embedded"]["wp:featuredmedia"][0]
        return media.get("source_url", "")
    except (KeyError, IndexError):
        return ""

def extract_categories(post):
    """Get category names from embedded data."""
    try:
        terms = post["_embedded"]["wp:term"]
        cats = [t["name"] for group in terms for t in group if t["taxonomy"] == "category"]
        return ", ".join(cats)
    except (KeyError, IndexError):
        return ""

raw_posts = get_all_posts()

rows = []
for p in raw_posts:
    rows.append({
        "post_title":      p["title"]["rendered"],
        "post_content":    p["content"]["rendered"],   # full HTML, ready to import
        "post_excerpt":    p["excerpt"]["rendered"],
        "post_date":       p["date"],                  # ISO 8601
        "post_status":     p["status"],                # publish / draft etc.
        "post_name":       p["slug"],                  # URL slug
        "featured_image":  extract_featured_image(p),
        "categories":      extract_categories(p),
    })

df = pd.DataFrame(rows)
df.to_csv("vierling_posts.csv", index=False)
print(f"Saved {len(df)} posts")
