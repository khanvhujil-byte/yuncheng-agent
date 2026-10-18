"""Restore domain objects at the JSON persistence boundary."""
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..models.schemas import Attraction, ConstraintReport, Hotel, Location, Restaurant, TripRequest, WeatherInfo


class StateRestoreError(ValueError):
    """Persisted state cannot satisfy the runtime model contract."""


_MODEL_FIELDS = {
    "request": TypeAdapter(TripRequest),
    "city_center": TypeAdapter(Location | None),
    "must_visit_resolved": TypeAdapter(list[Attraction]),
    "hotel_anchor": TypeAdapter(Hotel | None),
    "attractions": TypeAdapter(list[Attraction]),
    "restaurants": TypeAdapter(list[Restaurant]),
    "weather": TypeAdapter(list[WeatherInfo]),
    "hotels": TypeAdapter(list[Hotel]),
    "hotel": TypeAdapter(Hotel | None),
    "constraint_report": TypeAdapter(ConstraintReport | None),
}


def restore_state_types(state: dict[str, Any]) -> dict[str, Any]:
    """Validate model fields, preserving mappings such as plan, sources, and usage.

    Missing fields stay missing so partial journal inspection remains possible.
    Existing model instances and optional None values are also accepted.
    """
    restored = dict(state)
    for key, adapter in _MODEL_FIELDS.items():
        if key not in state:
            continue
        try:
            restored[key] = adapter.validate_python(state[key])
        except ValidationError as exc:
            raise StateRestoreError(f"状态字段 {key} 不符合恢复结构") from exc
    return restored
