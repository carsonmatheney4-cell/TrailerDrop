
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
USER_AGENT = "TrailerDrop/2.0"

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
BACKFILL = os.environ.get("YOUTUBE_BACKFILL", "false").lower() == "true"

ALLOWED_MOVIE_RATINGS = {"G", "PG", "PG-13"}

EXCLUDED_TERMS = [
    "reaction", "reactions", "reacts to", "reacting to",
    "trailer reaction", "review", "reviews", "movie review",
    "game review", "breakdown", "explained", "ending explained",
    "first time watching", "watch along", "commentary",
    "tier list", "ranking every", "honest review",
]

TRAILER_KEYWORDS = [
    "official trailer", "teaser trailer", "official teaser",
    "trailer", "teaser", "announcement trailer",
    "cinematic trailer", "launch trailer", "story trailer",
    "gameplay trailer", "world premiere", "official reveal",
    "release date trailer", "reveal trailer",
]

MOVIE_SEARCH_QUERIES = [
    "official movie trailer 2026",
    "official family movie trailer",
    "official animated movie trailer",
]

GAMING_SEARCH_QUERIES = [
    "official video game trailer",
    "official Nintendo game trailer",
]

FEEDS = [
    {
        "name": "ComingSoon",
        "url": "https://www.comingsoon.net/feed",
        "category": "Movie Trailers",
    },
    {
        "name": "JoBlo",
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

RATING_CACHE = {}
ATOM_NS = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"


def fetch(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.read()


def request_json(url):
    try:
        return json.loads(fetch(url).decode("utf-8"))
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Request failed: {exc}")
        return {}


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
        r"(?:youtube\.com|www\.youtube\.com)/watch\?.*?\bv=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
        r"youtube\.com/feeds/videos\.xml\?video_id=([A-Za-z0-9_-]{11})",
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
    lower = title.lower()
    return any(term in lower for term in EXCLUDED_TERMS)


def looks_like_trailer(title):
    lower = title.lower()
    return any(term in lower for term in TRAILER_KEYWORDS)


def is_movie_category(category):
    lower = category.lower()
    return "movie" in lower or "film" in lower


def normalize_title(title):
    title = re.sub(
        r"\b(official|final|teaser|movie|film|gameplay|cinematic|"
        r"announcement|reveal|launch|4k|hd|trailer|clip|"
        r"international|exclusive|new|coming soon)\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"[\[\](){}:|–—-]", " ", title)
    return re.sub(r"\s+", " ", title).strip().lower()


def tmdb_request(path, params=None):
    if not TMDB_API_KEY:
        return {}

    query = dict(params or {})
    query["api_key"] = TMDB_API_KEY

    url = "https://api.themoviedb.org/3" + path
    url += "?" + urllib.parse.urlencode(query)
    return request_json(url)


def movie_rating(title):
    if not TMDB_API_KEY:
        return ""

    search_title = re.sub(
        r"\b(official|final|teaser|trailer|4k|hd|exclusive)\b",
        " ",
        title,
        flags=re.I,
    )
    search_title = re.sub(r"\s+", " ", search_title).strip()
    cache_key = normalize_title(search_title)

    if not cache_key:
        return ""

    if cache_key in RATING_CACHE:
        return RATING_CACHE[cache_key]

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

    # Conservative matching prevents assigning the wrong rating.
    if not best_movie or best_score < 0.60:
        RATING_CACHE[cache_key] = ""
        return ""

    movie_id = best_movie.get("id")
    if not movie_id:
        RATING_CACHE[cache_key] = ""
        return ""

    release_data = tmdb_request(f"/movie/{movie_id}/release_dates")
    certifications = []

    for country in release_data.get("results", []):
        if country.get("iso_3166_1") != "US":
            continue

        for release in country.get("release_dates", []):
            cert = (release.get("certification") or "").strip().upper()
            if cert:
                certifications.append(cert)

    # Require a confirmed allowed US certification.
    rating = next(
        (cert for cert in certifications if cert in ALLOWED_MOVIE_RATINGS),
        "",
    )

    RATING_CACHE[cache_key] = rating
    time.sleep(0.05)
    return rating


def allowed_video(video):
    title = video.get("title", "")

    if not title or is_excluded(title):
        return False

    if not looks_like_trailer(title):
        return False

    if is_movie_category(video.get("category", "")):
        rating = movie_rating(title)
        if rating not in ALLOWED_MOVIE_RATINGS:
            return False
        video["rating"] = rating
    else:
        video.pop("rating", None)

    return True


def make_video(video_id, title, published, category, source):
    title = clean_text(title)

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        return None

    if not title or is_excluded(title) or not looks_like_trailer(title):
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

    if not allowed_video(video):
        return None

    return video


def extract_video_ids(text):
    if not text:
        return set()

    text = unescape(text)
    ids = set()

    patterns = [
        r"https?://(?:www\.)?youtube\.com/watch\?[^\s\"'<>]*",
        r"https?://youtu\.be/[A-Za-z0-9_-]{11}",
        r"https?://(?:www\.)?youtube\.com/embed/[A-Za-z0-9_-]{11}",
        r"https?://(?:www\.)?youtube\.com/shorts/[A-Za-z0-9_-]{11}",
    ]

    for pattern in patterns:
        for match in re.findall(pattern, text):
            video_id = video_id_from(match)
            if video_id:
                ids.add(video_id)

    return ids


def youtube_video_details(video_ids):
    if not YOUTUBE_API_KEY or not video_ids:
        return {}

    ids = list(video_ids)
    details = {}

    # YouTube videos.list accepts up to 50 IDs per request.
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        params = {
            "part": "snippet",
            "id": ",".join(batch),
            "key": YOUTUBE_API_KEY,
        }
        url = (
            "https://www.googleapis.com/youtube/v3/videos?"
            + urllib.parse.urlencode(params)
        )
        data = request_json(url)

        for item in data.get("items", []):
            details[item["id"]] = item.get("snippet", {})

    return details


def read_feed(feed):
    found = []

    try:
        root = ET.fromstring(fetch(feed["url"]))
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ET.ParseError,
    ) as exc:
        print(f"Feed unavailable: {feed['name']}: {exc}")
        return found

    entries = root.findall(".//item")
    entries += root.findall(f".//{ATOM_NS}entry")
    embedded_ids = set()
    entry_info = []

    for entry in entries:
        title_el = entry.find("title")
        if title_el is None:
            title_el = entry.find(f"{ATOM_NS}title")

        title = clean_text(title_el.text or "") if title_el is not None else ""

        link = ""
        link_el = entry.find("link")
        if link_el is not None:
            link = (
                link_el.get("href") or link_el.text or ""
            ).strip()

        if not link:
            atom_link = entry.find(f"{ATOM_NS}link")
            if atom_link is not None:
                link = atom_link.get("href", "").strip()

        published = ""
        for tag in ("pubDate", f"{ATOM_NS}published", f"{ATOM_NS}updated"):
            date_el = entry.find(tag)
            if date_el is not None and date_el.text:
                published = date_el.text
                break

        direct_id = video_id_from(link)

        id_el = entry.find(f"{ATOM_NS}id")
        if not direct_id and id_el is not None and id_el.text:
            match = re.search(r"yt:video:([A-Za-z0-9_-]{11})", id_el.text)
            if match:
                direct_id = match.group(1)

        content = ET.tostring(entry, encoding="unicode")
        ids = extract_video_ids(content)

        if direct_id:
            ids.add(direct_id)

        embedded_ids.update(ids)
        entry_info.append((title, published, ids))

    details = youtube_video_details(embedded_ids)

    for article_title, published, ids in entry_info:
        for video_id in ids:
            snippet = details.get(video_id, {})
            video_title = clean_text(snippet.get("title", ""))

            if not video_title:
                # Article titles are a fallback only when they themselves
                # look like a trailer title.
                video_title = article_title

            video = make_video(
                video_id=video_id,
                title=video_title,
                published=snippet.get("publishedAt", published),
                category=feed["category"],
                source=feed["name"],
            )

            if video:
                found.append(video)

    unique = {video["id"]: video for video in found}
    print(
        f"{feed['name']}: {len(entries)} feed entries, "
        f"{len(embedded_ids)} YouTube links, "
        f"{len(unique)} eligible videos"
    )
    return list(unique.values())


def search_youtube(query, category):
    if not YOUTUBE_API_KEY:
        print("YOUTUBE_API_KEY missing; skipping YouTube search.")
        return []

    params = {
        "part": "snippet",
        "type": "video",
        "maxResults": 25,
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

        video = make_video(
            video_id=video_id,
            title=snippet.get("title", ""),
            published=snippet.get("publishedAt", ""),
            category=category,
            source=snippet.get("channelTitle", "YouTube"),
        )

        if video:
            found.append(video)

    print(f"YouTube search '{query}': {len(found)} eligible")
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
    print(f"TMDB key configured: {bool(TMDB_API_KEY)}")
    print(f"YouTube key configured: {bool(YOUTUBE_API_KEY)}")

    if not TMDB_API_KEY:
        print("WARNING: Movies cannot pass rating checks without TMDB_API_KEY.")

    existing = load_existing()
    videos_by_id = {}

    # Recheck old entries; this also removes entries that no longer qualify.
    for video in existing.values():
        if allowed_video(video):
            videos_by_id[video["id"]] = video

    # Read RSS feeds and embedded YouTube links.
    for feed in FEEDS:
        for video in read_feed(feed):
            videos_by_id[video["id"]] = video

    # Search a small number of movie and gaming queries during regular runs.
    # Full historical searches only run when backfill is enabled.
    if YOUTUBE_API_KEY:
        queries = []

        if BACKFILL:
            queries.extend(
                (query, "Gaming") for query in BACKFILL_QUERIES
            )
            queries.extend(
                (query, "Movie Trailers") for query in MOVIE_SEARCH_QUERIES
            )
        else:
            queries.extend(
                (query, "Movie Trailers") for query in MOVIE_SEARCH_QUERIES[:1]
            )
            queries.extend(
                (query, "Gaming") for query in GAMING_SEARCH_QUERIES[:1]
            )

        for query, category in queries:
            for video in search_youtube(query, category):
                videos_by_id[video["id"]] = video
            time.sleep(0.2)

    # Final eligibility check and deduplication.
    cleaned = []
    seen = set()

    for video in videos_by_id.values():
        if video.get("id") in seen:
            continue

        if allowed_video(video):
            seen.add(video["id"])
            cleaned.append(video)

    cleaned.sort(
        key=lambda video: video.get("published", ""),
        reverse=True,
    )
    cleaned = cleaned[:MAX_VIDEOS]

    save_output(cleaned)

    movies = sum(
        1 for video in cleaned
        if is_movie_category(video.get("category", ""))
    )
    gaming = len(cleaned) - movies

    print(f"Final movie trailers: {movies}")
    print(f"Final gaming trailers: {gaming}")
    print(f"Saved {len(cleaned)} eligible videos to {OUTPUT}")


if __name__ == "__main__":
    main()
