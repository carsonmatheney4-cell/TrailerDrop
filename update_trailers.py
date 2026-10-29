
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

OUTPUT = Path("trailers.json")
MAX_VIDEOS = 10000
TIMEOUT = 20

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
BACKFILL = os.environ.get("YOUTUBE_BACKFILL", "false").lower() == "true"

EXCLUDED_TERMS = [
    "reaction", "reactions", "reacts to", "reacting to",
    "trailer reaction", "review", "reviews", "movie review",
    "game review", "breakdown", "explained", "ending explained",
    "first time watching", "watch along", "commentary",
    "tier list", "ranking every", "honest review",
]

TRAILER_KEYWORDS = [
    "official trailer", "teaser trailer", "official teaser",
    "trailer", "teaser", "announce trailer", "announcement trailer",
    "cinematic trailer", "launch trailer", "story trailer",
    "gameplay trailer", "world premiere", "official reveal",
    "release date trailer", "reveal trailer", "short trailer",
]

# Each feed is labeled with the category that will appear on the site.
FEEDS = [
    {
        "name": "ComingSoon",
        "url": "https://www.comingsoon.net/feed",
        "category": "Movie Trailers",
    },
    {
        "name": "JoBlo Movie Trailers",
        "url": "https://www.joblo.com/feed/",
        "category": "Movie Trailers",
    },
    {
        "name": "FirstShowing",
        "url": "https://www.firstshowing.net/feed/",
        "category": "Movie Trailers",
    },
    {
        "name": "GeekTyrant",
        "url": "https://geektyrant.com/news?format=rss",
        "category": "Gaming",
    },
    {
        "name": "We Got This Covered",
        "url": "https://wegotthiscovered.com/feed/",
        "category": "Gaming",
    },
    {
        "name": "Nintendo",
        "url": (
            "https://www.youtube.com/feeds/videos.xml"
            "?channel_id=UCGIY_O-8vW4rfX98KlMkvRg"
        ),
        "category": "Gaming",
    },
]

BACKFILL_QUERIES = [
    "official Nintendo game trailer",
    "Nintendo Switch 2 official trailer",
    "Mario official game trailer",
    "Zelda official trailer",
    "Pokemon official trailer",
    "Metroid official trailer",
    "Kirby official trailer",
    "Donkey Kong official trailer",
    "Splatoon official trailer",
    "Animal Crossing official trailer",
    "Super Smash Bros official trailer",
    "Nintendo Direct game trailer",
    "official video game cinematic trailer",
    "official video game reveal trailer",
]

USER_AGENT = "TrailerDrop/1.0 (personal trailer listing)"

def fetch(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()

def clean_text(value):
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    return re.sub(r"\s+", " ", value).strip()

def video_id_from(url):
    if not url:
        return ""
    patterns = [
        r"(?:youtube\.com/watch\?.*v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/feeds/videos\.xml\?video_id=)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""

def parse_date(value):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""

def is_excluded(title):
    title = title.lower()
    return any(term in title for term in EXCLUDED_TERMS)

def looks_like_trailer(title):
    title = title.lower()
    return any(term in title for term in TRAILER_KEYWORDS)

def is_movie_category(category):
    category = category.lower()
    return "movie" in category or "film" in category

def request_json(url):
    try:
        return json.loads(fetch(url).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {}

def tmdb_request(path, params=None):
    if not TMDB_API_KEY:
        return {}
    params = dict(params or {})
    params["api_key"] = TMDB_API_KEY
    url = "https://api.themoviedb.org/3" + path
    url += "?" + urllib.parse.urlencode(params)
    return request_json(url)

def normalize_title(title):
    title = re.sub(
        r"\b(official|final|teaser|movie|film|gameplay|cinematic|"
        r"announcement|reveal|launch|4k|hd|trailer|clip|"
        r"international|red band|exclusive)\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"[\[\](){}:|–—-]", " ", title)
    return re.sub(r"\s+", " ", title).strip().lower()

def movie_rating(title):
    """Return a confirmed US certification, or an empty string."""
    if not TMDB_API_KEY:
        return ""

    cache_key = normalize_title(title)
    if not cache_key:
        return ""

    # Avoid repeated requests for the same title during one run.
    if cache_key in RATING_CACHE:
        return RATING_CACHE[cache_key]

    search_title = re.sub(
        r"\b(official|final|teaser|trailer|4k|hd|exclusive)\b",
        " ",
        title,
        flags=re.I,
    )
    search_title = re.sub(r"\s+", " ", search_title).strip()

    data = tmdb_request(
        "/search/movie",
        {"query": search_title, "include_adult": "false"},
    )

    best_movie = None
    best_score = 0.0

    for candidate in data.get("results", []):
        candidate_title = candidate.get("title", "")
        score = SequenceMatcher(
            None,
            normalize_title(search_title),
            normalize_title(candidate_title),
        ).ratio()

        if score > best_score:
            best_score = score
            best_movie = candidate

    # Do not guess when the search result is a poor title match.
    if not best_movie or best_score < 0.60:
        RATING_CACHE[cache_key] = ""
        return ""

    movie_id = best_movie.get("id")
    if not movie_id:
        RATING_CACHE[cache_key] = ""
        return ""

    release_data = tmdb_request(
        f"/movie/{movie_id}/release_dates"
    )

    # Use the US theatrical certification, not an arbitrary country's rating.
    certifications = []
    for country in release_data.get("results", []):
        if country.get("iso_3166_1") != "US":
            continue
        for release in country.get("release_dates", []):
            cert = (release.get("certification") or "").strip().upper()
            if cert:
                certifications.append(cert)

    # Prefer a permitted rating if the movie has multiple US releases.
    permitted = {"G", "PG", "PG-13"}
    rating = next(
        (cert for cert in certifications if cert in permitted),
        "",
    )

    # If it has no permitted US certification, it is not displayed.
    RATING_CACHE[cache_key] = rating
    time.sleep(0.05)
    return rating

RATING_CACHE = {}

def allowed_video(video):
    title = video.get("title", "")
    if not title or is_excluded(title):
        return False

    if is_movie_category(video.get("category", "")):
        rating = movie_rating(title)
        if rating not in {"G", "PG", "PG-13"}:
            return False
        video["rating"] = rating

    return True

def make_video(video_id, title, published, category, source):
    title = clean_text(title)
    if not video_id or not title or is_excluded(title):
        return None

    # The feed and search results should describe trailers, not general articles.
    if not looks_like_trailer(title):
        return None

    video = {
        "id": video_id,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "published": parse_date(published),
        "category": category,
        "source": source,
    }

    if is_movie_category(category):
        rating = movie_rating(title)
        if rating not in {"G", "PG", "PG-13"}:
            return None
        video["rating"] = rating

    return video

def read_feed(feed):
    found = []
    try:
        root = ET.fromstring(fetch(feed["url"]))
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as exc:
        print(f"Feed unavailable: {feed['name']}: {exc}")
        return found

    # Supports both RSS <item> and YouTube Atom <entry>.
    entries = root.findall(".//item")
    atom_ns = "{http://www.w3.org/2005/Atom}"
    entries += root.findall(f".//{atom_ns}entry")

    for entry in entries:
        title = ""
        link = ""
        published = ""

        title_element = entry.find("title")
        if title_element is None:
            title_element = entry.find(f"{atom_ns}title")
        if title_element is not None:
            title = clean_text(title_element.text or "")

        link_element = entry.find("link")
        if link_element is not None:
            link = (
                link_element.get("href")
                or link_element.text
                or ""
            ).strip()
        if not link:
            atom_link = entry.find(f"{atom_ns}link")
            if atom_link is not None:
                link = atom_link.get("href", "").strip()

        published_element = (
            entry.find("pubDate")
            or entry.find(f"{atom_ns}published")
            or entry.find(f"{atom_ns}updated")
        )
        if published_element is not None:
            published = published_element.text or ""

        video_id = video_id_from(link)
        if not video_id:
            # YouTube Atom entries use a separate video ID element.
            id_element = entry.find(f"{atom_ns}id")
            if id_element is not None and id_element.text:
                match = re.search(
                    r"yt:video:([A-Za-z0-9_-]{11})",
                    id_element.text,
                )
                if match:
                    video_id = match.group(1)

        if not video_id:
            continue

        video = make_video(
            video_id,
            title,
            published,
            feed["category"],
            feed["name"],
        )
        if video and allowed_video(video):
            found.append(video)

    print(f"{feed['name']}: found {len(found)} eligible videos")
    return found

def search_youtube(query):
    if not YOUTUBE_API_KEY:
        print("YOUTUBE_API_KEY is missing; skipping YouTube search.")
        return []

    params = {
        "part": "snippet",
        "type": "video",
        "maxResults": 50,
        "order": "date",
        "q": query,
        "key": YOUTUBE_API_KEY,
        "videoEmbeddable": "true",
        "safeSearch": "moderate",
    }
    url = (
        "https://www.googleapis.com/youtube/v3/search?"
        + urllib.parse.urlencode(params)
    )
    data = request_json(url)

    if data.get("error"):
        print(f"YouTube API error for {query}: {data['error']}")
        return []

    found = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        video_id = item.get("id", {}).get("videoId", "")
        title = snippet.get("title", "")
        published = snippet.get("publishedAt", "")

        video = make_video(
            video_id,
            title,
            published,
            "Gaming",
            snippet.get("channelTitle", "YouTube"),
        )
        if video and allowed_video(video):
            found.append(video)

    return found

def load_existing():
    if not OUTPUT.exists():
        return {}
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        videos = data.get("videos", [])
        return {
            item["id"]: item
            for item in videos
            if isinstance(item, dict) and item.get("id")
        }
    except (OSError, ValueError, TypeError) as exc:
        print(f"Could not read existing JSON: {exc}")
        return {}

def save_output(videos):
    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "videos": videos,
    }
    OUTPUT.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

def main():
    print("Starting TrailerDrop update...")
    existing = load_existing()
    videos_by_id = {}

    # Re-check existing entries so unwanted old videos are removed.
    for video in existing.values():
        if allowed_video(video):
            videos_by_id[video["id"]] = video

    # Read the configured feeds.
    for feed in FEEDS:
        for video in read_feed(feed):
            videos_by_id[video["id"]] = video

    # Optional backfill. Enable by manually running the workflow with
    # the backfill checkbox selected. This uses YouTube API quota.
    if BACKFILL:
        for query in BACKFILL_QUERIES:
            print(f"Backfill search: {query}")
            for video in search_youtube(query):
                videos_by_id[video["id"]] = video
            time.sleep(0.2)

    # Final filtering pass, including previously saved videos.
    cleaned = []
    for video in videos_by_id.values():
        if allowed_video(video):
            cleaned.append(video)

    cleaned.sort(
        key=lambda item: item.get("published", ""),
        reverse=True,
    )
    cleaned = cleaned[:MAX_VIDEOS]

    save_output(cleaned)
    print(f"Saved {len(cleaned)} eligible videos to {OUTPUT}")

if __name__ == "__main__":
    main()
