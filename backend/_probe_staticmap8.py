"""探针 8：marker 的 label 能否用中文（为了在图上直接标景点名 / 序号）。"""

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

P = ["120.15507,30.274085", "120.10222,30.24048", "120.06836,30.27144"]


def render(name: str, labels):
    markers = "|".join(f"mid,,{lab}:{pt}" for lab, pt in zip(labels, P))
    q = urllib.parse.urlencode(
        {"location": "120.111,30.257", "zoom": "12", "size": "700*420",
         "markers": markers, "key": KEY},
        safe="|:,",
    )
    url = f"{STATIC}?{q}"
    r = httpx.get(url, timeout=25.0)
    ok = r.content[:8] == b"\x89PNG\r\n\x1a\n"
    print(f"{name:16s} png={ok} bytes={len(r.content)}")
    print("   markers:", markers)
    if ok:
        (OUT / f"label_{name}.png").write_bytes(r.content)
    else:
        print("   body:", r.content[:200].decode("utf-8", "replace"))


render("chinese", ["一", "二", "三"])
render("latin", ["A", "B", "C"])
render("digit", ["1", "2", "3"])
render("site_name", ["西湖", "灵隐寺", "西溪"])
