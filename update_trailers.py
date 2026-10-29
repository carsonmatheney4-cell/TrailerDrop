
import os
import re
import json
import time
import requests
from difflib import SequenceMatcher
from pathlib import Path

# =========================
# CONFIGURATION
# =========================

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()

BASE_DIR = Path(__file__).resolve().parent
TRAILERS_FILE = BASE_DIR / "trailers.json"

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
TMDB_API = "https://api.themoviedb.org/3"

MAX_RESULTS = 50
REQUEST_TIMEOUT = 20

# Ratings allowed on the site.
# R is allowed because an R rating alone does not prove
# that a movie contains sexual content or severe profanity.
ALLOWED_MOVIE_RATINGS = {"G", "PG", "PG-13", "R"}

# Only these ratings are automatically blocked.
BLOCKED_MOVIE_RATINGS = {"NC-17", "X"}

# Movie collections need at least this many other rated movies
# before we estimate a rating for an unrated movie.
MIN_FRANCHISE_RATINGS = 2

# Terms that indicate a video is not a suitable trailer candidate.
EXCLUDED_TITLE_TERMS = [
    "gameplay",
    "walkthrough",
    "reaction",
    "review",
    "ending explained",
    "full movie",
    "full episode",
    "compilation",
    "behind the scenes",
    "interview",
    "soundtrack",
    "lyrics",
    "fan made",
    "fanmade",
    "parody",
    "recap",
]

TRAILER_TITLE_TERMS = [
    "official trailer",
    "teaser trailer",
    "official teaser",
    "final trailer",
    "trailer",
    "teaser",
]


# =========================
# GENERAL HELPERS
# =========================

def normalize(text):
    """Normalize text for matching."""
    text = str(text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def title_similarity(first, second):
    """Return a similarity score between two titles."""
    return SequenceMatcher(
        None,
        normalize(first),
        normalize(second),
    ).ratio()


def load_trailers():
    """Load existing trailers.json."""
    if not TRAILERS_FILE.exists():
        return []

    try:
        with TRAILERS_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            for key in ("trailers", "videos", "results"):
                if isinstance(data.get(key), list):
                    return data[key]

        print("Warning: Unrecognized trailers.json format.")
        return []

    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not read trailers.json: {error}")
        return []


def save_trailers(trailers):
    """Save trailers.json safely."""
    temporary_file = TRAILERS_FILE.with_suffix(".json.tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(trailers, file, indent=2, ensure_ascii=False)

    temporary_file.replace(TRAILERS_FILE)


def get_json(url, params=None):
    """Make a GET request and return JSON or None."""
    try:
        response = requests.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 429:
            print("API rate limit reached. Waiting 3 seconds.")
            time.sleep(3)
            return None

        response.raise_for_status()
        return response.json()

    except requests.RequestException as error:
        print(f"Request failed: {error}")
        return None


# =========================
# YOUTUBE SEARCH
# =========================

def search_youtube(query, max_results=MAX_RESULTS):
    """Search YouTube for videos."""
    if not YOUTUBE_API_KEY:
        print("Missing YOUTUBE_API_KEY environment variable.")
        return []

    data = get_json(
        f"{YOUTUBE_API}/search",
        {
            "key": YOUTUBE_API_KEY,
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": min(max_results, 50),
            "safeSearch": "moderate",
            "videoEmbeddable": "true",
            "videoSyndicated": "true",
            "order": "relevance",
        },
    )

    if not data:
        return []

    results = []

    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})

        if not video_id:
            continue

        results.append({
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "channel": snippet.get("channelTitle", ""),
            "published_at": snippet.get("publishedAt", ""),
            "thumbnail": (
                snippet.get("thumbnails", {})
                .get("high", {})
                .get("url", "")
            ),
            "youtube_url": (
                f"https://www.youtube.com/watch?v={video_id}"
            ),
            "embed_url": (
                f"https://www.youtube-nocookie.com/embed/{video_id}"
            ),
        })

    return results


# =========================
# TRAILER FILTERING
# =========================

def looks_like_trailer(video):
    """Check whether a video title looks like a trailer."""
    title = normalize(video.get("title", ""))

    if not title:
        return False

    if any(term in title for term in EXCLUDED_TITLE_TERMS):
        return False

    return any(term in title for term in TRAILER_TITLE_TERMS)


def is_movie_category(category):
    """Identify movie-related categories."""
    category = normalize(category)

    return any(
        word in category
        for word in ("movie", "film", "cinema")
    )


# =========================
# TMDB MOVIE RATINGS
# =========================

def tmdb_request(endpoint, params=None):
    """Make a TMDB API request."""
    if not TMDB_API_KEY:
        print("Missing TMDB_API_KEY environment variable.")
        return None

    request_params = dict(params or {})
    request_params["api_key"] = TMDB_API_KEY

    return get_json(
        f"{TMDB_API}{endpoint}",
        request_params,
    )


def find_tmdb_movie(title):
    """Find the closest matching movie title on TMDB."""
    data = tmdb_request(
        "/search/movie",
        {
            "query": title,
            "include_adult": "false",
            "language": "en-US",
        },
    )

    if not data:
        return None

    best_movie = None
    best_score = 0.0

    for movie in data.get("results", []):
        candidate_title = (
            movie.get("title")
            or movie.get("original_title")
            or ""
        )

        score = title_similarity(title, candidate_title)

        if score > best_score:
            best_score = score
            best_movie = movie

    if best_score < 0.80:
        return None

    return best_movie


def get_us_certifications(movie_id):
    """Get US movie certifications from TMDB."""
    data = tmdb_request(
        f"/movie/{movie_id}/release_dates"
    )

    if not data:
        return []

    certifications = []

    for country in data.get("results", []):
        if country.get("iso_3166_1") != "US":
            continue

        for release in country.get("release_dates", []):
            certification = (
                release.get("certification") or ""
            ).strip().upper()

            if certification:
                certifications.append(certification)

    return list(dict.fromkeys(certifications))


def confirmed_rating(certifications):
    """
    Return a confirmed rating.

    NC-17 and X are blocked.
    G, PG, PG-13, and R are allowed.
    Unknown certifications do not automatically block a movie.
    """
    normalized = {
        str(cert).strip().upper()
        for cert in certifications
        if cert
    }

    if normalized & BLOCKED_MOVIE_RATINGS:
        return "", "blocked"

    for rating in ("G", "PG", "PG-13", "R"):
        if rating in normalized:
            return rating, "confirmed"

    return "", "unknown"


# =========================
# FRANCHISE RATING ESTIMATE
# =========================

def franchise_estimate(collection_id, current_movie_id):
    """
    Estimate an unrated movie's rating from rated movies
    in the same TMDB collection.

    This is only an estimate, not a content advisory.
    """
    if not collection_id:
        return ""

    collection = tmdb_request(
        f"/collection/{collection_id}",
        {"language": "en-US"},
    )

    if not collection:
        return ""

    rated_movies = []

    for movie in collection.get("parts", []):
        movie_id = movie.get("id")

        if not movie_id or movie_id == current_movie_id:
            continue

        certifications = get_us_certifications(movie_id)
        rating, status = confirmed_rating(certifications)

        if status == "confirmed":
            rated_movies.append(rating)

        time.sleep(0.1)

    if len(rated_movies) < MIN_FRANCHISE_RATINGS:
        return ""

    # Use the most restrictive rating among the rated movies.
    rating_order = ["G", "PG", "PG-13", "R"]

    return max(
        rated_movies,
        key=lambda rating: rating_order.index(rating),
    )


def movie_rating(title):
    """
    Return (rating, rating_status).

    Possible statuses:
    confirmed, estimated, unknown, blocked
    """
    movie = find_tmdb_movie(title)

    if not movie:
        return "", "unknown"

    movie_id = movie.get("id")

    if not movie_id:
        return "", "unknown"

    certifications = get_us_certifications(movie_id)
    rating, status = confirmed_rating(certifications)

    if status == "blocked":
        return "", "blocked"

    if status == "confirmed":
        return rating, "confirmed"

    collection_id = (
        movie.get("belongs_to_collection") or {}
    ).get("id")

    estimated_rating = franchise_estimate(
        collection_id,
        movie_id,
    )

    if estimated_rating:
        return estimated_rating, "estimated"

    return "", "unknown"


# =========================
# VIDEO APPROVAL
# =========================

def allowed_video(video, category="movie"):
    """
    Decide whether a video can be added.

    R-rated movies are allowed.
    NC-17/X-rated movies are blocked.
    Unknown movie ratings are allowed.
    """
    if not looks_like_trailer(video):
        return False

    if not is_movie_category(category):
        video["rating"] = ""
        video["rating_status"] = "not_applicable"
        return True

    rating, status = movie_rating(
        video.get("title", "")
    )

    if status == "blocked":
        return False

    if rating in BLOCKED_MOVIE_RATINGS:
        return False

    if rating in ALLOWED_MOVIE_RATINGS:
        video["rating"] = rating
        video["rating_status"] = status
        return True

    # Unknown rating: keep the trailer rather than automatically
    # excluding it. This does NOT mean its content was verified.
    video["rating"] = ""
    video["rating_status"] = "unknown"
    return True


# =========================
# BUILD TRAILER RECORDS
# =========================

def make_video(video, category="movie"):
    """Prepare a video record for trailers.json."""
    if not allowed_video(video, category):
        return None

    return {
        "id": video.get("video_id", ""),
        "video_id": video.get("video_id", ""),
        "title": video.get("title", ""),
        "description": video.get("description", ""),
        "channel": video.get("channel", ""),
        "published_at": video.get("published_at", ""),
        "thumbnail": video.get("thumbnail", ""),
        "youtube_url": video.get("youtube_url", ""),
        "embed_url": video.get("embed_url", ""),
        "category": category,
        "rating": video.get("rating", ""),
        "rating_status": video.get(
            "rating_status", "unknown"
        ),
    }


# =========================
# UPDATE TRAILERS
# =========================

def update_trailers():
    """
    Search for movie trailers and merge them into trailers.json.

    Set TRAILER_SEARCHES to a JSON array environment variable
    to customize the searches.
    """
    raw_searches = os.getenv("TRAILER_SEARCHES", "").strip()

    if raw_searches:
        try:
            searches = json.loads(raw_searches)
        except json.JSONDecodeError:
            print("TRAILER_SEARCHES must be a valid JSON array.")
            return
    else:
        searches = [
            {"query": "official movie trailer", "category": "movie"},
            {"query": "official movie teaser trailer", "category": "movie"},
            {"query": "new movie official trailer", "category": "movie"},
        ]

    existing = load_trailers()

    # Keep existing records and update by YouTube video ID.
    videos_by_id = {}

    for item in existing:
        video_id = (
            item.get("video_id")
            or item.get("id")
        )

        if video_id:
            videos_by_id[video_id] = item

    added = 0
    checked = 0

    for search in searches:
        query = str(search.get("query", "")).strip()
        category = str(
            search.get("category", "movie")
        ).strip()

        if not query:
            continue

        print(f"Searching YouTube: {query}")

        results = search_youtube(query)

        for video in results:
            checked += 1
            video_id = video.get("video_id")

            if not video_id or video_id in videos_by_id:
                continue

            record = make_video(video, category)

            if record is None:
                continue

            videos_by_id[video_id] = record
            added += 1

        time.sleep(0.2)

    final_videos = list(videos_by_id.values())

    # Newest first when publication dates are available.
    final_videos.sort(
        key=lambda item: item.get("published_at", ""),
        reverse=True,
    )

    save_trailers(final_videos)

    print(f"Checked: {checked}")
    print(f"Added: {added}")
    print(f"Total trailers: {len(final_videos)}")
    print(f"Saved to: {TRAILERS_FILE}")


if __name__ == "__main__":
    update_trailers()
