"""探针 7：用西湖 / 灵隐寺 / 西溪湿地等真实坐标，按前端将要用的 URL 构造方式出图并肉眼确认。"""

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

STATIC = "https://restapi.amap.com/v3/staticmap"
SIZE = "700*420"

# 一天 4 个真实景点（西湖 / 灵隐寺 / 西溪 / 宋城）
POINTS = [
    ("A", 120.155070, 30.274085, "西湖"),
    ("B", 120.102220, 30.240480, "灵隐寺"),
    ("C", 120.068360, 30.271440, "西溪湿地"),
    ("D", 120.085850, 30.192880, "宋城"),
]


def build(points, zoom=12):
    lngs = [p[1] for p in points]
    lats = [p[2] for p in points]
    center = f"{(min(lngs) + max(lngs)) / 2:.6f},{(min(lats) + max(lats)) / 2:.6f}"
    markers = "|".join(f"mid,,{p[0]}:{p[1]},{p[2]}" for p in points)
    q = urllib.parse.urlencode(
        {"location": center, "zoom": str(zoom), "size": SIZE, "markers": markers, "key": KEY},
        safe="|:,",
    )
    print("  markers 参数:", markers)
    return f"{STATIC}?{q}"


print("=== 4 个真实景点（一天一图）")
url = build(POINTS)
r = httpx.get(url, timeout=25.0)
ok = r.content[:8] == b"\x89PNG\r\n\x1a\n"
print("  ctype:", r.headers.get("content-type"), "bytes:", len(r.content), "is_png:", ok)
if not ok:
    print("  body:", r.content[:300].decode("utf-8", "replace"))
else:
    fp = OUT / "hangzhou_day.png"
    fp.write_bytes(r.content)
    print("  saved:", fp)

print("\n=== 加酒店做起点 S")
HOTEL = ("S", 120.15000, 30.25000)
url = build([HOTEL, *POINTS])
r = httpx.get(url, timeout=25.0)
ok = r.content[:8] == b"\x89PNG\r\n\x1a\n"
print("  ctype:", r.headers.get("content-type"), "bytes:", len(r.content), "is_png:", ok)
if ok:
    fp = OUT / "hangzhou_day_with_hotel.png"
    fp.write_bytes(r.content)
    print("  saved:", fp)

print("\n=== size 上限 1024*1024 是否接受")
q = urllib.parse.urlencode(
    {"location": "120.155070,30.274085", "zoom": "12", "size": "1024*1024",
     "markers": "mid,,A:120.155070,30.274085", "key": KEY}, safe="|:,")
r = httpx.get(f"{STATIC}?{q}", timeout=25.0)
print("  ctype:", r.headers.get("content-type"), "bytes:", len(r.content),
      "is_png:", r.content[:8] == b"\x89PNG\r\n\x1a\n")

print("\n=== 超过 1024 的 size（应当被拒）")
q = urllib.parse.urlencode(
    {"location": "120.155070,30.274085", "zoom": "12", "size": "2000*2000",
     "markers": "mid,,A:120.155070,30.274085", "key": KEY}, safe="|:,")
r = httpx.get(f"{STATIC}?{q}", timeout=25.0)
print("  ctype:", r.headers.get("content-type"), "bytes:", len(r.content),
      "body:", r.content[:200].decode("utf-8", "replace"))

print("\n=== 错误 key（验证失败时返回 JSON 而不是图片，前端据此降级）")
q = urllib.parse.urlencode(
    {"location": "120.155070,30.274085", "zoom": "12", "size": SIZE,
     "markers": "mid,,A:120.155070,30.274085", "key": "0" * 32}, safe="|:,")
r = httpx.get(f"{STATIC}?{q}", timeout=25.0)
print("  ctype:", r.headers.get("content-type"), "bytes:", len(r.content),
      "body:", r.content[:200].decode("utf-8", "replace"))
