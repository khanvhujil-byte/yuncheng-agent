"""Bounded public web discovery and evidence extraction for admission policies.

Search snippets never become prices. Fetch the source page, verify venue/city,
reject ambiguous variants, retain provenance and cache misses as well as hits.
"""
import base64
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import ipaddress
import json
import re
import socket
from time import monotonic
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from ..config import get_settings
from . import catalog
from .request_gate import RequestGate, parse_retry_after
from .tool_result import ToolResult
from .ticket_policy import aliases, extract

_gate = RequestGate()
_SCHEMA = '''CREATE TABLE IF NOT EXISTS ticket_web_cache (
 identity TEXT PRIMARY KEY, payload_json TEXT NOT NULL, expires_at TEXT NOT NULL)'''
# Government tourism pages plus maintained official scenic-area domains.
OFFICIAL_HOSTS = ('dpm.org.cn', 'panda.org.cn', 'szwwco.com', 'splendidchina.com', 'octhappyvalley.com')


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.hidden = [], [], 0
        self.raw = ''
        self.search_links = []
        self._heading = 0
        self.titles = []
        self._title = None
        self.search_labels = {}
        self._link = None
        self._link_text = []

    def handle_starttag(self, tag, attrs):
        if tag in ('title', 'h1'):
            self._title = []
        if tag in ('h2', 'h3'):
            self._heading += 1
        if tag in ('script', 'style', 'noscript', 'nav', 'footer'):
            self.hidden += 1
        if tag == 'a':
            self.links.append(dict(attrs).get('href', ''))
            if self._heading or dict(attrs).get('name') == 'dttl':
                self.search_links.append(dict(attrs).get('href', ''))
                self._link = dict(attrs).get('href', '')
                self._link_text = []
        if tag in ('p', 'br', 'li', 'tr', 'div', 'h1', 'h2', 'h3'):
            self.parts.append('\n')
        if tag in ('td', 'th'):
            self.parts.append(' | ')

    def handle_endtag(self, tag):
        if tag == 'a' and self._link is not None:
            self.search_labels[self._link] = ''.join(self._link_text)
            self._link = None
        if tag in ('title', 'h1') and self._title is not None:
            self.titles.append(''.join(self._title))
            self._title = None
        if tag in ('p', 'li', 'tr', 'div', 'h1', 'h2', 'h3', 'title'):
            self.parts.append('\n')
        if tag in ('h2', 'h3'):
            self._heading = max(0, self._heading - 1)
        if tag in ('script', 'style', 'noscript', 'nav', 'footer'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self._title is not None:
                self._title.append(data)
            if self._link is not None:
                self._link_text.append(data)


def trusted_url(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
            and (host.endswith('.gov.cn') or any(host == h or host.endswith('.' + h)
                                               for h in (*OFFICIAL_HOSTS, *get_settings().ticket_official_hosts))))


def _public(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('UNSAFE_SOURCE_URL')
    answers = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not answers or any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
        raise ValueError('UNSAFE_SOURCE_ADDRESS')


def _get(url, deadline, *, search=False, requests=None):
    for _ in range(3):
        if monotonic() >= deadline:
            raise TimeoutError('WEB_QUERY_BUDGET')
        if not search and not trusted_url(url):
            raise ValueError('SOURCE_NOT_VERIFIED')
        _public(url)
        _gate.acquire(interval=.2, budget=max(.001, deadline - monotonic()))
        trace = {'url': url}
        started = monotonic()
        if requests is not None:
            requests.append(trace)
        try:
            response = httpx.get(url, timeout=min(3, max(.001, deadline - monotonic())), follow_redirects=False,
                                 headers={'User-Agent': 'TripPlanner/1.0 (public ticket policy lookup)'})
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            trace['outcome'] = ToolResult.failure(type(exc).__name__, 'WEB_TRANSPORT_ERROR', retryable=True).to_dict()
            raise
        else:
            trace['status_code'] = response.status_code
            trace['outcome'] = (ToolResult.failure(f'HTTP_{response.status_code}',
                'UPSTREAM_RATE_LIMITED' if response.status_code == 429 else f'HTTP_{response.status_code}',
                retryable=response.status_code == 429 or response.status_code >= 500)
                if response.is_error else ToolResult.success('HTTP response received')).to_dict()
        finally:
            trace['duration_ms'] = int((monotonic() - started) * 1000)
        if response.status_code == 429:
            _gate.defer(min(60, parse_retry_after(response.headers.get('Retry-After')) or 2))
            raise ValueError('HTTP_429')
        if response.status_code in (301, 302, 303, 307, 308):
            target = urljoin(url, response.headers.get('location', ''))
            if search and urlsplit(target).hostname not in ('www.bing.com', 'cn.bing.com', 'www.sogou.com') and not trusted_url(target):
                raise ValueError('SEARCH_REDIRECT_UNSUPPORTED')
            url = target
            continue
        if response.is_error:
            raise ValueError(f'HTTP_{response.status_code}')
        if len(response.content) > 2_000_000:
            raise ValueError('SOURCE_TOO_LARGE')
        page = Page()
        page.feed(response.text)
        page.raw = response.text
        return url, page
    raise ValueError('REDIRECT_LIMIT')


def _relevant_label(label, venue):
    label = re.sub(r'\s+', '', label)
    return venue in label or bool(re.fullmatch(r'(?:门票(?:价格|信息)?|票价(?:信息)?|参观须知|购票指南|游览须知)', label))


def result_links(page, venue=None):
    links = []
    for raw in page.search_links:
        if venue and not _relevant_label(page.search_labels.get(raw, ''), venue):
            continue
        try:
            parsed = urlsplit(raw)
            if parsed.hostname in ('www.bing.com', 'cn.bing.com') and parsed.path.startswith('/ck/'):
                encoded = parse_qs(parsed.query).get('u', [''])[0]
                if encoded.startswith('a1'):
                    raw = base64.urlsafe_b64decode(encoded[2:] + '=' * (-len(encoded[2:]) % 4)).decode('utf-8')
            if trusted_url(raw) and urlsplit(raw).path not in ('', '/') and raw not in links:
                links.append(raw)
        except (ValueError, UnicodeError):
            continue
    return links[:3]


def _discover(query, deadline, requests):
    from urllib.parse import urlencode
    links = []
    venue = query.split()[0]
    try:
        _, search = _get('https://www.bing.com/search?' + urlencode({'q': query}), deadline,
                         search=True, requests=requests)
        links = result_links(search, venue)
        yield from links
    except (ValueError, httpx.HTTPError, TimeoutError, OSError):
        pass
    if monotonic() >= deadline:
        return
    _, search = _get('https://www.sogou.com/web?' + urlencode({'query': query + ' site:gov.cn'}), deadline,
                     search=True, requests=requests)
    # Some result pages return direct government links instead of wrappers.
    for link in result_links(search, venue):
        if link not in links:
            links.append(link)
            yield link
    # Sogou wraps results in same-origin links. Read a bounded redirect page;
    # extract a literal URL, never execute its JavaScript or follow arbitrary URLs.
    redirects = list(dict.fromkeys(raw for raw in search.search_links if raw.startswith('/link?url=')
                                  and _relevant_label(search.search_labels.get(raw, ''), venue)))[:3]
    for raw in redirects:
        if monotonic() >= deadline:
            break
        try:
            resolved, page = _get('https://www.sogou.com' + raw, deadline, search=True, requests=requests)
            match = re.search(r'window\.location\.replace\(["\'](https?://[^"\']+)["\']\)', page.raw)
            target = match.group(1) if match else resolved
            # Only upgrade the transport; the host still must pass the same trust check.
            if target.startswith('http://'):
                target = 'https://' + target[7:]
            if trusted_url(target) and target not in links:
                links.append(target)
                yield target
        except (ValueError, httpx.HTTPError, TimeoutError, OSError):
            # One blocked search redirect must not discard the remaining results.
            continue


def extract_policy(page, name, city, target):
    return extract(page, name, city, target)


def _identity(city, spot, target):
    return json.dumps([2, city.removesuffix('市'), spot['poi_id'], target], ensure_ascii=False)


def cached_lookup(city, spot, target):
    identity = _identity(city, spot, target)
    with catalog._connection() as db:
        db.execute(_SCHEMA)
        row = db.execute('SELECT * FROM ticket_web_cache WHERE identity=?', (identity,)).fetchone()
    if row and datetime.fromisoformat(row['expires_at']) > datetime.now(timezone.utc):
        return {**json.loads(row['payload_json']), 'cached': True, 'http_calls': []}
    return None


def lookup(city, spot, target, deadline):
    identity = _identity(city, spot, target)
    now = datetime.now(timezone.utc)
    cached = cached_lookup(city, spot, target)
    if cached is not None:
        return cached
    name = min(aliases(spot['name'], city), key=len)
    query = f"{name} {city.removesuffix('市')} 门票"
    result = {'status': 'WEB_SEARCH_NO_VERIFIED_SOURCE', 'query': query}
    requests = []
    try:
        reasons = []
        for link in _discover(query, deadline, requests):
            try:
                url, page = _get(link, deadline, requests=requests)
                policy = extract_policy(page, spot['name'], city, target)
                result = {**result, **policy, 'status': 'ok', 'url': url,
                          'observed_at': now.isoformat(), 'expires_at': (now + timedelta(hours=24)).isoformat()}
                break
            except (ValueError, httpx.HTTPError, TimeoutError, OSError) as exc:
                reasons.append(str(exc) if isinstance(exc, (ValueError, TimeoutError)) else type(exc).__name__)
        if result['status'] != 'ok' and reasons:
            result['status'] = 'WEB_EVIDENCE_UNRESOLVED'
            result['reasons'] = reasons
    except (ValueError, httpx.HTTPError, TimeoutError, OSError) as exc:
        result.update(status='WEB_SEARCH_FAILED', reasons=[str(exc) if isinstance(exc, (ValueError, TimeoutError)) else type(exc).__name__])
    result['http_calls'] = requests
    expiry = result.get('expires_at') if result['status'] == 'ok' else (now + timedelta(minutes=15)).isoformat()
    with catalog._connection() as db:
        db.execute('INSERT OR REPLACE INTO ticket_web_cache VALUES(?,?,?)', (identity, json.dumps(result, ensure_ascii=False), expiry))
    return result
