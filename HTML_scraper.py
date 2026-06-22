import requests
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm
from datetime import datetime

BASE = "https://sites.biochem.umass.edu/<LABNAME>"

posts = []
seen = set()
months = []

for year in range(2011, 2026):
    for month in range(1, 13):
        months.append(f"{BASE}/{year}/{month:02d}/")

headers = {
    "User-Agent": "Mozilla/5.0"
}

for archive_url in tqdm(months):

    r = requests.get(archive_url, headers=headers, timeout=15)

    if r.status_code != 200:
        continue

    soup = BeautifulSoup(r.text, "lxml")

    for article in soup.select("article"):

        link = article.find("a")

        if not link:
            continue

        post_url = link.get("href")

        if not post_url:
            continue

        if post_url in seen:
            continue

        seen.add(post_url)

        try:
            post_page = requests.get(
                post_url,
                headers=headers,
                timeout=15
            )

            post_soup = BeautifulSoup(post_page.text, "lxml")

            title = post_soup.find("h1")

            title = title.get_text(strip=True) if title else ""

            time_tag = post_soup.find("time")

            post_date = ""

            if time_tag:
                post_date = time_tag.get("datetime", "")

            content = post_soup.select_one(".entry-content")

            content_html = str(content) if content else ""

            image = ""

            img = content.find("img") if content else None

            if img:
                image = img.get("src", "")

            posts.append({
                "post_title": title,
                "post_content": content_html,
                "post_date": post_date,
                "featured_image": image,
                "post_status": "publish"
            })

        except Exception as e:
            print(f"Error: {post_url}")
            print(e)

df = pd.DataFrame(posts)

df.drop_duplicates(
    subset=["post_title"],
    inplace=True
)

df.to_csv(
    "vierling_news.csv",
    index=False
)

print(f"Saved {len(df)} posts")
