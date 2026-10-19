"""Local-first candidate policy shared by collection tools.

Catalog reads never refresh observations. Current date-specific prices and hard
constraints are applied before counting a cache hit as sufficient.
"""
import logging
from dataclasses import replace

from ..config import get_settings
from ..models.schemas import Attraction, Hotel, Restaurant
from . import catalog, price_catalog
from .requirements import excluded, hotel_allowed, restaurant_allowed

logger = logging.getLogger(__name__)


def minimum_candidates(kind, days=1):
    if kind == "hotel":
        return 3
    return min(12 if kind == "restaurant" else 6, max(4 if kind == "restaurant" else 2, days * 2))


def priced_allowed(city, kind, places, needs, *, start_date=None, days=1, nights=0):
    """Never reuse an old hotel's model estimate as a current room quote."""
    if kind == "hotel":
        places = [p.model_copy(update={"estimated_cost": 0, "cost_status": "unknown",
                                      "cost_source": "unknown", "price_evidence_url": None,
                                      "nights": nights, "price_range": ""}) for p in places]
    places = price_catalog.apply_prices(city, kind, places, service_date=start_date,
                                        travel_days=days, nights=nights)
    if kind == "restaurant":
        return [p for p in places if restaurant_allowed(p, needs)]
    if kind == "hotel":
        return [p for p in places if p.location is not None and hotel_allowed(p, needs)]
    return [p for p in places if not excluded(p.name, needs) and not any(
        code[:2] in ("05", "10") for code in (p.typecode or "").split("|") if code)]


def local_candidates(city, kind, query, needs, *, start_date=None, days=1, nights=0, limit=50):
    report = {"local_count": 0, "upstream_count": 0, "upstream_calls": 0,
              "cache_hit": 0, "retrieval_decision": "upstream_fill", "retrieval_reasons": []}
    if not getattr(get_settings(), "catalog_local_first", True):
        report["retrieval_reasons"] = ["local_first_disabled"]
        return [], report
    try:
        rows, search_report = catalog.search_places(city, kind, query=query, limit=limit)
        model = {"attraction": Attraction, "hotel": Hotel, "restaurant": Restaurant}[kind]
        places = []
        for raw in rows:
            try:
                places.append(model.model_validate(raw))
            except (ValueError, TypeError):
                report["retrieval_reasons"].append("invalid_local_payload")
        valid = priced_allowed(city, kind, places, needs, start_date=start_date, days=days, nights=nights)
        report["local_count"] = len(valid)
        # Tool messages carry compact diagnostics, not a second copy of POI
        # evidence rows. Full provenance remains available in the catalog.
        report["local_search"] = {key: value for key, value in search_report.items() if key != "records"}
        if len(valid) < len(places):
            report["retrieval_reasons"].append("hard_constraints_filtered")
        if not valid:
            report["retrieval_reasons"].append("no_fresh_relevant_local_candidates")
        return valid, report
    except Exception:
        logger.exception("Local %s catalog lookup failed; upstream fallback remains available", kind)
        report["retrieval_reasons"].append("catalog_read_failed")
        return [], report


def decision(report, sufficient):
    report = dict(report)
    report["retrieval_reasons"] = list(report["retrieval_reasons"])
    if sufficient:
        report.update(cache_hit=1, retrieval_decision="local_hit")
    else:
        report["retrieval_reasons"].append("insufficient_usable_candidates")
    return report


def with_stats(result, report):
    return replace(result, stats={**result.stats, **report})


def save_fresh(city, kind, places, report):
    """Only callers holding genuinely new upstream results may invoke this."""
    if not places:
        return
    try:
        catalog.upsert_places(city, kind, places, source="amap" if get_settings().map_backend == "local" else "mcp")
    except Exception:
        logger.exception("Fresh %s candidates could not be persisted", kind)
        report["retrieval_reasons"].append("catalog_write_failed")
