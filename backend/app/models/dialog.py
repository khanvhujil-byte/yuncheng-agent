"""Strict boundaries for conversation routing and explicit memory writes."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .requirements import Requirements

Text = Annotated[str, Field(min_length=1, max_length=200)]


class SlotPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    city: Text | None = None
    start_date: Annotated[str, Field(max_length=10)] | None = None
    travel_days: Annotated[int, Field(ge=1, le=30)] | None = None
    end_date: Annotated[str, Field(max_length=10)] | None = None
    nights: Annotated[int, Field(ge=0, le=30)] | None = None
    transportation: Text | None = None
    accommodation: Text | None = None
    budget_limit: Annotated[int, Field(ge=1, le=10_000_000)] | None = None
    preferences: Annotated[list[Text], Field(max_length=20)] | None = None
    must_visit: Annotated[list[Text], Field(max_length=10)] | None = None
    fixed_hotel: Text | None = None
    fixed_hotel_address: Text | None = None
    free_text_input: Annotated[str, Field(max_length=2000)] | None = None
    # Nested patches stay sparse; conversation.merge deep-merges only supplied fields.
    requirements: Requirements | None = None


class ClarificationOption(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label: Annotated[str, Field(min_length=1, max_length=120)]
    slot_updates: SlotPatch = Field(default_factory=SlotPatch)

    @model_validator(mode="after")
    def explicit_effect(self):
        def has_leaf(value):
            return any(has_leaf(item) for item in value.values()) if isinstance(value, dict) else True
        if not has_leaf(self.slot_updates.model_dump(exclude_unset=True)):
            raise ValueError("选项必须明确说明选择后修改的字段；需要用户补充资料时使用无选项问题。")
        return self


class ClarificationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    module: Literal["food", "attractions", "hotel", "transport", "pace", "weather", "budget", "general"]
    question: Annotated[str, Field(min_length=1, max_length=400)]
    # Empty options means that a specific name, date or free-text answer is needed.
    options: Annotated[list[ClarificationOption], Field(max_length=4)] = Field(default_factory=list)


class ModuleEdit(BaseModel):
    """Only selectors cross this boundary; factual POI fields never come from clients."""
    model_config = ConfigDict(extra="forbid", strict=True)
    module: Literal["hotel", "meals", "attractions", "day"]
    day_index: Annotated[int, Field(ge=0, le=29)] | None = None
    item_index: Annotated[int, Field(ge=0, le=29)] | None = None
    candidate_id: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    candidate_ids: Annotated[list[Text], Field(min_length=1, max_length=6)] | None = None
    instruction: Annotated[str, Field(max_length=2000)] = ""


class IntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["new_plan", "modify_plan", "edit_module", "explain_plan", "knowledge", "clarify"]
    slot_updates: SlotPatch = Field(default_factory=SlotPatch)
    edit: ModuleEdit | None = None
    clarifications: Annotated[list[ClarificationQuestion], Field(max_length=3)] = Field(default_factory=list)
    resolved_question_ids: Annotated[list[Text], Field(max_length=3)] = Field(default_factory=list)


class ClarificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question_id: Annotated[str, Field(min_length=1, max_length=80)]
    option_index: Annotated[int, Field(ge=0, le=3)]


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=80)
    expected_revision: int = Field(ge=0)
    action: Literal["message", "edit", "answer", "confirm", "reject"] = "message"
    message: str = Field(default="", max_length=4000)
    slot_updates: SlotPatch = Field(default_factory=SlotPatch)
    edit: ModuleEdit | None = None
    answer: ClarificationAnswer | None = None


class Credentials(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_]{3,32}$")
    password: str = Field(min_length=10, max_length=128)


class RunImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class PreferenceInput(BaseModel):
    expected_version: int = Field(ge=0)
    value: str | int | list[str] | None
    source: str = Field(min_length=1, max_length=1000)


class DocumentInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=1000)
    content: str = Field(min_length=1, max_length=100_000)
    valid_until: str | None = None
