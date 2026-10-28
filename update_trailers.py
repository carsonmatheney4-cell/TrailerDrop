
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from html import unescape

OUTPUT = Path("trailers.json")
MAX_VIDEOS = 10000
TIMEOUT = 15

FEEDS = {
    "Movie Trailers": [
        "https://www.comingsoon.net/feed",
        "https://www.joblo.com/feed/",
        "https://www.firstshowing.net/feed/",
    ],
    "Gaming": [
        # Official Nintendo of America
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCGIY_O-8vW4rfX98KlMkvRg",
        # Additional Nintendo channels
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCkH3CcMfqww9RsZvPRPkAJA",
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCgK_2VQWwigtTr_5TviGy2w",
        # Gaming news
        "https://www.geektyrant.com/news?format=rss",
        "https://wegotthiscovered.com/feed/",
    ],
}

KEYWORDS = (
    "trailer", "teaser", "gameplay", "game trailer",
    "nintendo direct", "indie world", "partner showcase",
    "treehouse", "mario", "zelda", "pokemon", "pokémon",
    "kirby", "metroid", "splatoon", "donkey kong",
    "super smash bros", "fire emblem", "pikmin",
    "switch 2", "official reveal", "game reveal",
    "launch trailer", "announcement trailer", "first look",
    "mario tennis", "mario party", "mario odyssey",
    "super mario bros", "mario wonder", "mario galaxy",
    "mario kart", "mario kart world", "gameplay trailer",
    "release date trailer", "direct presentation",
    "direct showcase", "mario gameplay", "gameplay overview",
    "game overview", "nintendo switch",
)

VIDEO_PATTERNS = [
    r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
    r"youtube\.com/watch\?[^\s\"'<>]*?(?:^|&)v=([A-Za-z0-9_-]{11})",
    r"youtu\.be/([A-Za-z0-9_-]{11})",
    r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
    r"youtube\.com/live/([A-Za-z0-9_-]{11})",
    r"youtube-nocookie\.com/embed/([A-Za-z0-9_-]{11})",
]

def text_of(element):
    if element is None:
        return ""
    return unescape(" ".join("".join(element.itertext()).split()))

def video_id_from(text):
    text = unescape(text or "")
    for pattern in VIDEO_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""

def parse_date(value):
    if not value:
        return ""
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return date.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError):
            return ""

def item_link(item):
    for child in item:
        if child.tag.split("}")[-1] == "link":
            href = child.attrib.get("href", "")
            if href:
                return href
            if child.text:
                return child.text.strip()
    return ""

def main():
    now = datetime.now(timezone.utc).isoformat()
    videos_by_id = {}

    # Preserve videos already collected.
    if OUTPUT.exists():
        try:
            old_data = json.loads(OUTPUT.read_text(encoding="utf-8"))
            for video in old_data.get("videos", []):
                vid = video.get("videoId", "")
                if vid and video.get("title"):
                    videos_by_id[vid] = video
        except (OSError, ValueError):
            print("Warning: couldn't read previous trailer data.")

    successful_feeds = 0
    found_new = 0

    for category, urls in FEEDS.items():
        for feed_url in urls:
            try:
                request = urllib.request.Request(
                    feed_url,
                    headers={"User-Agent": "Mozilla/5.0 TrailerDrop/1.0"}
                )

                with urllib.request.urlopen(
                    request, timeout=TIMEOUT
                ) as response:
                    root = ET.fromstring(response.read())

                successful_feeds += 1
                items = [
                    node for node in root.iter()
                    if node.tag.split("}")[-1] in ("item", "entry")
                ]

                feed_found = 0

                for item in items:
                    title = ""
                    published = ""
                    link = item_link(item)
                    parts = [link]

                    for child in item.iter():
                        name = child.tag.split("}")[-1].lower()
                        value = text_of(child)

                        if name == "title" and not title:
                            title = value

                        if name in (
                            "description", "summary", "content",
                            "encoded", "enclosure", "group", "videoid"
                        ):
                            parts.append(value)

                        if name in ("pubdate", "published", "updated"):
                            if value and not published:
                                published = parse_date(value)

                        parts.append(str(child.attrib))

                    if not title:
                        continue

                    searchable = (
                        title + " " + " ".join(parts)
                    ).lower()

                    if not any(word in searchable for word in KEYWORDS):
                        continue

                    vid = video_id_from(" ".join(parts) + " " + link)

                    if not vid:
                        continue

                    if vid not in videos_by_id:
                        found_new += 1
                        feed_found += 1

                    videos_by_id[vid] = {
                        "title": title,
                        "category": category,
                        "link": link or f"https://www.youtube.com/watch?v={vid}",
                        "videoId": vid,
                        "published": (
                            published
                            or videos_by_id.get(vid, {}).get("published")
                            or now
                        ),
                        "source": feed_url,
                    }

                print(f"Feed OK: {feed_url} | New videos: {feed_found}")

            except Exception as error:
                print(f"Feed failed: {feed_url}: {error}")

    if successful_feeds == 0:
        raise RuntimeError(
            "All feeds failed. Previous trailer data was preserved."
        )

    videos = sorted(
        videos_by_id.values(),
        key=lambda video: video.get("published", ""),
        reverse=True
    )[:MAX_VIDEOS]

    OUTPUT.write_text(
        json.dumps(
            {"updated": now, "videos": videos},
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    print(f"Feeds successfully read: {successful_feeds}")
    print(f"New embedded videos found: {found_new}")
    print(f"Total saved videos: {len(videos)}")

if __name__ == "__main__":
    main()
