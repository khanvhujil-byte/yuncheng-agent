"""探针 3：用 httpx params 交给库做 URL 编码，逐个变量二分，找到真正的失败变量。"""

import pathlib
import re

import httpx

ENV = pathlib.Path(__file__).with_name(".env")
KEY = re.search(
    r"^AMAP_API_KEY\s*=\s*(\S+)\s*$", ENV.read_text(encoding="utf-8"), re.MULTILINE
).group(1)

URL = "https://restapi.amap.com/v3/staticmap"


def call(label: str, **params) -> None:
    p = {"key": KEY, **params}
    r = httpx.get(URL, params=p, timeout=20.0)
    body = r.content
    ok = body[:8] == b"\x89PNG\r\n\x1a\n"
    extra = "" if ok else " " + body[:200].decode("utf-8", "replace")
    print(f"{label:34s} png={ok!s:5s} bytes={len(body):6d}{extra}")


# 基线：已确认能出图的那一条
call("基线 原样", location="120.15507,30.274085", zoom="12", size="700*420",
     markers="mid,,A:120.15507,30.274085")

# 缩小坐标精度
call("精度 3 位小数", location="120.155,30.274", zoom="12", size="700*420",
     markers="mid,,A:120.155,30.274")

# 换 zoom
call("zoom=11", location="120.15507,30.274085", zoom="11", size="700*420",
     markers="mid,,A:120.15507,30.274085")

# 两个 marker（用 params 编码）
call("2 marker 分号", location="120.15507,30.274085", zoom="11", size="700*420",
     markers="mid,,A:120.15507,30.274085;mid,,B:120.1406,30.2431")

# 两个 markers 参数（重复同名参数）
r = httpx.get(URL, params=[
    ("key", KEY), ("location", "120.15507,30.274085"), ("zoom", "11"), ("size", "700*420"),
    ("markers", "mid,,A:120.15507,30.274085"),
    ("markers", "mid,,B:120.1406,30.2431"),
], timeout=20.0)
print(f"{'2 marker 重复同名参数':34s} png={r.content[:8] == b'\\x89PNG\\r\\n\\x1a\\n'!s:5s} bytes={len(r.content):6d} {r.content[:200].decode('utf-8','replace') if r.content[:1] != b'\\x89' else ''}")

# 3 个 marker
call("3 marker 分号", location="120.15507,30.274085", zoom="11", size="700*420",
     markers="mid,,A:120.15507,30.274085;mid,,B:120.1406,30.2431;mid,,C:120.1008,30.2596")

# size 上限
call("size 1024*1024", location="120.15507,30.274085", zoom="12", size="1024*1024",
     markers="mid,,A:120.15507,30.274085")

# 不传 zoom / size（用默认值）
call("无 zoom 无 size", location="120.15507,30.274085",
     markers="mid,,A:120.15507,30.274085")
