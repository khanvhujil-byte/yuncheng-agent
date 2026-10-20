"""User requirements shared by collection, selection, scheduling and auditing."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ShortText = Annotated[str, Field(min_length=1, max_length=200)]
Names = Annotated[list[ShortText], Field(max_length=10)]


class ModuleNeeds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instructions: Annotated[str, Field(max_length=1000)] = ""
    unverified: Names = Field(default_factory=list)


class PlaceNeeds(ModuleNeeds):
    keywords: Names = Field(default_factory=list)
    excluded_names: Names = Field(default_factory=list)


class FoodNeeds(PlaceNeeds):
    priority: Literal["nearby", "food_first"] = "nearby"
    travel_scope: Literal["nearby", "citywide"] = "nearby"
    max_cost_per_meal: Annotated[int, Field(ge=1, le=100_000)] | None = None


class AttractionNeeds(PlaceNeeds):
    max_per_day: Annotated[int, Field(ge=1, le=4)] = 4
    day_goals: Annotated[list['DayGoal'], Field(max_length=10)] = Field(default_factory=list)


class DayGoal(BaseModel):
    """A theme must be the main activity on this many distinct trip days."""
    model_config = ConfigDict(extra="forbid", strict=True)
    theme: ShortText
    days: Annotated[int, Field(ge=1, le=30)]
    scope: Literal["city", "surrounding"] = "city"
    day_indices: Annotated[list[Annotated[int, Field(ge=0, le=29)]], Field(max_length=30)] = Field(default_factory=list)


class HotelNeeds(PlaceNeeds):
    stay_policy: Literal["auto", "prefer_same", "must_same"] = "auto"
    luggage_policy: Literal["carry", "storage_required"] = "carry"

    max_price_per_night: Annotated[int, Field(ge=1, le=100_000)] | None = None


class TransportNeeds(ModuleNeeds):
    max_leg_minutes: Annotated[int, Field(ge=1, le=300)] | None = None
    allow_estimated_routes: bool = True


class PaceNeeds(ModuleNeeds):
    max_day_minutes: Annotated[int, Field(ge=120, le=660)] = 660
    max_activity_minutes: Annotated[int, Field(ge=30, le=600)] | None = None


class WeatherNeeds(ModuleNeeds):
    avoid_rain_outdoors: bool = False


class Requirements(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    food: FoodNeeds = Field(default_factory=FoodNeeds)
    attractions: AttractionNeeds = Field(default_factory=AttractionNeeds)
    hotel: HotelNeeds = Field(default_factory=HotelNeeds)
    transport: TransportNeeds = Field(default_factory=TransportNeeds)
    pace: PaceNeeds = Field(default_factory=PaceNeeds)
    weather: WeatherNeeds = Field(default_factory=WeatherNeeds)
    budget: ModuleNeeds = Field(default_factory=ModuleNeeds)
