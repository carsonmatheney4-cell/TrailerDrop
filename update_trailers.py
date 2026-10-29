import json
import os
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import requests

# ======================== CONFIG ========================
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()

BASE_DIR = Path(__file__).resolve().parent
TRAILERS_FILE = BASE_DIR / "trailers.json"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
TMDB_API = "https://api.themoviedb.org/3"
REQUEST_TIMEOUT = 20
MAX_RESULTS = 25
MIN_TITLE_SIMILARITY = 0.78

EXCLUDED_TERMS = [
    "gameplay", "walkthrough", "reaction", "review", "ending explained",
    "full movie", "full episode", "compilation", "behind the scenes",
    "interview", "soundtrack", "lyrics", "fan made", "fanmade", "parody",
    "recap", "lego mario", "super mario gameplay", "game trailer",
    "gaming trailer", "minecraft", "roblox", "fortnite", "shorts",
    "#shorts", "fan edit", "concept trailer", "fake trailer",
    "honest trailer", "trailer reaction", "movie clips", "deleted scene",
    "scene pack", "movie scenes",
]
TRAILER_TERMS = [
    "official trailer", "official teaser", "official teaser trailer",
    "official final trailer", "official theatrical trailer",
    "teaser trailer", "final trailer", "trailer", "teaser",
]
SEARCHES = [
    "official movie trailer 2026",
    "official theatrical trailer 2026 movie",
    "official movie teaser trailer 2026",
    "official upcoming movie trailer 2027",
    "official film trailer 2026",
    "official animated movie trailer 2026",
    "official family movie trailer 2026",
]
BLOCKED_MOVIE_RATINGS = {"NC-17", "X"}


# ======================== HELPERS ========================
def normalize(value):
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def similarity(first, second):
    return SequenceMatcher(None, normalize(first), normalize(second)).ratio()


def get_json(url, params=None):
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            if attempt == 2:
                print(f"API request failed: {error}")
                return None
            time.sleep(2 ** (attempt + 1))
    return None


def youtube_request(endpoint, params):
    if not YOUTUBE_API_KEY:
        raise RuntimeError("Missing YOUTUBE_API_KEY repository secret.")
    params = dict(params)
    params["key"] = YOUTUBE_API_KEY
    return get_json(f"{YOUTUBE_API}{endpoint}", params)


def tmdb_request(endpoint, params=None):
    if not TMDB_API_KEY:
        raise RuntimeError("Missing TMDB_API_KEY repository secret.")
    params = dict(params or {})
    params["api_key"] = TMDB_API_KEY
    return get_json(f"{TMDB_API}{endpoint}", params)


# ======================== JSON FILE ========================
def recover_json_array(content):
    """Recover complete object records from a damaged JSON array."""
    stripped = content.lstrip()
    if not stripped.startswith("["):
        raise ValueError("Expected trailers.json to start with '['.")

    decoder = json.JSONDecoder()
    pos = len(content) - len(stripped) + 1
    records = []
    notes = []

    while pos < len(content):
        while pos < len(content) and content[pos].isspace():
            pos += 1
        if pos >= len(content):
            notes.append("Reached end of file before closing array bracket.")
            break
        if content[pos] == "]":
            pos += 1
            break
        if content[pos] == ",":
            pos += 1
            while pos < len(content) and content[pos].isspace():
                pos += 1
            if pos < len(content) and content[pos] == "]":
                notes.append("Found trailing comma before closing bracket.")
                break

        try:
            item, end = decoder.raw_decode(content, pos)
        except json.JSONDecodeError as error:
            notes.append(
                f"Stopped at line {error.lineno}, column {error.colno}: "
                f"{error.msg}"
            )
            break

        if not isinstance(item, dict):
            notes.append(f"Stopped at character {pos}: item wasn't an object.")
            break
        records.append(item)
        pos = end

    if not records:
        raise ValueError("No complete trailer records could be recovered.")
    return records, notes


def load_trailers():
    if not TRAILERS_FILE.exists():
        print("trailers.json not found; starting with an empty list.")
        return []

    content = TRAILERS_FILE.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        print(f"Invalid trailers.json: {error}")
        print("Trying to recover complete records without guessing at the error.")

        try:
            data, notes = recover_json_array(content)
        except ValueError as recovery_error:
            raise RuntimeError(
                f"{recovery_error} The original file was not changed. "
                "Restore trailers.json from Git history or repair it manually."
            ) from error

        # Keep a complete backup before writing any recovered data.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = TRAILERS_FILE.with_name(f"trailers.json.backup_{stamp}")
        backup.write_text(content, encoding="utf-8")

        temp = TRAILERS_FILE.with_suffix(".json.tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        temp.replace(TRAILERS_FILE)

        print(f"Recovered {len(data)} complete records.")
        print(f"Original saved to {backup}")
        for note in notes:
            print(f"Recovery note: {note}")

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("videos", "trailers", "results"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
    raise ValueError("Unexpected trailers.json format; expected an array of objects.")


def save_trailers(videos):
    temp = TRAILERS_FILE.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(videos, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    temp.replace(TRAILERS_FILE)


# ======================== YOUTUBE ========================
def search_youtube(query):
    data = youtube_request("/search", {
        "part": "snippet", "q": query, "type": "video",
        "maxResults": MAX_RESULTS, "safeSearch": "moderate",
        "videoEmbeddable": "true", "videoSyndicated": "true",
        "order": "date",
    })
    results = []
    for item in (data or {}).get("items", []):
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})
        if not video_id:
            continue
        thumbs = snippet.get("thumbnails", {})
        thumb = thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}
        results.append({
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "channel": snippet.get("channelTitle", ""),
            "channel_id": snippet.get("channelId", ""),
            "published_at": snippet.get("publishedAt", ""),
            "thumbnail": thumb.get("url", ""),
        })
    return results


def get_video_details(video_ids):
    details = {}
    for start in range(0, len(video_ids), 50):
        batch = video_ids[start:start + 50]
        data = youtube_request("/videos", {
            "part": "contentDetails,snippet,status",
            "id": ",".join(batch), "maxResults": 50,
        })
        for item in (data or {}).get("items", []):
            details[item["id"]] = item
    return details


def duration_seconds(duration):
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not match:
        return None
    hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return hours * 3600 + minutes * 60 + seconds


# ======================== FILTERING ========================
def looks_like_trailer(video):
    title = normalize(video.get("title", ""))
    description = normalize(video.get("description", ""))
    if not title:
        return False
    if any(normalize(term) in title for term in EXCLUDED_TERMS):
        return False
    if any(term in description for term in (
        "gameplay walkthrough", "reaction channel", "fan made trailer",
        "concept trailer",
    )):
        return False
    return any(normalize(term) in title for term in TRAILER_TERMS)


def is_likely_official_channel(video):
    channel = normalize(video.get("channel", ""))
    names = [
        "warner bros", "warner brothers", "sony pictures", "universal pictures",
        "universal studios", "paramount pictures", "walt disney studios",
        "disney", "marvel entertainment", "20th century studios",
        "searchlight pictures", "lionsgate", "netflix", "amazon mgm studios",
        "prime video", "a24", "focus features", "dreamworks", "illumination",
        "pixar", "walt disney animation", "sony pictures animation",
        "studio ghibli", "crunchyroll",
    ]
    return any(name in channel for name in names)


def get_movie_search_title(title):
    value = str(title or "")
    value = re.sub(
        r"\b(official\s+)?(final\s+|teaser\s+|theatrical\s+)?trailer\b",
        " ", value, flags=re.IGNORECASE,
    )
    value = re.sub(r"\bofficial\s+teaser\b|\bteaser\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(4k|hd|official video)\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\(\s*\d{4}\s*\)|\[\s*\d{4}\s*\]", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -|:[]()")


# ======================== TMDB / RATINGS ========================
def find_tmdb_movie(title):
    search_title = get_movie_search_title(title)
    if len(normalize(search_title)) < 2:
        return None
    data = tmdb_request("/search/movie", {
        "query": search_title, "include_adult": "false", "language": "en-US",
    })
    if not data:
        return None

    best_movie = None
    best_score = 0.0
    for movie in data.get("results", []):
        candidate = movie.get("title") or movie.get("original_title") or ""
        score = similarity(search_title, candidate)
        if normalize(search_title) == normalize(candidate):
            score = 1.0
        if score > best_score:
            best_score, best_movie = score, movie
    return best_movie if best_score >= MIN_TITLE_SIMILARITY else None


def get_us_certifications(movie_id):
    data = tmdb_request(f"/movie/{movie_id}/release_dates")
    if not data:
        return []
    certifications = []
    for country in data.get("results", []):
        if country.get("iso_3166_1") != "US":
            continue
        for release in country.get("release_dates", []):
            cert = (release.get("certification") or "").strip().upper()
            if cert:
                certifications.append(cert)
    return list(dict.fromkeys(certifications))


def confirmed_rating(certifications):
    values = {str(cert).strip().upper() for cert in certifications if cert}
    if values & BLOCKED_MOVIE_RATINGS:
        return "", "blocked"
    for rating in ("G", "PG", "PG-13", "R"):
        if rating in values:
            return rating, "confirmed"
    return "", "unknown"


def movie_rating(movie):
    if not movie.get("id"):
        return "", "unknown"
    rating, status = confirmed_rating(get_us_certifications(movie["id"]))
    return (rating, status) if status in ("blocked", "confirmed") else ("", "unknown")


# ======================== VALIDATION ========================
def validate_video(video, detail):
    status = detail.get("status", {})
    content = detail.get("contentDetails", {})
    if status.get("privacyStatus") != "public":
        return None, "not public"
    if status.get("embeddable") is False:
        return None, "not embeddable"

    seconds = duration_seconds(content.get("duration", ""))
    if seconds is None:
        return None, "unknown duration"
    if seconds < 61:
        return None, "short video"
    if not looks_like_trailer(video):
        return None, "not a trailer title"

    movie = find_tmdb_movie(video.get("title", ""))
    if not movie:
        return None, "no confident TMDB movie match"

    cleaned = get_movie_search_title(video.get("title", ""))
    movie_title = movie.get("title") or movie.get("original_title") or ""
    if similarity(cleaned, movie_title) < MIN_TITLE_SIMILARITY:
        return None, "movie title mismatch"

    rating, rating_status = movie_rating(movie)
    if rating_status == "blocked":
        return None, "blocked movie rating"

    video_id = video["video_id"]
    return {
        "id": video_id,
        "video_id": video_id,
        "title": video.get("title", ""),
        "description": video.get("description", ""),
        "channel": video.get("channel", ""),
        "published_at": video.get("published_at", ""),
        "thumbnail": video.get("thumbnail", ""),
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "embed_url": f"https://www.youtube-nocookie.com/embed/{video_id}",
        "category": "Movies",
        "rating": rating,
        "rating_status": rating_status,
        "tmdb_id": movie["id"],
        "duration_seconds": seconds,
        "official_channel_candidate": is_likely_official_channel(video),
    }, "accepted"


# ======================== MAIN ========================
def update_trailers():
    if not YOUTUBE_API_KEY:
        raise RuntimeError("Missing YOUTUBE_API_KEY repository secret.")
    if not TMDB_API_KEY:
        raise RuntimeError("Missing TMDB_API_KEY repository secret.")

    existing = load_trailers()
    by_id = {}
    for item in existing:
        video_id = item.get("video_id") or item.get("id")
        if video_id:
            by_id[str(video_id)] = item

    candidates = {}
    checked = 0
    for query in SEARCHES:
        print(f"Searching YouTube: {query}")
        for video in search_youtube(query):
            checked += 1
            candidates.setdefault(video["video_id"], video)
        time.sleep(0.2)

    print(f"Unique YouTube candidates: {len(candidates)}")
    details = get_video_details(list(candidates))
    added = rejected = 0
    reasons = {}

    for video_id, video in candidates.items():
        if video_id in by_id:
            continue
        detail = details.get(video_id)
        if not detail:
            rejected += 1
            reasons["missing video details"] = reasons.get("missing video details", 0) + 1
            continue

        record, reason = validate_video(video, detail)
        if record is None:
            rejected += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            continue

        by_id[video_id] = record
        added += 1
        print(f"ADDED: {record['title']} (rating: {record['rating'] or 'unknown'})")

    final_videos = list(by_id.values())
    final_videos.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    save_trailers(final_videos)

    print("\n========== UPDATE SUMMARY ==========")
    print(f"Candidates checked: {checked}")
    print(f"Unique candidates: {len(candidates)}")
    print(f"Actually added: {added}")
    print(f"Rejected: {rejected}")
    print(f"Total saved trailers: {len(final_videos)}")
    if reasons:
        print("\nRejection reasons:")
        for reason, count in sorted(reasons.items(), key=lambda pair: pair[1], reverse=True):
            print(f"  {reason}: {count}")
    print(f"\nSaved: {TRAILERS_FILE}")


if __name__ == "__main__":
    update_trailers()
