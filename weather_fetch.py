import json, urllib.request, sys

CITIES = [
    ("辽宁/沈阳", "41.81", "123.43"),
    ("吉林/长春", "43.88", "125.32"),
    ("黑龙江/哈尔滨", "45.80", "126.53"),
    ("山东/济南", "36.65", "117.12"),
    ("河南/郑州", "34.75", "113.62"),
]

WCODE = {}  # use weatherDesc directly

out = {}
for name, lat, lon in CITIES:
    url = f"https://wttr.in/{lat},{lon}?format=j1&lang=zh"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
    try:
        raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
        d = json.loads(raw)
    except Exception as e:
        out[name] = {"error": str(e)}
        continue

    cur = d["current_condition"][0]
    nearest = d.get("nearest_area", [{}])[0]
    def area(field):
        v = nearest.get(field)
        return v[0]["value"] if v else ""
    days = []
    for day in d.get("weather", []):
        hourly = day.get("hourly", [])
        def at(h):
            for x in hourly:
                if x["time"] == h:
                    return x
            return None
        d9 = at("900") or (hourly[0] if hourly else {})
        d15 = at("1500") or d9
        days.append({
            "date": day["date"],
            "max": day["maxtempC"],
            "min": day["mintempC"],
            "avg": day.get("avgtempC"),
            "desc_mid": " / ".join(x["weatherDesc"][0]["value"].strip() for x in [d9, d15] if x),
            "chanceofrain": day.get("hourly", [{}])[4].get("chanceofrain", "") if len(day.get("hourly", [])) > 4 else "",
            "sunrise": day["astronomy"][0]["sunrise"],
            "sunset": day["astronomy"][0]["sunset"],
        })
    out[name] = {
        "area": " ".join(x for x in [area("areacode"), area("areaName"), area("region"), area("country")] if x),
        "obs": cur["observation_time"],
        "temp_C": cur["temp_C"],
        "feels_C": cur["FeelsLikeC"],
        "desc": cur["weatherDesc"][0]["value"].strip(),
        "humidity": cur["humidity"],
        "pressure": cur["pressure"],
        "visibility_km": cur["visibility"],
        "wind_kmph": cur["windspeedKmph"],
        "wind_dir": cur["winddir16Point"],
        "uv": cur["uvIndex"],
        "days": days,
    }

print(json.dumps(out, ensure_ascii=False, indent=1))
