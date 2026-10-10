"""探针 2：定位多 marker 失败的真实原因，而不是靠猜。"""

import pathlib
import re

import httpx

ENV = pathlib.Path(__file__).with_name(".env")
KEY = re.search(
    r"^AMAP_API_KEY\s*=\s*(\S+)\s*$", ENV.read_text(encoding="utf-8"), re.MULTILINE
).group(1)

BASE = "https://restapi.amap.com/v3/staticmap"

CASES = [
    ("A 两个 marker 分号", f"{BASE}?location=120.155,30.274&zoom=11&size=700*420&markers=mid,,A:120.155,30.274;mid,,B:120.140,30.243&key={KEY}"),
    ("B 两个 marker 逗号", f"{BASE}?location=120.155,30.274&zoom=11&size=700*420&markers=mid,,A:120.155,30.274,B:120.140,30.243&key={KEY}"),
    ("C size 数字乘号", f"{BASE}?location=120.155,30.274&zoom=11&size=700,420&markers=mid,,A:120.155,30.274;mid,,B:120.140,30.243&key={KEY}"),
    ("D 无 label 两点", f"{BASE}?location=120.155,30.274&zoom=11&size=700*420&markers=mid,,:120.155,30.274;mid,,:120.140,30.243&key={KEY}"),
    ("E 同一点两次", f"{BASE}?location=120.155,30.274&zoom=11&size=700*420&markers=mid,,A:120.155,30.274;mid,,B:120.155,30.274&key={KEY}"),
    ("F 两个 location 半径+markers", f"{BASE}?location=120.155,30.274&zoom=11&size=700*420&markers=mid,,A:120.155,30.274;mid,,B:120.140,30.243&key={KEY}&scale=1"),
]

for label, url in CASES:
    try:
        r = httpx.get(url, timeout=20.0)
        body = r.content
        ok = body[:8] == b"\x89PNG\r\n\x1a\n"
        extra = "" if ok else " " + body[:200].decode("utf-8", "replace")
        print(f"{label:28s} status={r.status_code} png={ok} bytes={len(body)}{extra}")
    except Exception as exc:  # noqa: BLE001
        print(f"{label:28s} EXC {type(exc).__name__}: {exc}")
