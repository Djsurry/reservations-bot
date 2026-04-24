"""End-to-end test of the updated tools.check_availability flow."""
import json
from datetime import date, timedelta
from tools import check_availability

DAY = (date.today() + timedelta(days=1)).isoformat()
LAT, LNG = 37.7599, -122.4148  # SF Mission

cases = [
    # (name, expected_outcome)
    ("Penny Roma", "should find Resy slots"),
    ("Heirloom Cafe", "should find Resy slots"),
    ("Lazy Bear", "Resy has venue, likely no slots → OT deep link"),
    ("Zuni Cafe", "not on Resy (OT only) → OT deep link"),
]

for name, note in cases:
    print(f"\n=== {name}  ({note}) ===")
    result = check_availability(
        name=name, lat=LAT, lng=LNG,
        date=DAY, time_hhmm="1900", party_size=2,
    )
    print(json.dumps(result, indent=2)[:700])
