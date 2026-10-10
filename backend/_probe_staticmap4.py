"""探针 4：把 2-marker 的失败复现到最小，并打印真实 URL 与字节头。"""

import pathlib
import re
import urllib.parse

import httpx

ENV = pathlib.Path(__file__).with_name(".env")
KEY = re.search(
    r"^AMAP_API_KEY\s*=\s*(\S+)\s*$", ENV.read_text(encoding="utf-8"), re.MULTILINE
).group(1)

BASE = "https://restapi.amap.com/v3/staticmap"
P1 = "120.15507,30.274085"
P2 = "120.1406,30.2431"


def show(label: str, url: str) -> None:
    r = httpx.get(url, timeout=20.0)
    b = r.content
    ok = b[:8] == b"\x89PNG\r\n\x1a\n"
    print(f"\n=== {label}")
    print("  raw url :", url.replace(KEY, "<KEY>"))
    print("  png     :", ok, "bytes=", len(b))
    if not ok:
        print("  body    :", b[:200].decode("utf-8", "replace"))


# 1) 原始字面 URL（不编码分号，逗号也不编码）
show(
    "字面 URL / 分号不编码",
    f"{BASE}?location={P1}&zoom=11&size=700*420&markers=mid,,A:{P1};mid,,B:{P2}&key={KEY}",
)

# 2) 看 httpx 用 params= 时到底编成了什么
req = httpx.Request(
    "GET", BASE,
    params={"key": KEY, "location": P1, "zoom": "11", "size": "700*420",
            "markers": f"mid,,A:{P1};mid,,B:{P2}"},
)
print("\n=== httpx params 实际编码出的 URL")
print(" ", str(req.url).replace(KEY, "<KEY>"))

# 3) 手动只编码分号
m = urllib.parse.quote(f"mid,,A:{P1};mid,,B:{P2}", safe=":,;")
show(
    "只保留 : , ; 不编码",
    f"{BASE}?location={P1}&zoom=11&size=700*420&markers={m}&key={KEY}",
)

# 4) mid 换成 small / large，看是不是样式名问题
for style in ("small", "mid", "large"):
    show(
        f"style={style} 两 marker",
        f"{BASE}?location={P1}&zoom=11&size=700*420&markers={style},,A:{P1};{style},,B:{P2}&key={KEY}",
    )

# 5) 一个 marker 用 mid,0x0000FF,A 形式（带颜色）
show(
    "带颜色 两 marker",
    f"{BASE}?location={P1}&zoom=11&size=700*420&markers=mid,0x0000FF,A:{P1};mid,0x0000FF,B:{P2}&key={KEY}",
)
