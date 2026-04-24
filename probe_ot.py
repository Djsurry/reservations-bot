"""Probe OpenTable API endpoints (skip the HTML hop — bot-protected)."""
import json, httpx
from datetime import date, timedelta

DAY = (date.today() + timedelta(days=1)).isoformat()
PARTY = 2
ot_headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.opentable.com/",
    "Origin": "https://www.opentable.com",
    "Content-Type": "application/json",
}

probes = [
    # Various dapi guesses
    ("dapi/search GET", "GET", "https://www.opentable.com/dapi/search",
     {"term": "lazy bear", "covers": PARTY, "dateTime": f"{DAY}T19:00:00"}, None),
    ("dapi/booking/availability GET", "GET",
     "https://www.opentable.com/dapi/booking/availability",
     {"rid": 11819, "covers": PARTY, "dateTime": f"{DAY}T19:00:00"}, None),
    ("dapi/fe/gql/format POST minimal", "POST",
     "https://www.opentable.com/dapi/fe/gql/format",
     None, {"operationName": "x", "variables": {}, "query": "{__typename}"}),
    # The one their app actually uses for the search results page
    ("dapi/booking/search-results POST", "POST",
     "https://www.opentable.com/dapi/booking/search-results",
     None, {"covers": PARTY, "dateTime": f"{DAY}T19:00:00",
            "term": "lazy bear", "metroId": 4}),
    # Affiliate API (well-documented, requires API key but often returns shape)
    ("opentable.com/affiliate/search", "GET",
     "https://opentable.com/affiliate/search", {"term": "lazy bear"}, None),
    # Mobile app endpoint
    ("mobile-api restaurants", "GET",
     "https://mobile-api.opentable.com/api/v2/restaurants",
     {"term": "lazy bear", "lat": 37.7599, "lng": -122.4148}, None),
]

for label, method, url, params, body in probes:
    try:
        r = httpx.request(method, url, params=params, json=body,
                          headers=ot_headers, timeout=8.0,
                          follow_redirects=False)
        ct = r.headers.get("content-type", "")
        body_preview = r.text[:250].replace("\n", " ")
        print(f"[{label}] {r.status_code} ct={ct[:40]}")
        print(f"  body: {body_preview!r}")
    except httpx.HTTPError as e:
        print(f"[{label}] ERROR {type(e).__name__}: {str(e)[:120]}")
    print()
