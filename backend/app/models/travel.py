"""Read-only platform queries; separate from itinerary and map contracts."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def china_today():
    return datetime.now(timezone(timedelta(hours=8))).date()


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    @field_validator('*', mode='after')
    @classmethod
    def safe_text(cls, value):
        if isinstance(value, str) and (value.startswith('-') or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('查询文本不能包含控制字符或命令选项')
        return value


class PoiQuery(Query):
    city: str = Field(min_length=1, max_length=60)
    keyword: str = Field(default='', max_length=100)


class HotelQuery(Query):
    city: str = Field(min_length=1, max_length=60)
    keyword: str = Field(default='', max_length=100)
    check_in: date
    check_out: date
    max_price: int | None = Field(default=None, ge=1, le=100000)
    sort: Literal['no_rank', 'distance_asc', 'rate_desc', 'price_asc', 'price_desc'] = 'no_rank'

    @model_validator(mode='after')
    def dates(self):
        if self.check_in <= china_today() or self.check_out <= self.check_in or (self.check_out-self.check_in).days > 30:
            raise ValueError('入住须为未来日期，离店须晚于入住且不超过30晚')
        return self


class TransportQuery(Query):
    origin: str = Field(min_length=1, max_length=60)
    destination: str = Field(min_length=1, max_length=60)
    departure_date: date
    max_price: int | None = Field(default=None, ge=1, le=100000)

    @model_validator(mode='after')
    def dates(self):
        if self.origin == self.destination or self.departure_date <= china_today():
            raise ValueError('出发地与目的地必须不同，出发日期须为未来日期')
        return self


def cny_amount(value) -> float | None:
    """An exact nonnegative yuan number; never interpret '起'/ranges as a quote."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    text = str(value).strip()
    match = re.fullmatch(r'(?:[¥￥]|CNY\s*)?\s*(\d{1,6}(?:\.\d{1,2})?)\s*(?:元)?', text)
    if not match:
        return None
    amount = Decimal(match.group(1))
    return float(amount) if amount <= 100000 else None
