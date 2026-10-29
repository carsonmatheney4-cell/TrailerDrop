
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
USER_AGENT = "TrailerDrop/3.0"

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

MOVIE_BACKFILL_QUERIES = [
    "official family movie trailer",
    "official animated movie trailer",
    "official movie teaser trailer",
]

GAMING_BACKFILL_QUERIES = [
    "official Nintendo game trailer",
    "Mario official game trailer",
    "Zelda official trailer",
    "Pokemon official trailer",
    "Metroid official trailer",
    "official video game reveal trailer",
]

ATOM_NS = "{http://www.w3.org/2005/Atom}"
RATING_CACHE = {}


def fetch(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def request_json(url):
    try:
        return json.loads(fetch(url).decode("utf-8"))
    except (
        urllib.error.URLError, TimeoutError, OSError, ValueError
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
    title = clean_text(title).lower()

    # Remove common trailer labels and technical tags.
    title = re.sub(
        r"\b(official|final|teaser|trailer|4k|hd|uhd|"
        r"exclusive|international|cinematic|announcement|"
        r"reveal|launch|clip|subtitles|dubbed|"
        r"vertical|extended|new trailer|movie trailer)\b",
        " ",
        title,
    )

    # Remove years and common video labels.
    title = re.sub(r"\b(19|20)\d{2}\b", " ", title)
    title = re.sub(r"\b1080p\b|\b720p\b|\b2160p\b", " ", title)
    title = re.sub(r"[\[\](){}:|–—\-!.,\"']", " ", title)

    return re.sub(r"\s+", " ", title).strip()


def movie_search_title(title):
    """Extract a likely movie title from a YouTube trailer title."""
    title = clean_text(title)

    # Remove text after common separators used for channel names.
    title = re.split(r"\s+[|]\s+|\s+[-–—]\s+", title, maxsplit=1)[0]

    # Remove bracketed quality labels.
    title = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title)

    title = re.sub(
        r"\b(official|final|teaser|trailer|4k|hd|uhd|"
        r"exclusive|international|new|movie|film|"
        r"coming soon|in theaters|only in theaters|"
        r"announcement|reveal|subtitled|dubbed)\b",
        " ",
        title,
        flags=re.I,
    )

    title = re.sub(r"\b(19|20)\d{2}\b", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -:|")
    return title


def tmdb_request(path, params=None):
    if not TMDB_API_KEY:
        return {}

    query = dict(params or {})
    query["api_key"] = TMDB_API_KEY

    url = "https://api.themoviedb.org/3" + path
    url += "?" + urllib.parse.urlencode(query)
    return request_json(url)


def movie_rating(title):
    """
    Return a confirmed allowed US certification.
    Unknown or unconfirmed ratings remain excluded.
    """
    if not TMDB_API_KEY:
        return ""

    search_title = movie_search_title(title)
    cache_key = normalize_title(search_title)

    if not cache_key:
        return ""

    if cache_key in RATING_CACHE:
        return RATING_CACHE[cache_key]

    data = tmdb_request(
        "/search/movie",
        {"query": search_title, "include_adult": "false"},
    )

    candidates = data.get("results", [])
    best_movie = None
    best_score = 0.0

    search_normalized = normalize_title(search_title)

    for candidate in candidates:
        candidate_title = candidate.get("title", "")
        candidate_normalized = normalize_title(candidate_title)

        if not candidate_normalized:
            continue

        score = SequenceMatcher(
            None, search_normalized, candidate_normalized
        ).ratio()

        # Prefer an exact normalized title match.
        if candidate_normalized == search_normalized:
            score = 1.0

        if score > best_score:
            best_score = score
            best_movie = candidate

    if not best_movie or best_score < 0.65:
        print(
            f"Movie rating unknown: '{title}' "
            f"(TMDB match score {best_score:.2f})"
        )
        RATING_CACHE[cache_key] = ""
        return ""

    movie_id = best_movie.get("id")
    matched_title = best_movie.get("title", "")

    if not movie_id:
        RATING_CACHE[cache_key] = ""
        return ""

    release_data = tmdb_request(
        f"/movie/{movie_id}/release_dates"
    )

    certifications = []

    for country in release_data.get("results", []):
        if country.get("iso_3166_1") != "US":
            continue

        for release in country.get("release_dates", []):
            cert = (
                release.get("certification") or ""
            ).strip().upper()

            if cert:
                certifications.append(cert)

    # Use a rating only when the US certification is allowed.
    rating = next(
        (
            cert for cert in certifications
            if cert in ALLOWED_MOVIE_RATINGS
        ),
        "",
    )

    if rating:
        print(
            f"Movie accepted: '{title}' -> "
            f"{matched_title} ({rating})"
        )
    else:
        print(
            f"Movie excluded: '{title}' -> "
            f"{matched_title}; no confirmed allowed US rating"
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
        r"https?://(?:www\.)?youtube\.com/watch\?[^\s\"'<>]+",
        r"https?://youtu\.be/[A-Za-z0-9_-]{11}",
        r"https?://(?:www\.)?youtube\.com/embed/[A-Za-z0-9_-]{11}",
        r"https?://(?:www\.)?youtube\.com/shorts/[A-Za-z0-9_-]{11}",
    ]

    for pattern in patterns:
        for url in re.findall(pattern, text):
            video_id = video_id_from(url)
            if video_id:
                ids.add(video_id)

    return ids


def youtube_video_details(video_ids):
    if not YOUTUBE_API_KEY or not video_ids:
        return {}

    details = {}
    ids = list(video_ids)

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

        article_title = (
            clean_text(title_el.text or "")
            if title_el is not None else ""
        )

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

        for tag in (
            "pubDate",
            f"{ATOM_NS}published",
            f"{ATOM_NS}updated",
        ):
            date_el = entry.find(tag)

            if date_el is not None and date_el.text:
                published = date_el.text
                break

        ids = extract_video_ids(ET.tostring(entry, encoding="unicode"))

        direct_id = video_id_from(link)

        if direct_id:
            ids.add(direct_id)

        id_el = entry.find(f"{ATOM_NS}id")

        if id_el is not None and id_el.text:
            match = re.search(
                r"yt:video:([A-Za-z0-9_-]{11})",
                id_el.text,
            )
            if match:
                ids.add(match.group(1))

        embedded_ids.update(ids)
        entry_info.append((article_title, published, ids))

    details = youtube_video_details(embedded_ids)

    for article_title, published, ids in entry_info:
        for video_id in ids:
            snippet = details.get(video_id, {})
            video_title = clean_text(snippet.get("title", ""))

            if not video_title:
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
        f"{feed['name']}: {len(entries)} entries, "
        f"{len(embedded_ids)} YouTube links, "
        f"{len(unique)} eligible videos"
    )

    return list(unique.values())


def search_youtube(query, category):
    if not YOUTUBE_API_KEY:
        print("YOUTUBE_API_KEY missing; skipping search.")
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
        print(f"YouTube API error for '{query}': {data['error']}")
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
    print(f"Backfill enabled: {BACKFILL}")

    existing = load_existing()
    videos_by_id = {}

    for video in existing.values():
        if allowed_video(video):
            videos_by_id[video["id"]] = video

    for feed in FEEDS:
        for video in read_feed(feed):
            videos_by_id[video["id"]] = video

    # Searches run only when manually enabled.
    if BACKFILL:
        queries = (
            [(q, "Movie Trailers") for q in MOVIE_BACKFILL_QUERIES]
            + [(q, "Gaming") for q in GAMING_BACKFILL_QUERIES]
        )

        for query, category in queries:
            for video in search_youtube(query, category):
                videos_by_id[video["id"]] = video

            time.sleep(0.3)
    else:
        print(
            "YouTube search skipped during scheduled run. "
            "Use manual backfill to discover additional videos."
        )

    cleaned = []
    seen = set()

    for video in videos_by_id.values():
        video_id = video.get("id")

        if not video_id or video_id in seen:
            continue

        if allowed_video(video):
            seen.add(video_id)
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
