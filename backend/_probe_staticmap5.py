"""探针 5：最后一轮穷举 —— 分隔符 / 距离 / 点数，确认「多 marker 必失败」这个结论。"""

import pathlib
import re

import httpx

ENV = pathlib.Path(__file__).with_name(".env")
KEY = re.search(
    r"^AMAP_API_KEY\s*=\s*(\S+)\s*$", ENV.read_text(encoding="utf-8"), re.MULTILINE
).group(1)

BASE = "https://restapi.amap.com/v3/staticmap"
HZ = "120.15507,30.274085"   # 杭州西湖
BJ = "116.39745,39.90878"    # 北京天安门（极远，排除"点太近"假设）


def show(label: str, markers: str, location: str = HZ, zoom: str = "11") -> None:
    url = f"{BASE}?location={location}&zoom={zoom}&size=700*420&markers={markers}&key={KEY}"
    r = httpx.get(url, timeout=20.0)
    b = r.content
    ok = b[:8] == b"\x89PNG\r\n\x1a\n"
    note = "" if ok else " " + b[:120].decode("utf-8", "replace")
    print(f"{label:38s} png={ok!s:5s} bytes={len(b):6d}{note}")


show("1 点 分号前", f"mid,,A:{HZ}")
show("2 点 分号", f"mid,,A:{HZ};mid,,B:{BJ}", zoom="4")
show("2 点 竖线", f"mid,,A:{HZ}|mid,,B:{BJ}", zoom="4")
show("2 点 空格", f"mid,,A:{HZ} mid,,B:{BJ}", zoom="4")
show("2 点 半角句号分隔", f"mid,,A:{HZ}.mid,,B:{BJ}", zoom="4")
show("2 点 无 style 前缀", f"A:{HZ};B:{BJ}", zoom="4")
show("2 点 style 省略逗号", f"mid,A:{HZ};mid,B:{BJ}", zoom="4")
show("同一点两次（不同 label）", f"mid,,A:{HZ};mid,,B:{HZ}")
show("同一点两次（同 label）", f"mid,,A:{HZ};mid,,A:{HZ}")
show("3 点 竖线", f"mid,,A:{HZ}|mid,,B:{BJ}|mid,,C:121.4737,31.2304", zoom="4")
