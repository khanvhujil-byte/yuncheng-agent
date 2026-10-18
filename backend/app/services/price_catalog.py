"""Explicit source-backed price observations, separate from POI identity facts.

Imports are a maintainer action: a source URL is evidence for review, not proof
that a fetched page was automatically interpreted as a binding future quote.
"""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import store


class PriceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    city: str = Field(min_length=1, max_length=80)
    kind: Literal["attraction", "hotel", "restaurant"]
    poi_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9]+$")
    amount: int = Field(ge=0, le=10_000_000)
    unit: Literal["admission", "night", "person_meal"]
    status: Literal["known", "estimated"]
    source_url: str = Field(max_length=2000)
    observed_at: str
    expires_at: str
    service_from: str | None = None
    service_to: str | None = None
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def consistency(self):
        if self.unit != {"attraction": "admission", "hotel": "night", "restaurant": "person_meal"}[self.kind]:
            raise ValueError("价格单位与实体类型不匹配")
        source = urlparse(self.source_url)
        if source.scheme != "https" or not source.hostname or source.username or source.password:
            raise ValueError("价格证据必须是 HTTPS 来源，不能包含账号密码")
        observed, expires = datetime.fromisoformat(self.observed_at), datetime.fromisoformat(self.expires_at)
        if observed.tzinfo is None or expires.tzinfo is None or expires <= observed:
            raise ValueError("观察时间与失效时间必须带时区且先后合法")
        if observed > datetime.now(timezone.utc):
            raise ValueError("价格观察时间不能在未来")
        if expires - observed > timedelta(days=90):
            raise ValueError("价格证据有效期最多90天，需要持续维护")
        if bool(self.service_from) != bool(self.service_to):
            raise ValueError("适用日期必须同时提供起止")
        if self.kind in {"attraction", "hotel"} and not self.service_from:
            raise ValueError("门票和房价必须指定适用日期，不能把当前报价套用未来行程")
        if self.service_from and date.fromisoformat(self.service_from) > date.fromisoformat(self.service_to):
            raise ValueError("适用日期范围不合法")
        if self.kind == "restaurant" and self.status != "estimated":
            raise ValueError("餐厅人均消费只能是估计，不能冒充用户实际账单")
        return self


SCHEMA = """CREATE TABLE IF NOT EXISTS price_observation (
 city TEXT NOT NULL,kind TEXT NOT NULL,poi_id TEXT NOT NULL,
 unit TEXT NOT NULL,source_url TEXT NOT NULL,payload_json TEXT NOT NULL,
 observed_at TEXT NOT NULL,expires_at TEXT NOT NULL,
 PRIMARY KEY(city,kind,poi_id,unit,source_url)
)"""


def import_prices(records):
    if not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ValueError("一次导入必须为1到1000条价格观察")
    validated = [PriceObservation.model_validate(record) for record in records]
    rows = []
    for record in validated:
        observed = datetime.fromisoformat(record.observed_at).astimezone(timezone.utc).isoformat()
        expires = datetime.fromisoformat(record.expires_at).astimezone(timezone.utc).isoformat()
        rows.append((record.city.removesuffix("市"), record.kind, record.poi_id, record.unit, record.source_url,
                     record.model_dump_json(), observed, expires))
    store.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(store.DB_PATH, timeout=10)
    try:
        with db:
            db.execute(SCHEMA)
            db.executemany("""INSERT INTO price_observation VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(city,kind,poi_id,unit,source_url) DO UPDATE SET
                payload_json=excluded.payload_json,observed_at=excluded.observed_at,
                expires_at=excluded.expires_at""", rows)
    finally:
        db.close()
    return len(rows)


def apply_prices(city, kind, places, *, service_date=None, nights=0, travel_days=1):
    """Apply only exact POI matches and date-compatible, unexpired observations."""
    # Cached catalog entities may carry a price observation that has since
    # expired. Clear it before matching the current evidence table.
    cleaned = []
    for place in places:
        source = getattr(place, "ticket_price_source" if kind == "attraction" else "cost_source", None)
        if source == "imported":
            if kind == "attraction":
                fields = {"ticket_price": None, "ticket_price_status": "unknown", "ticket_price_source": "unknown", "ticket_price_evidence_url": None}
            elif kind == "hotel":
                fields = {"estimated_cost": 0, "cost_status": "unknown", "cost_source": "unknown", "price_evidence_url": None}
            else:
                fields = {"avg_cost": None, "avg_cost_status": "unknown", "cost_source": "unknown", "evidence_url": None}
            place = place.model_copy(update=fields)
        cleaned.append(place)
    places = cleaned
    if not store.DB_PATH.exists():
        return list(places)
    db = sqlite3.connect(store.DB_PATH, timeout=10)
    try:
        # Read paths do not create tables or update observation freshness.
        exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='price_observation'").fetchone()
        if not exists:
            return list(places)
        rows = db.execute("""SELECT payload_json FROM price_observation WHERE city=? AND kind=?
            AND expires_at>? ORDER BY observed_at DESC""",
                          (city.removesuffix("市"), kind, datetime.now(timezone.utc).isoformat())).fetchall()
    finally:
        db.close()
    observations = {}
    start = date.fromisoformat(service_date) if service_date else None
    count = max(nights, 1) if kind == "hotel" else max(travel_days, 1)
    end = start + timedelta(days=count - 1) if start else None
    for row in rows:
        record = json.loads(row[0])
        if record.get("service_from"):
            if not start or date.fromisoformat(record["service_from"]) > start or date.fromisoformat(record["service_to"]) < end:
                continue
        elif kind in {"hotel", "attraction"}:
            continue
        observations.setdefault(record["poi_id"], record)
    updated = []
    for place in places:
        record = observations.get(place.poi_id)
        if not record:
            updated.append(place)
            continue
        if kind == "attraction":
            fields = {"ticket_price": record["amount"], "ticket_price_status": record["status"],
                      "ticket_price_source": "imported", "ticket_price_evidence_url": record["source_url"]}
        elif kind == "hotel":
            fields = {"estimated_cost": record["amount"], "cost_status": record["status"],
                      "cost_source": "imported", "price_evidence_url": record["source_url"]}
        else:
            fields = {"avg_cost": record["amount"], "avg_cost_status": "estimated", "cost_source": "imported",
                      "evidence_url": record["source_url"]}
        updated.append(place.model_copy(update=fields))
    return updated
