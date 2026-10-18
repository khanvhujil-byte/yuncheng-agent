"""Small public ranking crawler. No cookies, login, hidden endpoints or browser bypass.

Only AMap's public HTML ranking pages are accepted. Robots must be readable and
allow our declared agent. The downloaded ItemList positions keep their original
list title and are never advertised as a live, city-wide quality score.
"""
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from . import catalog
from .request_gate import RequestGate, parse_retry_after

USER_AGENT = "TripPlannerCatalogBot/1.0"
MAX_BYTES = 1_000_000
crawl_gate = RequestGate()


def validate_url(url, *, robots=False):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != "www.amap.com" or
            parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("仅允许 https://www.amap.com 的公开榜单页面")
    if robots:
        allowed = parsed.path == "/robots.txt"
    else:
        allowed = bool(re.fullmatch(r"/ranking/[a-z0-9-]+(?:/[a-z0-9-]+){0,2}/?", parsed.path))
    if not allowed:
        raise ValueError("仅允许 /ranking/<city>/... 路径")
    return url


def _public_host():
    addresses = socket.getaddrinfo("www.amap.com", 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("数据源域名未解析到公开地址")


def fetch_public(url, *, interval=5):
    validate_url(url, robots=url.endswith("/robots.txt"))
    _public_host()
    for attempt in range(2):
        crawl_gate.acquire(interval=max(5, interval), budget=30)
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False,
                          headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,application/json"}) as client:
            with client.stream("GET", url) as response:
                if response.status_code in (429, 503) and attempt == 0:
                    wait = max(5, parse_retry_after(response.headers.get("Retry-After")) or 0)
                    if wait > 25:
                        raise ValueError("数据源限流，稍后重试")
                    crawl_gate.defer(wait)
                    continue
                response.raise_for_status()
                if response.is_redirect:
                    raise ValueError("不跟随数据源重定向")
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("数据源内容超过 1MB 上限")
                return raw.decode("utf-8", errors="strict")
    raise ValueError("数据源暂不可用")


class RankingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.json_scripts = []
        self.cards = []
        self._script = None
        self._card = None
        self._capture = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self._script = []
        if tag == "a" and "poi-card" in attrs.get("class", "").split():
            match = re.fullmatch(r"/place/([A-Za-z0-9]+)", attrs.get("href", ""))
            if match:
                self._card = {"poi_id": match.group(1), "name": ""}
        if self._card is not None and "poi-name" in attrs.get("class", "").split():
            self._capture = True

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        if self._capture and self._card is not None:
            self._card["name"] += data

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.json_scripts.append("".join(self._script))
            self._script = None
        if tag == "div":
            self._capture = False
        if tag == "a" and self._card is not None:
            self._card["name"] = self._card["name"].strip()
            self.cards.append(self._card)
            self._card = None


def parse_ranking(html, *, city, limit=20):
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("数据源内容过大")
    parser = RankingParser()
    parser.feed(html)
    names = {entry["name"]: entry["poi_id"] for entry in parser.cards if entry["name"]}
    for raw in parser.json_scripts:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "ItemList":
            continue
        title = data.get("name", "")
        if not isinstance(title, str) or city.removesuffix("市") not in title or not any(word in title for word in ("美食", "餐饮")):
            continue
        records, seen = [], set()
        for item in data.get("itemListElement", [])[:100]:
            if not isinstance(item, dict) or not isinstance(item.get("item"), dict):
                continue
            name, rank = item["item"].get("name"), item.get("position")
            identifier = names.get(name)
            if not identifier or identifier in seen or isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
                continue
            seen.add(identifier)
            records.append({"name": name, "poi_id": identifier, "rank": rank})
            if len(records) >= min(max(limit, 1), 20):
                break
        if records:
            return {"city": city, "ranking_name": title, "records": records}
    raise ValueError("页面没有与目标城市匹配的美食 ItemList 和可核验 POI ID；不写入空白或猜测数据")


def crawl_ranking(url, *, city, limit=20, fetch=fetch_public):
    validate_url(url)
    robots = RobotFileParser()
    robots.parse(fetch("https://www.amap.com/robots.txt", interval=5).splitlines())
    if not robots.can_fetch(USER_AGENT, url):
        raise ValueError("数据源 robots.txt 不允许采集此页面")
    delay = max(5, robots.crawl_delay(USER_AGENT) or 5)
    result = parse_ranking(fetch(url, interval=delay), city=city, limit=limit)
    result["source_url"] = url
    result["captured_at"] = datetime.now(timezone.utc).isoformat()
    result["stored"] = catalog.save_rankings(city, result["records"], source_url=url,
                                               ranking_name=result["ranking_name"], observed_at=result["captured_at"])
    return result


def import_ranking(document):
    """Explicit administrator export import; same bounded records and provenance."""
    url = validate_url(document["source_url"])
    city, title = document["city"], document["ranking_name"]
    captured = datetime.fromisoformat(document["captured_at"])
    if captured.tzinfo is None or captured > datetime.now(timezone.utc):
        raise ValueError("采集时间缺少时区或晚于当前时间")
    if city.removesuffix("市") not in title or not any(word in title for word in ("美食", "餐饮")):
        raise ValueError("导入城市和美食榜单标题不匹配")
    records = document["records"]
    if not isinstance(records, list) or len(records) > 100:
        raise ValueError("导入记录超过上限")
    for item in records:
        if (not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9]+", item.get("poi_id", "")) or
                not isinstance(item.get("name"), str) or not item["name"].strip() or
                isinstance(item.get("rank"), bool) or not isinstance(item.get("rank"), int) or item["rank"] < 1):
            raise ValueError("榜单记录格式不合法")
    return catalog.save_rankings(city, records, source_url=url, ranking_name=title, observed_at=captured.isoformat())
