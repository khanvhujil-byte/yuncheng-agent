"""探针 6：用真实后端行程的坐标，验证「竖线分隔 + 一天一图」这套用法真能出 PNG。

这是最终要用到前端的那套 URL 构造方式的端到端验证，
不是只验证一个手工凑出来的示例点。
"""

import json
import pathlib
import re
import urllib.parse

import httpx

ENV = pathlib.Path(__file__).with_name(".env")
KEY = re.search(
    r"^AMAP_API_KEY\s*=\s*(\S+)\s*$", ENV.read_text(encoding="utf-8"), re.MULTILINE
).group(1)

OUT = pathlib.Path(__file__).with_name("_probe_out")
OUT.mkdir(exist_ok=True)

print("=== 1. 调后端拿一份真实行程")
req = {
    "city": "杭州",
    "start_date": "2025-10-01",
    "travel_days": 2,
    "transportation": "公共交通",
    "accommodation": "经济型酒店",
    "preferences": ["自然风光", "历史人文"],
}
r = httpx.post("http://127.0.0.1:8000/api/trip/plan", json=req, timeout=180.0)
print("  status:", r.status_code)
body = r.json()
print("  success:", body.get("success"), "message:", body.get("message"))
plan = body.get("data")
if not plan:
    raise SystemExit("no plan data, abort")

hotel = plan.get("hotel") or {}
print("  hotel :", hotel.get("name"), hotel.get("location"))

STATIC = "https://restapi.amap.com/v3/staticmap"
SIZE = "700*420"


def markers_of(points):
    """points = [(label, lng, lat)]；用 | 连接 —— 实测分号会让高德返回 20003。"""
    return "|".join(f"mid,,{lab}:{lng},{lat}" for lab, lng, lat in points)


def build_url(points, zoom=12):
    lngs = [p[1] for p in points]
    lats = [p[2] for p in points]
    center = f"{(min(lngs) + max(lngs)) / 2:.6f},{(min(lats) + max(lats)) / 2:.6f}"
    q = urllib.parse.urlencode(
        {"location": center, "zoom": str(zoom), "size": SIZE,
         "markers": markers_of(points), "key": KEY},
        safe="|:,",
    )
    return f"{STATIC}?{q}"


print("\n=== 2. 用真实坐标逐天出图（竖线分隔）")
for day in plan.get("days", []):
    pts = []
    for i, spot in enumerate(day.get("attractions", [])):
        loc = spot.get("location") or {}
        if loc.get("longitude") is None:
            continue
        pts.append((chr(ord("A") + i), loc["longitude"], loc["latitude"]))
    print(f"  第{day['day_index'] + 1}天 {day['date']}: {len(pts)} 个点 "
          f"{[p[0] + '=' + (day['attractions'][i].get('name') or '') for i, p in enumerate(pts)]}")
    if not pts:
        continue
    url = build_url(pts)
    rr = httpx.get(url, timeout=25.0)
    b = rr.content
    ok = b[:8] == b"\x89PNG\r\n\x1a\n"
    # 只打码 key，其余 URL 原样打印，便于人工核对
    print("    url  :", url.replace(KEY, "<KEY>"))
    print("    ctype:", rr.headers.get("content-type"), "bytes:", len(b), "is_png:", ok)
    if not ok:
        print("    body :", b[:200].decode("utf-8", "replace"))
    else:
        fp = OUT / f"day{day['day_index'] + 1}.png"
        fp.write_bytes(b)
        print("    saved:", fp)

print("\n=== 3. 带酒店的第 1 天（酒店参与同一天的点位）")
if plan.get("days"):
    d0 = plan["days"][0]
    pts = []
    hl = hotel.get("location") or {}
    if hl.get("longitude") is not None:
        pts.append(("S", hl["longitude"], hl["latitude"]))
    for i, spot in enumerate(d0.get("attractions", [])):
        loc = spot.get("location") or {}
        if loc.get("longitude") is None:
            continue
        pts.append((chr(ord("A") + i), loc["longitude"], loc["latitude"]))
    print("  点数:", len(pts))
    if pts:
        url = build_url(pts)
        rr = httpx.get(url, timeout=25.0)
        b = rr.content
        ok = b[:8] == b"\x89PNG\r\n\x1a\n"
        print("  is_png:", ok, "bytes:", len(b), "ctype:", rr.headers.get("content-type"))
        if ok:
            (OUT / "day1_with_hotel.png").write_bytes(b)
        else:
            print("  body:", b[:200].decode("utf-8", "replace"))
