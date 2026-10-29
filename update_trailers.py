
import os
import re
import json
import time
import requests
from pathlib import Path
from difflib import SequenceMatcher
from datetime import datetime, timezone

# ============================================================
# CONFIGURATION
# ============================================================

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()

BASE_DIR = Path(__file__).resolve().parent
TRAILERS_FILE = BASE_DIR / "trailers.json"

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
TMDB_API = "https://api.themoviedb.org/3"

REQUEST_TIMEOUT = 20
MAX_RESULTS = 25

ALLOWED_MOVIE_RATINGS = {"G", "PG", "PG-13", "R"}
BLOCKED_MOVIE_RATINGS = {"NC-17", "X"}

MIN_TITLE_SIMILARITY = 0.78

EXCLUDED_TERMS = [
    "gameplay", "walkthrough", "reaction", "review",
    "ending explained", "full movie", "full episode",
    "compilation", "behind the scenes", "interview",
    "soundtrack", "lyrics", "fan made", "fanmade",
    "parody", "recap", "lego mario", "super mario gameplay",
    "game trailer", "gaming trailer", "minecraft",
    "roblox", "fortnite", "shorts", "#shorts",
    "edit", "fan edit", "concept trailer", "fake trailer",
    "honest trailer", "trailer reaction", "movie clips",
    "deleted scene", "scene pack", "movie scenes",
]

TRAILER_TERMS = [
    "official trailer", "official teaser",
    "official teaser trailer", "official final trailer",
    "official theatrical trailer", "teaser trailer",
    "final trailer", "official trailer",
    "trailer", "teaser",
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


# ============================================================
# HELPERS
# ============================================================

def normalize(value):
    value = str(value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def similarity(first, second):
    return SequenceMatcher(
        None, normalize(first), normalize(second)
    ).ratio()


def get_json(url, params=None):
    try:
        response = requests.get(
            url, params=params, timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 429:
            print("Rate limited by API; waiting 5 seconds.")
            time.sleep(5)
            return None

        response.raise_for_status()
        return response.json()

    except requests.RequestException as error:
        print(f"API request failed: {error}")
        return None


def youtube_request(endpoint, params):
    if not YOUTUBE_API_KEY:
        raise RuntimeError("Missing YOUTUBE_API_KEY secret.")

    params = dict(params)
    params["key"] = YOUTUBE_API_KEY

    return get_json(f"{YOUTUBE_API}/{endpoint}", params)


def tmdb_request(endpoint, params=None):
    if not TMDB_API_KEY:
        raise RuntimeError("Missing TMDB_API_KEY secret.")

    params = dict(params or {})
    params["api_key"] = TMDB_API_KEY

    return get_json(f"{TMDB_API}{endpoint}", params)


# ============================================================
# JSON FILE
# ============================================================


def load_trailers():
    if not TRAILERS_FILE.exists():
        return []

    try:
        with TRAILERS_FILE.open("r", encoding="utf-8") as file:
            content = file.read()

        print("LAST 100 CHARACTERS:", repr(content[-100:]))
        print("LAST CHARACTER:", repr(content[-1:]))
        print("ENDS WITH ]:", content.rstrip().endswith("]"))

        try:
            data = json.loads(content)

        except json.JSONDecodeError as error:
            start = max(0, error.pos - 250)
            end = min(len(content), error.pos + 250)

            print("\n========== JSON ERROR DETAILS ==========")
            print(f"File: {TRAILERS_FILE}")
            print(f"Error: {error.msg}")
            print(f"Line: {error.lineno}")
            print(f"Column: {error.colno}")
            print(f"Character position: {error.pos}")
            print(f"File length: {len(content)} characters")
            print("\nText surrounding the error:")
            print(repr(content[start:end]))
            print("\n========== END JSON ERROR DETAILS ==========")

            raise

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            for key in ("videos", "trailers", "results"):
                if isinstance(data.get(key), list):
                    return data[key]

        raise ValueError("Unrecognized trailers.json structure.")

    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(
            f"Could not safely load trailers.json: {error}"
        ) from error

            print(f"File length: {len(content)} characters")
            print("\nText surrounding the error:")
            print(repr(content[start:end]))
            print("\n========== END JSON ERROR DETAILS ==========")

            raise

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            for key in ("videos", "trailers", "results"):
                if isinstance(data.get(key), list):
                    return data[key]

        raise ValueError("Unrecognized trailers.json structure.")

    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(
            f"Could not safely load trailers.json: {error}"
        ) from error


def save_trailers(videos):
    temporary = TRAILERS_FILE.with_suffix(".json.tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(videos, file, indent=2, ensure_ascii=False)

    temporary.replace(TRAILERS_FILE)


# ============================================================
# YOUTUBE SEARCH AND VIDEO DETAILS
# ============================================================

def search_youtube(query):
    data = youtube_request(
        "/search",
        {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": MAX_RESULTS,
            "safeSearch": "moderate",
            "videoEmbeddable": "true",
            "videoSyndicated": "true",
            "order": "date",
        },
    )

    results = []

    for item in (data or {}).get("items", []):
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})

        if not video_id:
            continue

        results.append({
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "channel": snippet.get("channelTitle", ""),
            "channel_id": snippet.get("channelId", ""),
            "published_at": snippet.get("publishedAt", ""),
            "thumbnail": (
                snippet.get("thumbnails", {})
                .get("high", {})
                .get("url", "")
            ),
        })

    return results


def get_video_details(video_ids):
    details = {}

    for start in range(0, len(video_ids), 50):
        batch = video_ids[start:start + 50]

        data = youtube_request(
            "/videos",
            {
                "part": "contentDetails,snippet,status",
                "id": ",".join(batch),
                "maxResults": 50,
            },
        )

        for item in (data or {}).get("items", []):
            details[item["id"]] = item

    return details


def duration_seconds(duration):
    """Convert ISO 8601 YouTube duration to seconds."""
    match = re.fullmatch(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        duration or "",
    )

    if not match:
        return None

    hours, minutes, seconds = (
        int(value or 0) for value in match.groups()
    )

    return hours * 3600 + minutes * 60 + seconds


# ============================================================
# FILTERING
# ============================================================

def looks_like_trailer(video):
    title = normalize(video.get("title", ""))
    description = normalize(video.get("description", ""))

    if not title:
        return False

    if any(term in title for term in EXCLUDED_TERMS):
        return False

    if any(term in description for term in [
        "gameplay walkthrough",
        "reaction channel",
        "fan made trailer",
        "concept trailer",
    ]):
        return False

    return any(
        normalize(term) in title
        for term in TRAILER_TERMS
    )


def is_likely_official_channel(video):
    """
    Heuristic only: channel names are not proof of authenticity.
    Known studio/distributor names receive priority.
    """
    channel = normalize(video.get("channel", ""))

    official_names = [
        "warner bros", "warner brothers", "sony pictures",
        "sony pictures entertainment", "universal pictures",
        "universal studios", "paramount pictures",
        "walt disney studios", "disney", "marvel entertainment",
        "20th century studios", "searchlight pictures",
        "lionsgate movies", "lionsgate", "netflix",
        "amazon mgm studios", "prime video", "a24",
        "focus features", "dreamworks", "illumination",
        "pixar", "walt disney animation", "sony pictures animation",
        "studio ghibli", "crunchyroll",
    ]

    return any(name in channel for name in official_names)


def get_movie_search_title(title):
    """Remove trailer wording and year suffixes from a video title."""
    value = str(title or "")

    value = re.sub(
        r"\b(official\s+)?(final\s+|teaser\s+|"
        r"theatrical\s+)?trailer\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\bofficial\s+teaser\b|\bteaser\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\b(4k|hd|official video)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(r"\(\s*\d{4}\s*\)", " ", value)
    value = re.sub(r"\[\s*\d{4}\s*\]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -|:[]()")

    return value


# ============================================================
# TMDB MOVIE MATCHING AND RATINGS
# ============================================================

def find_tmdb_movie(title):
    search_title = get_movie_search_title(title)

    if len(normalize(search_title)) < 2:
        return None

    data = tmdb_request(
        "/search/movie",
        {
            "query": search_title,
            "include_adult": "false",
            "language": "en-US",
        },
    )

    if not data:
        return None

    best_movie = None
    best_score = 0

    for movie in data.get("results", []):
        candidate = (
            movie.get("title")
            or movie.get("original_title")
            or ""
        )

        score = similarity(search_title, candidate)

        if normalize(search_title) == normalize(candidate):
            score = 1.0

        if score > best_score:
            best_score = score
            best_movie = movie

    if best_score < MIN_TITLE_SIMILARITY:
        return None

    return best_movie


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
    certifications = {
        str(cert).strip().upper()
        for cert in certifications if cert
    }

    if certifications & BLOCKED_MOVIE_RATINGS:
        return "", "blocked"

    for rating in ("G", "PG", "PG-13", "R"):
        if rating in certifications:
            return rating, "confirmed"

    return "", "unknown"


def movie_rating(movie):
    movie_id = movie.get("id")

    if not movie_id:
        return "", "unknown"

    rating, status = confirmed_rating(
        get_us_certifications(movie_id)
    )

    if status == "blocked" or status == "confirmed":
        return rating, status

    return "", "unknown"


# ============================================================
# ACCEPT AND BUILD RECORDS
# ============================================================

def validate_video(video, detail):
    title = video.get("title", "")
    content = detail.get("contentDetails", {})
    status = detail.get("status", {})

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

    movie = find_tmdb_movie(title)

    if not movie:
        return None, "no confident TMDB movie match"

    cleaned_title = get_movie_search_title(title)
    tmdb_title = movie.get("title") or movie.get("original_title") or ""

    if similarity(cleaned_title, tmdb_title) < MIN_TITLE_SIMILARITY:
        return None, "movie title mismatch"

    rating, rating_status = movie_rating(movie)

    if rating_status == "blocked":
        return None, "blocked movie rating"

    return {
        "id": video["video_id"],
        "video_id": video["video_id"],
        "title": title,
        "description": video.get("description", ""),
        "channel": video.get("channel", ""),
        "published_at": video.get("published_at", ""),
        "thumbnail": video.get("thumbnail", ""),
        "youtube_url": (
            f"https://www.youtube.com/watch?v={video['video_id']}"
        ),
        "embed_url": (
            f"https://www.youtube-nocookie.com/embed/{video['video_id']}"
        ),
        "category": "Movies",
        "rating": rating,
        "rating_status": rating_status,
        "tmdb_id": movie["id"],
        "duration_seconds": seconds,
        "official_channel_candidate": is_likely_official_channel(video),
    }, "accepted"


# ============================================================
# MAIN UPDATE
# ============================================================

def update_trailers():
    if not YOUTUBE_API_KEY:
        raise RuntimeError("Missing YOUTUBE_API_KEY repository secret.")

    if not TMDB_API_KEY:
        raise RuntimeError("Missing TMDB_API_KEY repository secret.")

    existing = load_trailers()
    videos_by_id = {}

    for item in existing:
        if not isinstance(item, dict):
            continue

        video_id = item.get("video_id") or item.get("id")

        if video_id:
            videos_by_id[video_id] = item

    candidates = {}
    checked = 0

    for query in SEARCHES:
        print(f"Searching YouTube: {query}")

        for video in search_youtube(query):
            checked += 1
            candidates.setdefault(video["video_id"], video)

        time.sleep(0.2)

    print(f"Unique YouTube candidates: {len(candidates)}")

    details = get_video_details(list(candidates.keys()))

    added = 0
    rejected = 0
    reasons = {}

    for video_id, video in candidates.items():
        if video_id in videos_by_id:
            continue

        detail = details.get(video_id)

        if not detail:
            rejected += 1
            reasons["missing video details"] = (
                reasons.get("missing video details", 0) + 1
            )
            continue

        record, reason = validate_video(video, detail)

        if record is None:
            rejected += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            continue

        videos_by_id[video_id] = record
        added += 1

        print(
            f"ADDED: {record['title']} "
            f"(rating: {record['rating'] or 'unknown'})"
        )

    final_videos = list(videos_by_id.values())

    final_videos.sort(
        key=lambda item: item.get("published_at", ""),
        reverse=True,
    )

    save_trailers(final_videos)

    print("\n========== UPDATE SUMMARY ==========")
    print(f"Candidates checked: {checked}")
    print(f"Unique candidates: {len(candidates)}")
    print(f"Actually added: {added}")
    print(f"Rejected: {rejected}")
    print(f"Total saved trailers: {len(final_videos)}")

    if reasons:
        print("\nRejection reasons:")
        for reason, count in sorted(
            reasons.items(),
            key=lambda item: item[1],
            reverse=True,
        ):
            print(f"  {reason}: {count}")

    print(f"\nSaved: {TRAILERS_FILE}")


if __name__ == "__main__":
    update_trailers()
