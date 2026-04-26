"""Tool implementations for the dinner-rez agent.

External APIs used:
- Google Places (Text Search) for discovery
- Google Routes (computeRoutes) for travel-time estimates
- Resy /4/find for availability (unofficial; public web key)
- OpenTable: deep-link only — their API is locked behind Akamai bot mgmt
  (HTML page hangs, all dapi probes 403/404). Real availability would need
  a browser-automation layer; not worth it for v1.

prefs.md and history.md are read/written from disk every call.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("tools")

ROOT = Path(__file__).parent
PREFS_PATH = ROOT / "prefs.md"
HISTORY_PATH = ROOT / "history.md"
BELI_PATH = ROOT / "beli.md"
BOOKINGS_PATH = ROOT / "bookings.md"

# Read env at call time (not module load) so import order doesn't matter —
# bot.py used to import this before load_dotenv(), which captured "" here.
def _google_key() -> str:
    return os.environ.get("GOOGLE_PLACES_API_KEY", "")


def _resy_key() -> str:
    # Public Resy web API key — pulled from their site. Override via env to rotate.
    return os.environ.get("RESY_API_KEY", "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5")


# ---------- prefs / history ----------

def read_prefs() -> str:
    return PREFS_PATH.read_text() if PREFS_PATH.exists() else ""


def append_pref(line: str) -> str:
    line = line.strip()
    if not line:
        return "empty pref, ignored"
    with PREFS_PATH.open("a") as f:
        f.write(f"\n- {line}")
    return f"saved: {line}"


def log_history(entry: str) -> str:
    stamp = datetime.now().isoformat(timespec="minutes")
    with HISTORY_PATH.open("a") as f:
        f.write(f"\n- [{stamp}] {entry}")
    return "logged"


# ---------- bookings ----------

def read_bookings() -> str:
    """Return the bookings file. Use this when the user shares a rating but
    doesn't name the venue — match against the most recent booking."""
    return BOOKINGS_PATH.read_text() if BOOKINGS_PATH.exists() else ""


def _calendar_url(
    venue: str,
    date: str | None,
    time: str | None,
    party_size: int | None,
    notes: str | None,
) -> str | None:
    """Build a Google Calendar 'add event' URL for a reservation.
    Returns None if date/time can't be parsed; caller skips the link in that case."""
    if not date or not time:
        return None
    try:
        d = datetime.strptime(date.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None
    t_norm = time.strip().lower().replace(" ", "")
    start_time = None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            start_time = datetime.strptime(t_norm, fmt).time()
            break
        except ValueError:
            continue
    if start_time is None:
        return None
    start = datetime.combine(d, start_time)
    end = start + timedelta(hours=2)
    stamp_fmt = "%Y%m%dT%H%M%S"
    details_parts = []
    if party_size:
        details_parts.append(f"Party of {party_size}")
    if notes:
        details_parts.append(notes.strip())
    params = {
        "action": "TEMPLATE",
        "text": venue,
        "dates": f"{start.strftime(stamp_fmt)}/{end.strftime(stamp_fmt)}",
        "location": venue,
        "ctz": os.environ.get("CALENDAR_TZ", "America/New_York"),
    }
    if details_parts:
        params["details"] = " — ".join(details_parts)
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


def log_booking(
    venue: str,
    date: str | None = None,
    time: str | None = None,
    party_size: int | None = None,
    notes: str | None = None,
) -> str:
    """Record a confirmed reservation. Call when user says 'I booked X',
    'going with the dutch', 'let's do raoul's at 8', etc."""
    venue = (venue or "").strip()
    if not venue:
        return "empty venue, ignored"
    stamp = datetime.now().isoformat(timespec="minutes")
    parts = [venue]
    if party_size:
        parts.append(f"— party of {party_size}")
    when_bits = [b for b in [date, time] if b]
    if when_bits:
        parts.append(f"@ {' '.join(when_bits)}")
    if notes:
        parts.append(f"({notes.strip()})")
    line = " ".join(parts)
    with BOOKINGS_PATH.open("a") as f:
        f.write(f"\n- [{stamp}] {line}")
    result = f"booking saved: {line}"
    cal_url = _calendar_url(venue, date, time, party_size, notes)
    if cal_url:
        result += f"\ncalendar_url: {cal_url}"
    return result


# ---------- beli ----------

def read_beli() -> str:
    return BELI_PATH.read_text() if BELI_PATH.exists() else ""


def log_beli(name: str, score: float | None = None, notes: str | None = None) -> str:
    """Append a Beli rating to beli.md under '## Been'.

    score: 0-10 Beli rating (omit for want-to-try)
    notes: optional cuisine, neighborhood, vibe
    """
    name = (name or "").strip()
    if not name:
        return "empty name, ignored"

    parts = [name]
    if score is not None:
        parts.append(f"— {score}")
    if notes:
        parts.append(f"({notes.strip()})")
    line = " ".join(parts)

    text = read_beli()
    if not text:
        # cold-start: create the file with the header
        BELI_PATH.write_text("# Beli\n\n## Been\n\n## Want to try\n")
        text = read_beli()

    # Insert under the right heading
    target = "## Been" if score is not None else "## Want to try"
    if target not in text:
        # heading missing — append at end
        with BELI_PATH.open("a") as f:
            f.write(f"\n{target}\n- {line}\n")
        return f"saved (new section): {line}"

    # Insert right after the target heading line
    lines = text.splitlines()
    out = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and ln.strip() == target:
            out.append(f"- {line}")
            inserted = True
    BELI_PATH.write_text("\n".join(out) + "\n")
    return f"saved: {line}"


# ---------- neighborhood resolver ----------

# Hand-picked centroids for NYC areas the user actually dines in. Saves a
# Places geocoding hop and gives more consistent location_bias than letting
# Google interpret "SoHo" inside a free-text query. Lower-case keys; "."
# stripped so "St. George" and "St George" both match.
NEIGHBORHOODS: dict[str, str] = {
    # Manhattan
    "soho": "40.7233,-74.0030",
    "nolita": "40.7227,-73.9956",
    "tribeca": "40.7163,-74.0086",
    "west village": "40.7358,-74.0036",
    "east village": "40.7264,-73.9818",
    "greenwich village": "40.7336,-74.0027",
    "lower east side": "40.7150,-73.9843",
    "les": "40.7150,-73.9843",
    "chinatown": "40.7158,-73.9970",
    "two bridges": "40.7115,-73.9930",
    "financial district": "40.7075,-74.0090",
    "fidi": "40.7075,-74.0090",
    "chelsea": "40.7465,-74.0014",
    "flatiron": "40.7411,-73.9897",
    "nomad": "40.7449,-73.9882",
    "gramercy": "40.7378,-73.9844",
    "murray hill": "40.7479,-73.9776",
    "midtown": "40.7549,-73.9840",
    "midtown east": "40.7549,-73.9700",
    "midtown west": "40.7589,-73.9851",
    "hell's kitchen": "40.7638,-73.9918",
    "hells kitchen": "40.7638,-73.9918",
    "upper east side": "40.7736,-73.9566",
    "ues": "40.7736,-73.9566",
    "upper west side": "40.7870,-73.9754",
    "uws": "40.7870,-73.9754",
    "harlem": "40.8116,-73.9465",
    "washington heights": "40.8417,-73.9393",
    # Brooklyn
    "williamsburg": "40.7081,-73.9571",
    "south williamsburg": "40.7045,-73.9595",
    "greenpoint": "40.7301,-73.9540",
    "bushwick": "40.6944,-73.9213",
    "bed-stuy": "40.6872,-73.9418",
    "bedford-stuyvesant": "40.6872,-73.9418",
    "crown heights": "40.6694,-73.9442",
    "prospect heights": "40.6776,-73.9690",
    "park slope": "40.6710,-73.9814",
    "fort greene": "40.6890,-73.9737",
    "clinton hill": "40.6884,-73.9657",
    "downtown brooklyn": "40.6928,-73.9851",
    "brooklyn heights": "40.6960,-73.9933",
    "dumbo": "40.7033,-73.9881",
    "cobble hill": "40.6859,-73.9968",
    "boerum hill": "40.6849,-73.9863",
    "carroll gardens": "40.6797,-73.9994",
    "red hook": "40.6743,-74.0099",
    "gowanus": "40.6731,-73.9897",
    "sunset park": "40.6450,-74.0102",
    "bay ridge": "40.6260,-74.0331",
    # Queens
    "long island city": "40.7447,-73.9485",
    "lic": "40.7447,-73.9485",
    "astoria": "40.7720,-73.9302",
    "sunnyside": "40.7434,-73.9196",
}


def resolve_neighborhood(name: str) -> str | None:
    """Return 'lat,lng' for a known neighborhood, or None."""
    if not name:
        return None
    key = name.lower().replace(".", "").strip()
    return NEIGHBORHOODS.get(key)


# ---------- discovery: Google Places ----------

def search_restaurants(
    query: str,
    location_bias: str | None = None,
    neighborhood: str | None = None,
    max_results: int = 8,
) -> list[dict]:
    """Text-search restaurants. Returns name/address/rating/price/lat/lng/place_id.

    `neighborhood` (e.g. "SoHo", "Williamsburg") is resolved against a built-in
    NYC dict and overrides `location_bias` when matched. Saves a Places
    geocoding hop and gives more consistent results than free-text queries.
    """
    key = _google_key()
    if not key:
        return [{"error": "GOOGLE_PLACES_API_KEY not set"}]

    if neighborhood:
        resolved = resolve_neighborhood(neighborhood)
        if resolved:
            location_bias = resolved
        else:
            log.info("unknown neighborhood %r, falling back to query text", neighborhood)

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.rating,places.userRatingCount,places.priceLevel,"
            "places.location,places.primaryTypeDisplayName,"
            "places.editorialSummary,places.websiteUri"
        ),
    }
    body: dict = {"textQuery": query, "maxResultCount": max_results}
    if location_bias:
        body["locationBias"] = {"circle": {"center": _parse_latlng(location_bias),
                                            "radius": 3000.0}}

    with httpx.Client(timeout=15.0) as client:
        r = client.post(url, headers=headers, json=body)
    if r.status_code != 200:
        return [{"error": f"places api {r.status_code}: {r.text[:200]}"}]

    out = []
    for p in r.json().get("places", []):
        out.append({
            "place_id": p.get("id"),
            "name": (p.get("displayName") or {}).get("text"),
            "address": p.get("formattedAddress"),
            "rating": p.get("rating"),
            "n_reviews": p.get("userRatingCount"),
            "price": p.get("priceLevel"),
            "type": (p.get("primaryTypeDisplayName") or {}).get("text"),
            "summary": (p.get("editorialSummary") or {}).get("text"),
            "website": p.get("websiteUri"),
            "lat": (p.get("location") or {}).get("latitude"),
            "lng": (p.get("location") or {}).get("longitude"),
        })
    return out


def _parse_latlng(s: str) -> dict:
    m = re.match(r"\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*", s)
    if not m:
        raise ValueError(f"expected 'lat,lng', got: {s!r}")
    return {"latitude": float(m.group(1)), "longitude": float(m.group(2))}


# ---------- travel time: Google Routes ----------

_LATLNG_RE = re.compile(r"^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$")

_MODE_MAP = {
    "transit": "TRANSIT",
    "walking": "WALK",
    "walk": "WALK",
    "driving": "DRIVE",
    "drive": "DRIVE",
    "bicycling": "BICYCLE",
    "bicycle": "BICYCLE",
    "bike": "BICYCLE",
}


def _waypoint(s: str) -> dict:
    """Routes API waypoint from either 'lat,lng' or a free-text address."""
    m = _LATLNG_RE.match(s)
    if m:
        return {"location": {"latLng": {
            "latitude": float(m.group(1)),
            "longitude": float(m.group(2)),
        }}}
    return {"address": s}


def travel_time(
    origin: str,
    destination: str,
    mode: str = "transit",
    date: str | None = None,
    time_hhmm: str | None = None,
) -> dict:
    """Estimate travel time between two points via Google Routes API.

    origin/destination: address string OR 'lat,lng'.
    mode: transit | walking | driving | bicycling.
    date (YYYY-MM-DD) + time_hhmm ('1930'): optional departure. Required for
      traffic-aware driving. Past times are dropped (Routes rejects them for
      transit) and the call falls back to 'depart now'.
    """
    key = _google_key()
    if not key:
        return {"error": "GOOGLE_PLACES_API_KEY not set"}

    travel_mode = _MODE_MAP.get(mode.lower())
    if not travel_mode:
        return {"error": f"unknown mode: {mode!r}"}

    body: dict = {
        "origin": _waypoint(origin),
        "destination": _waypoint(destination),
        "travelMode": travel_mode,
    }
    if travel_mode == "DRIVE":
        body["routingPreference"] = "TRAFFIC_AWARE"

    if date and time_hhmm and len(time_hhmm) == 4 and time_hhmm.isdigit():
        try:
            tz = ZoneInfo(os.environ.get("CALENDAR_TZ", "America/New_York"))
            d = datetime.strptime(date, "%Y-%m-%d").date()
            hh, mm = int(time_hhmm[:2]), int(time_hhmm[2:])
            dep = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            if dep > datetime.now(tz) + timedelta(minutes=1):
                body["departureTime"] = (
                    dep.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                )
        except ValueError:
            pass

    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(url, headers=headers, json=body)
    except httpx.RequestError as e:
        return {"error": f"routes request failed: {e}"}

    if r.status_code != 200:
        return {"error": f"routes api {r.status_code}: {r.text[:200]}"}

    routes = r.json().get("routes") or []
    if not routes:
        return {"error": "no route found", "mode": mode}

    route = routes[0]
    dur_str = route.get("duration", "")  # e.g. "1234s"
    dur_sec = int(dur_str.rstrip("s")) if dur_str.endswith("s") else None
    distance_m = route.get("distanceMeters")

    return {
        "mode": mode,
        "duration_min": round(dur_sec / 60) if dur_sec is not None else None,
        "distance_m": distance_m,
        "distance_mi": round(distance_m / 1609.34, 1) if distance_m else None,
    }


# ---------- availability: Resy ----------

def resy_find(
    name: str,
    lat: float,
    lng: float,
    date: str,
    time_hhmm: str,
    party_size: int,
) -> dict:
    """Find availability for a venue near a point on Resy.

    date: YYYY-MM-DD
    time_hhmm: '1900' for 7pm
    Returns {found, slug, slots:[{time, type, token}], booking_url}
    """
    url = "https://api.resy.com/4/find"
    params = {
        "lat": lat,
        "long": lng,
        "day": date,
        "party_size": party_size,
        "query": name,
        # Critical: without limit, NYC-area queries return ~120 MB of JSON
        # (every venue + every slot) and time out. Resy's `query` param is a
        # loose filter so target venue may sit ~30-50 deep — 60 covers it
        # while staying ~2 MB / 1-2s.
        "limit": 60,
    }
    headers = {
        "Authorization": f'ResyAPI api_key="{_resy_key()}"',
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": "https://resy.com",
        "Referer": "https://resy.com/",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, params=params, headers=headers)
    except httpx.RequestError as e:
        return {"found": False, "error": f"resy request failed: {e}"}

    if r.status_code != 200:
        return {"found": False, "error": f"resy {r.status_code}", "body": r.text[:200]}

    data = r.json()
    venues = (data.get("results") or {}).get("venues", []) or []
    if not venues:
        return {"found": False, "venue_name": None}

    # Match on actual name overlap. The Resy /find endpoint returns lots of
    # nearby venues regardless of the `query` param, so a default-to-first
    # would silently lie about coverage. Require ≥2 shared word tokens, OR
    # exact substring match.
    target = name.lower().strip()
    target_words = {w for w in re.split(r"\W+", target) if len(w) > 2}

    venue = None
    for v_wrap in venues:
        vname = (v_wrap.get("venue", {}).get("name") or "").lower()
        if not vname:
            continue
        if target in vname or vname in target:
            venue = v_wrap
            break
        vwords = {w for w in re.split(r"\W+", vname) if len(w) > 2}
        if len(target_words & vwords) >= max(1, min(len(target_words), 2)):
            venue = v_wrap
            break

    if venue is None:
        return {"found": False, "venue_name": None}

    v = venue.get("venue", {})

    # Filter slots to within 90 min of target; sort by closeness.
    target_minutes = int(time_hhmm[:2]) * 60 + int(time_hhmm[2:])
    raw_slots = []
    for s in venue.get("slots", []) or []:
        config = s.get("config", {}) or {}
        start = (s.get("date", {}) or {}).get("start") or ""
        # start = "YYYY-MM-DD HH:MM:SS"
        if len(start) < 19:
            continue
        hh = int(start[11:13])
        mm = int(start[14:16])
        slot_minutes = hh * 60 + mm
        delta = abs(slot_minutes - target_minutes)
        if delta > 90:
            continue
        raw_slots.append((delta, {
            "time": f"{hh:02d}:{mm:02d}",
            "type": config.get("type"),
            "token": config.get("token"),
        }))
    raw_slots.sort(key=lambda x: x[0])
    slots = [s for _, s in raw_slots[:6]]

    slug = (v.get("url_slug") or "").strip()
    city = (v.get("location", {}) or {}).get("url_slug", "")
    # Resy's canonical venue URL uses /venues/. The legacy /cities/<city>/<slug>
    # form silently 200s with the SPA shell but often won't route correctly.
    booking_url = (
        f"https://resy.com/cities/{city}/venues/{slug}?date={date}&seats={party_size}"
        if (slug and city)
        else None
    )
    return {
        "found": bool(slots),
        "venue_name": v.get("name"),
        "neighborhood": (v.get("location", {}) or {}).get("neighborhood"),
        "slug": slug,
        "slots": slots,
        "booking_url": booking_url,
        "platform": "resy",
    }


# ---------- availability: OpenTable (deep-link only) ----------

def reservation_search_url(
    name: str,
    date: str,
    time_hhmm: str,
    party_size: int,
    neighborhood: str | None = None,
) -> dict:
    """Build a Google search URL as the fallback when Resy doesn't have the venue.

    Why not direct OpenTable: their /r/<slug> URLs aren't deterministic from
    name (e.g. "central-park-boathouse-new-york-2") and their /s?term= search
    route is unreliable. Their site is also bot-blocked from server-side
    verification, so we can't verify a guessed URL works.

    Google search lands the user on the right OT/Resy/widget page in one tap.
    """
    parts = [name]
    if neighborhood:
        parts.append(neighborhood)
    parts.append("reservations")
    qs = urllib.parse.urlencode({"q": " ".join(parts)})
    return {
        "found": None,  # availability not verified — user sees it on tap-through
        "platform": "search",
        "kind": "deeplink",
        "venue_name": name,
        "booking_url": f"https://www.google.com/search?{qs}",
    }


def check_availability(
    name: str,
    lat: float,
    lng: float,
    date: str,
    time_hhmm: str,
    party_size: int,
) -> dict:
    """Resy = real availability. If Resy doesn't have the venue, fall back to
    an OpenTable deep link (no availability check — user sees it on tap-through).
    """
    resy = resy_find(name, lat, lng, date, time_hhmm, party_size)
    if resy.get("found"):
        return resy
    # Resy didn't have the spot OR had it but no slots. Hand off a Google
    # search URL — reliably lands on OT/restaurant widget in one tap.
    fallback = reservation_search_url(name, date, time_hhmm, party_size)
    return {
        **fallback,
        "resy_checked": True,
        "resy_had_venue": bool(resy.get("venue_name")),
        "resy_error": resy.get("error"),
    }


# ---------- tool schemas (Anthropic format) ----------

TOOL_SCHEMAS = [
    {
        "name": "read_prefs",
        "description": "Read the user's preferences file. Call this once per session before searching.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "append_pref",
        "description": "Append a single preference line to prefs.md. Use when the user explicitly asks you to remember something.",
        "input_schema": {
            "type": "object",
            "properties": {
                "line": {"type": "string", "description": "The preference text to remember."}
            },
            "required": ["line"],
        },
    },
    {
        "name": "log_history",
        "description": "Append an entry to history.md. Call when the user picks a candidate so future searches can learn from it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entry": {"type": "string", "description": "Short description of the search and pick."}
            },
            "required": ["entry"],
        },
    },
    {
        "name": "log_booking",
        "description": (
            "Record a confirmed reservation. Call this whenever the user signals they "
            "actually booked / are going to a place — phrases like 'booked the dutch', "
            "'going with raoul's', 'let's do balthazar at 8', 'reserved penny roma for "
            "tomorrow'. Capture date/time/party from context if available. Critical for "
            "fuzzy-matching later when the user shares a rating without naming the spot. "
            "When date is YYYY-MM-DD and time is concrete (e.g. '8:00pm'), the tool "
            "returns a `calendar_url:` line — a Google Calendar 'add event' link. Pass "
            "that URL through to the user verbatim in your reply so they can tap to save. "
            "Prefer YYYY-MM-DD over 'Friday' here — resolve natural dates to ISO first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "venue": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD or natural language ('Friday')"},
                "time": {"type": "string", "description": "Time of the reservation, e.g. '8:00pm'"},
                "party_size": {"type": "integer"},
                "notes": {"type": "string", "description": "Anything else worth remembering (occasion, who's going)"},
            },
            "required": ["venue"],
        },
    },
    {
        "name": "read_bookings",
        "description": (
            "Return the recent confirmed-bookings log. Use this when the user shares "
            "feedback or a rating without naming the venue ('last night was incredible, "
            "give it a 9.1') — match against the most recent booking, then call log_beli "
            "with that name."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_beli",
        "description": "Read the user's Beli list (their personal restaurant ratings + want-to-try). Call once per session alongside read_prefs.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "log_beli",
        "description": (
            "Append a Beli entry. Call this whenever the user mentions a place "
            "they tried or a Beli score in passing — e.g. 'btw Raoul's was great, "
            "Beli 8.7' or 'add Lilia to my list'. Score 0-10 if mentioned; omit "
            "score for want-to-try. Be liberal — capture every mention so the list "
            "stays in sync. Do not announce the save unless asked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Restaurant name."},
                "score": {"type": "number", "description": "Beli score 0-10. Omit for want-to-try."},
                "notes": {"type": "string", "description": "Optional: cuisine, neighborhood, vibe."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_restaurants",
        "description": (
            "Search for restaurants by free-text query (e.g. 'lively wine bar', 'omakase'). "
            "Pass `neighborhood` (e.g. 'SoHo', 'Williamsburg', 'Cobble Hill') for NYC-area "
            "biasing — preferred over free-text neighborhood mentions. Returns up to 8 "
            "candidates with name, rating, price, and lat/lng."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free text — vibe + cuisine. Skip the neighborhood here if you're passing it via `neighborhood`."},
                "neighborhood": {
                    "type": "string",
                    "description": "NYC neighborhood name (SoHo, Williamsburg, Cobble Hill, West Village, etc.). Resolved to a centroid internally — preferred over `location_bias` for NYC.",
                },
                "location_bias": {
                    "type": "string",
                    "description": "Optional 'lat,lng' string (e.g. '40.7233,-74.0030'). Use only when you have explicit coords or for non-NYC areas. Ignored if `neighborhood` matches.",
                },
                "max_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "travel_time",
        "description": (
            "Estimate travel time between two points via Google Routes. Use this "
            "whenever the user asks how long to get somewhere, whether a venue is "
            "convenient, or to compare candidates by proximity. Never guess transit/"
            "walk/drive times — call this tool. Default origin: the user's home "
            "address from prefs (unless they name another start). Destination: pass "
            "the venue's `lat,lng` from search_restaurants. Default mode: 'transit' "
            "in NYC. Pass `date`+`time_hhmm` (the reservation time) for traffic-aware "
            "driving estimates. Fan out modes in parallel (one call per mode) when "
            "comparing options."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "Address string or 'lat,lng'. Use the home/work address from prefs unless the user gives another starting point.",
                },
                "destination": {
                    "type": "string",
                    "description": "Address string or 'lat,lng' (use the venue's lat,lng from search_restaurants).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["transit", "walking", "driving", "bicycling"],
                    "description": "Travel mode. Default 'transit' for NYC.",
                },
                "date": {"type": "string", "description": "YYYY-MM-DD departure date. Optional — defaults to now."},
                "time_hhmm": {"type": "string", "description": "Departure time, 4 digits e.g. '1930'. Optional — pass the reservation time for traffic-aware driving."},
            },
            "required": ["origin", "destination"],
        },
    },
    {
        "name": "check_availability",
        "description": (
            "Check Resy for real availability at a specific restaurant near a lat/lng. "
            "Returns slots within ~90 min of target time when found=true. "
            "If Resy doesn't have the venue OR has no slots in the window, returns a "
            "Google search URL as fallback (platform='search', found=null) — user gets "
            "to OpenTable/Resy/widget in one tap. OpenTable's API is bot-blocked so we "
            "cannot verify their availability server-side."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "lat": {"type": "number"},
                "lng": {"type": "number"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time_hhmm": {"type": "string", "description": "Target time as 4 digits, e.g. '1930' for 7:30pm"},
                "party_size": {"type": "integer"},
            },
            "required": ["name", "lat", "lng", "date", "time_hhmm", "party_size"],
        },
    },
]


def dispatch(name: str, args: dict):
    """Route a tool call from the agent to the right Python function."""
    # Log every tool call so we can diff log vs. agent claims afterwards.
    args_summary = {k: (str(v)[:80] + "…" if isinstance(v, str) and len(v) > 80 else v)
                    for k, v in (args or {}).items()}
    log.info("tool=%s args=%s", name, args_summary)
    if name == "read_prefs":
        return read_prefs()
    if name == "append_pref":
        return append_pref(args["line"])
    if name == "log_history":
        return log_history(args["entry"])
    if name == "log_booking":
        return log_booking(
            venue=args["venue"],
            date=args.get("date"),
            time=args.get("time"),
            party_size=args.get("party_size"),
            notes=args.get("notes"),
        )
    if name == "read_bookings":
        return read_bookings()
    if name == "read_beli":
        return read_beli()
    if name == "log_beli":
        return log_beli(
            name=args["name"],
            score=args.get("score"),
            notes=args.get("notes"),
        )
    if name == "search_restaurants":
        return search_restaurants(
            query=args["query"],
            location_bias=args.get("location_bias"),
            neighborhood=args.get("neighborhood"),
            max_results=args.get("max_results", 8),
        )
    if name == "travel_time":
        return travel_time(
            origin=args["origin"],
            destination=args["destination"],
            mode=args.get("mode", "transit"),
            date=args.get("date"),
            time_hhmm=args.get("time_hhmm"),
        )
    if name == "check_availability":
        return check_availability(
            name=args["name"],
            lat=args["lat"],
            lng=args["lng"],
            date=args["date"],
            time_hhmm=args["time_hhmm"],
            party_size=args["party_size"],
        )
    return {"error": f"unknown tool: {name}"}
