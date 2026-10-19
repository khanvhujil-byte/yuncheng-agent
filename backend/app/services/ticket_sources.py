"""Maintained source registry and bounded daily refresh of observed official pages."""
from datetime import datetime, timedelta, timezone
import json
from time import time, monotonic
from . import catalog, price_catalog

SCHEMA = '''CREATE TABLE IF NOT EXISTS ticket_source(
 city TEXT NOT NULL, poi_id TEXT NOT NULL, name TEXT NOT NULL, url TEXT NOT NULL,
 next_due REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'new',
 PRIMARY KEY(city,poi_id));'''


def register(city, poi_id, name, url):
    from .ticket_web import trusted_url
    if not trusted_url(url):
        raise ValueError('Source must be a maintained official HTTPS host')
    with catalog._connection() as db:
        db.execute(SCHEMA)
        db.execute('INSERT INTO ticket_source(city,poi_id,name,url) VALUES(?,?,?,?) ON CONFLICT(city,poi_id) DO UPDATE SET name=excluded.name,url=excluded.url',
                   (city.removesuffix('市'), poi_id, name, url))


def refresh(limit=3):
    from .execution_lease import hold, Busy
    from .ticket_web import _get, extract_policy
    try:
        with hold('ticket-source-refresh'):
            with catalog._connection() as db:
                db.execute(SCHEMA)
                rows = db.execute('SELECT * FROM ticket_source WHERE next_due<=? ORDER BY next_due LIMIT ?', (time(), limit)).fetchall()
            result = []
            for row in rows:
                status = 'ok'
                try:
                    url, page = _get(row['url'], monotonic()+5)
                    now = datetime.now(timezone.utc)
                    records = []
                    for offset in range(7):
                        target = (now.astimezone(timezone(timedelta(hours=8))).date()+timedelta(days=offset)).isoformat()
                        policy = extract_policy(page, row['name'], row['city'], target)
                        records.append({'city': row['city'], 'poi_id': row['poi_id'], 'kind':'attraction', 'unit':'admission',
                                        'amount':policy['amount'], 'status':'estimated', 'source_url':url,
                                        'observed_at':now.isoformat(), 'expires_at':(now+timedelta(days=1)).isoformat(),
                                        'service_from':target, 'service_to':target, 'note':'日常成人政策参考，非优惠成交价。原文：'+policy['quote']})
                    price_catalog.import_prices(records)
                except Exception as exc:
                    status = type(exc).__name__
                with catalog._connection() as db:
                    db.execute('UPDATE ticket_source SET next_due=?,status=? WHERE city=? AND poi_id=?',
                               (time()+(86400 if status=='ok' else 900), status,row['city'],row['poi_id']))
                result.append({'city':row['city'],'poi_id':row['poi_id'],'status':status})
            return result
    except Busy:
        return []


def coverage():
    counts = {'places':0,'priced':0,'sourced':0,'model_estimated':0,'reserve':0}
    with catalog._connection() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='place_catalog'").fetchone()
        if not exists:
            return counts
        rows = db.execute("SELECT payload_json FROM place_catalog WHERE kind='attraction'").fetchall()
    for row in rows:
        item = json.loads(row[0])
        counts['places'] += 1
        if item.get('ticket_price') is not None:
            counts['priced'] += 1
        source = item.get('ticket_price_source')
        if source in ('amap','imported'):
            counts['sourced'] += 1
        elif source == 'llm_estimated':
            counts['model_estimated'] += 1
        elif source == 'static_estimate':
            counts['reserve'] += 1
    return counts
