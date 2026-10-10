"""一次性探针脚本：验证高德静态地图 REST API 是否可用（用完即可删）。

为什么单独写脚本而不是在文档里"假设"：
静态地图 API 的返回体不看 content-type 是分不清 PNG 和 JSON 错误体的，
必须真的发一次请求、打字节头和 content-type，才能下结论。
"""

import pathlib
import re

import httpx

ENV = pathlib.Path(__file__).with_name(".env")


def load_key() -> str:
    text = ENV.read_text(encoding="utf-8")
    m = re.search(r"^AMAP_API_KEY\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    if not m:
        raise SystemExit("AMAP_API_KEY not found in .env")
    return m.group(1)


def probe(url: str, label: str) -> None:
    r = httpx.get(url, timeout=20.0)
    body = r.content
    print(f"--- {label}")
    print("  url      :", url.replace(KEY, "<KEY>"))
    print("  status   :", r.status_code)
    print("  ctype    :", r.headers.get("content-type"))
    print("  bytes    :", len(body))
    print("  magic    :", body[:8].hex())
    print("  is_png   :", body[:8] == b"\x89PNG\r\n\x1a\n")
    if body[:1] != b"\x89":
        print("  text     :", body[:400].decode("utf-8", "replace"))


KEY = load_key()
print("key len  :", len(KEY))

# 1) 单点 + 单 marker（size 用题目给的 700*420）
probe(
    "https://restapi.amap.com/v3/staticmap"
    f"?location=120.15507,30.274085&zoom=12&size=700*420"
    f"&markers=mid,,A:120.15507,30.274085&key={KEY}",
    "单 marker 700*420",
)

# 2) 多点 + 多点位 marker（真实用法：一天的 3 个景点）
probe(
    "https://restapi.amap.com/v3/staticmap"
    f"?location=120.15507,30.274085&zoom=11&size=700*420"
    f"&markers=mid,,A:120.15507,30.274085;mid,,B:120.1406,30.2431;mid,,C:120.1008,30.2596"
    f"&key={KEY}",
    "多 marker 700*420",
)
