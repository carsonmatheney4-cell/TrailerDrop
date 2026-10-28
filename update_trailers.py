
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

FEEDS = {
    "Movie Trailers": [
        "https://www.comingsoon.net/feed",
        "https://www.joblo.com/feed/",
        "https://www.firstshowing.net/feed/",
    ],
    "Gaming": [
        "https://www.nintendo.com/us/whatsnew/feed/",
        "https://www.geektyrant.com/news?format=rss",
        "https://wegotthiscovered.com/feed/",
    ],
}

def clean_text(value):
    return re.sub(r"<[^>]+>", "", value or "").strip()

def get_video_id(text):
    patterns = [
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
        r"youtube\.com/watch\?v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return match.group(1)
    return ""

def main():
    videos = []
    seen = set()

    for category, urls in FEEDS.items():
        for feed_url in urls:
            try:
                request = urllib.request.Request(
                    feed_url,
                    headers={"User-Agent": "TrailerDrop/1.0"}
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    xml_data = response.read()

                root = ET.fromstring(xml_data)
                items = root.findall(".//item")
                if not items:
                    items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

                for item in items:
                    title = clean_text(
                        item.findtext("title")
                        or item.findtext("{http://www.w3.org/2005/Atom}title")
                    )
                    link = (
                        item.findtext("link")
                        or item.findtext("{http://www.w3.org/2005/Atom}link")
                        or ""
                    )
                    description = (
                        item.findtext("description")
                        or item.findtext("{http://www.w3.org/2005/Atom}summary")
                        or ""
                    )
                    video_id = get_video_id(link + " " + description)

                    if not title or title in seen:
                        continue

                    # Keep trailer, gaming announcement, and Nintendo Direct items.
                    searchable = (title + " " + clean_text(description)).lower()
                    keywords = (
                        "trailer", "teaser", "nintendo direct",
                        "gameplay", "official reveal", "video game"
                    )
                    if not any(word in searchable for word in keywords):
                        continue

                    seen.add(title)
                    videos.append({
                        "title": title,
                        "category": category,
                        "link": link,
                        "videoId": video_id,
                        "published": datetime.now(timezone.utc).isoformat(),
                        "source": feed_url,
                    })

            except Exception as error:
                print(f"Feed failed: {feed_url}: {error}")

    videos.sort(key=lambda video: video["published"], reverse=True)

    with open("trailers.json", "w", encoding="utf-8") as file:
        json.dump({
            "updated": datetime.now(timezone.utc).isoformat(),
            "videos": videos[:200],
        }, file, indent=2, ensure_ascii=False)

    print(f"Saved {min(len(videos), 200)} items.")

if __name__ == "__main__":
    main()
