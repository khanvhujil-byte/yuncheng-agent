import json

from app.services import price_catalog, store
from tests.test_price_catalog import observation, attraction


def test_different_service_dates_do_not_overwrite_each_other():
    price_catalog.import_prices([observation(amount=30,service_from='2026-11-01',service_to='2026-11-01'),
                                 observation(amount=40,service_from='2026-11-02',service_to='2026-11-02')])
    for day, amount in [('2026-11-01',30),('2026-11-02',40)]:
        assert price_catalog.apply_prices('杭州','attraction',[attraction()],service_date=day)[0].ticket_price==amount


def test_old_database_migrates_without_discarding_price_evidence():
    old = observation(amount=0,service_from='2026-11-01',service_to='2026-11-01')
    with store._connect() as db:
        db.execute('''CREATE TABLE price_observation(city TEXT,kind TEXT,poi_id TEXT,unit TEXT,
            source_url TEXT,payload_json TEXT,observed_at TEXT,expires_at TEXT,
            PRIMARY KEY(city,kind,poi_id,unit,source_url))''')
        db.execute('INSERT INTO price_observation VALUES(?,?,?,?,?,?,?,?)',
                   tuple(old[k] for k in ('city','kind','poi_id','unit','source_url'))+
                   (json.dumps(old),old['observed_at'],old['expires_at']))
    price_catalog.import_prices([observation(amount=40,service_from='2026-11-02',service_to='2026-11-02')])
    assert price_catalog.apply_prices('杭州','attraction',[attraction()],service_date='2026-11-01')[0].ticket_price==0
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM price_observation').fetchone()[0]==2
    price_catalog.import_prices([observation(amount=50,service_from='2026-11-02',service_to='2026-11-02')])
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM price_observation').fetchone()[0]==2
