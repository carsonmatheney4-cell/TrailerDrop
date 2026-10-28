
import json
import os
import re
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from html import unescape

OUTPUT = Path("trailers.json")
MAX_VIDEOS = 10000
TIMEOUT = 15
MAX_BACKFILL_PAGES = 3

FEEDS = {
    "Movie Trailers": [
        "https://www.comingsoon.net/feed",
        "https://www.joblo.com/feed/",
        "https://www.firstshowing.net/feed/",
    ],
    "Gaming": [
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCGIY_O-8vW4rfX98KlMkvRg",
        "https://www.geektyrant.com/news?format=rss",
        "https://wegotthiscovered.com/feed/",
    ],
}

KEYWORDS = (
    "trailer", "teaser", "gameplay", "nintendo direct",
    "indie world", "partner showcase", "treehouse",
    "mario", "zelda", "the legend of zelda", "link",
    "pokemon", "pokémon", "kirby", "metroid", "splatoon",
    "donkey kong", "smash bros", "super smash",
    "fire emblem", "pikmin", "animal crossing",
    "xenoblade", "luigi", "yoshi", "princess peach",
    "mario kart", "mario party", "super mario",
    "switch 2", "nintendo switch", "game reveal",
    "launch trailer", "announcement trailer",
    "release date trailer", "gameplay overview",
    "game overview", "official reveal", "game trailer",
)

BACKFILL_QUERIES = [
    "Nintendo official game trailer",
    "Nintendo Switch 2 game trailer",
    "Super Mario official trailer",
    "The Legend of Zelda official trailer",
    "Kirby official game trailer",
    "Pokemon official game trailer",
    "Metroid official game trailer",
    "Donkey Kong official game trailer",
    "Animal Crossing official trailer",
    "Splatoon official game trailer",
    "Super Smash Bros official trailer",
    "Fire Emblem official game trailer",
    "Pikmin official game trailer",
    "Luigi's Mansion official trailer",
    "Yoshi official game trailer",
    "Xenoblade Chronicles official trailer",
    "Princess Peach official game trailer",
    "Nintendo Direct game announcements",
]

VIDEO_PATTERNS = [
    r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
    r"youtube\.com/watch\?[^\s\"'<>]*?\bv=([A-Za-z0-9_-]{11})",
    r"youtu\.be/([A-Za-z0-9_-]{11})",
    r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
    r"youtube\.com/live/([A-Za-z0-9_-]{11})",
    r"youtube-nocookie\.com/embed/([A-Za-z0-9_-]{11})",
)


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


def load_existing():
    videos_by_id = {}
    if not OUTPUT.exists():
        return videos_by_id

    try:
        old_data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        for video in old_data.get("videos", []):
            vid = video.get("videoId", "")
            if vid and video.get("title"):
                videos_by_id[vid] = video
    except (OSError, ValueError) as error:
        print(f"Warning: couldn't read previous trailer data: {error}")

    return videos_by_id


def save_video(videos_by_id, video):
    """Add a video while preserving useful existing metadata."""
    video_id = video.get("videoId", "")
    if not video_id:
        return False

    existing = videos_by_id.get(video_id, {})
    merged = {**existing, **video}

    # Preserve existing values when the new source has no better value.
    for field in ("category", "source", "published", "link"):
        if existing.get(field) and not video.get(field):
            merged[field] = existing[field]

    videos_by_id[video_id] = merged
    return video_id not in existing


def search_youtube(query, api_key, videos_by_id):
    """Search YouTube, adding unique results and preserving existing metadata."""
    base_url = "https://www.googleapis.com/youtube/v3/search"
    page_token = ""
    added = 0

    for _ in range(MAX_BACKFILL_PAGES):
        params = {
            "part": "snippet",
            "type": "video",
            "maxResults": 50,
            "order": "relevance",
            "q": query,
            "regionCode": "US",
            "relevanceLanguage": "en",
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token

        url = base_url + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "TrailerDrop/1.0"},
        )

        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))

        for item in data.get("items", []):
            video_id = item.get("id", {}).get("videoId", "")
            snippet = item.get("snippet", {})
            title = unescape(snippet.get("title", "")).strip()

            if not video_id or not title:
                continue

            searchable = title.lower()
            if not any(word in searchable for word in KEYWORDS):
                continue

            existing = videos_by_id.get(video_id, {})
            published = (
                parse_date(snippet.get("publishedAt", ""))
                or existing.get("published", "")
                or datetime.now(timezone.utc).isoformat()
            )

            video = {
                "title": title,
                "category": existing.get("category", "Gaming"),
                "link": f"https://www.youtube.com/watch?v={video_id}",
                "videoId": video_id,
                "published": published,
                "source": existing.get("source", "YouTube Data API"),
            }

            if save_video(videos_by_id, video):
                added += 1

        page_token = data.get("nextPageToken", "")
        if not page_token:
            break

    print(f"YouTube search: {query} | New videos: {added}")
    return added


def read_feed(category, feed_url, videos_by_id, now):
    request = urllib.request.Request(
        feed_url,
        headers={"User-Agent": "Mozilla/5.0 TrailerDrop/1.0"},
    )

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        root = ET.fromstring(response.read())

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
        video_id = ""

        for child in item.iter():
            name = child.tag.split("}")[-1].lower()
            value = text_of(child)

            if name == "title" and not title:
                title = value

            if name == "videoid" and value:
                video_id = value

            if name in (
                "description", "summary", "content",
                "encoded", "enclosure", "group",
            ):
                parts.append(value)

            if name in ("pubdate", "published", "updated"):
                if value and not published:
                    published = parse_date(value)

            parts.append(str(child.attrib))

        if not title:
            continue

        searchable = (title + " " + " ".join(parts)).lower()
        if not any(word in searchable for word in KEYWORDS):
            continue

        video_id = video_id or video_id_from(" ".join(parts) + " " + link)
        if not video_id:
            continue

        existing = videos_by_id.get(video_id, {})
        video = {
            "title": title,
            "category": existing.get("category", category),
            "link": link or f"https://www.youtube.com/watch?v={video_id}",
            "videoId": video_id,
            "published": published or existing.get("published") or now,
            "source": existing.get("source", feed_url),
        }

        if save_video(videos_by_id, video):
            feed_found += 1

    print(f"Feed OK: {feed_url} | New videos: {feed_found}")
    return feed_found


def main():
    now = datetime.now(timezone.utc).isoformat()
    videos_by_id = load_existing()

    successful_feeds = 0
    found_new = 0
    backfill = os.environ.get("YOUTUBE_BACKFILL", "").lower() == "true"

    # Read feeds. One failed feed won't stop the others.
    for category, urls in FEEDS.items():
        for feed_url in urls:
            try:
                found_new += read_feed(
                    category, feed_url, videos_by_id, now
                )
                successful_feeds += 1
            except Exception as error:
                print(f"Feed failed: {feed_url}: {error}")

    # Search history only when manually enabled in GitHub Actions.
    if backfill:
        api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "Backfill selected, but YOUTUBE_API_KEY is missing. "
                "Check your GitHub Actions secret."
            )

        print("Starting manual YouTube historical search.")
        failed_searches = 0

        for query in BACKFILL_QUERIES:
            try:
                found_new += search_youtube(
                    query, api_key, videos_by_id
                )
            except Exception as error:
                failed_searches += 1
                print(f"YouTube search failed for '{query}': {error}")

        if failed_searches == len(BACKFILL_QUERIES):
            raise RuntimeError(
                "Every YouTube search failed. Check the API key, "
                "API restrictions, and quota. Trailer data was not saved."
            )

        print(
            f"Historical search finished: {failed_searches} "
            f"of {len(BACKFILL_QUERIES)} queries failed."
        )

    if successful_feeds == 0 and not backfill:
        raise RuntimeError(
            "All feeds failed. Previous trailer data was preserved."
        )

    videos = sorted(
        videos_by_id.values(),
        key=lambda video: video.get("published", ""),
        reverse=True,
    )[:MAX_VIDEOS]

    OUTPUT.write_text(
        json.dumps(
            {"updated": now, "videos": videos},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(f"Feeds successfully read: {successful_feeds}")
    print(f"New videos found: {found_new}")
    print(f"Total saved videos: {len(videos)}")


if __name__ == "__main__":
    main()
