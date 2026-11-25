from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=2000)]


class PlanObject(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rows(PlanObject):
    sheet: Text
    data_rows: int = Field(ge=0)
    header_rows: int = Field(default=1, ge=0)


class Published(PlanObject):
    method: Literal["program"]
    rule: Literal["published"]


class SheetCheck(PlanObject):
    method: Literal["program"]
    rule: Literal["published_xlsx_rows"]
    params: Rows


class SelfAssessment(PlanObject):
    method: Literal["self"] = "self"


class UserReview(PlanObject):
    method: Literal["user"]
    question: Text


class Criterion(PlanObject):
    text: Text
    required: bool = True
    verification: Published | SheetCheck | SelfAssessment | UserReview = Field(default_factory=SelfAssessment)


class Initial(PlanObject):
    goal: Text
    constraints: list[Text] = Field(default_factory=list, max_length=30)
    criteria: list[Criterion] = Field(default_factory=list, max_length=30)


class RequirementOperation(PlanObject):
    op: Literal["add", "replace", "remove"]
    field: Literal["goal", "constraints", "criteria"]
    id: str | None = None
    client_ref: str | None = None
    value: Text | Criterion | None = None
    evidence: Text


class RequirementPatch(PlanObject):
    expected_revision: int | None = Field(default=None, ge=0)
    operations: list[RequirementOperation] = Field(default_factory=list, max_length=30)


class LocationIntent(PlanObject):
    intent: Text
    device_id: str | None = None
    workspace_id: str | None = None


class Wait(PlanObject):
    step_key: Text
    criterion_ref: Text
    verdict: Literal["met"] = "met"
    artifact_role: Text = "response"


class StepSpec(PlanObject):
    goal: Text
    constraints: list[Text] = Field(default_factory=list)
    criteria: list[Text] = Field(default_factory=list)
    local_reviews: dict[str, Criterion] = Field(default_factory=dict)
    criterion_refs: list[str] = Field(default_factory=list)
    wait_for: list[Wait] = Field(default_factory=list)


class Item(PlanObject):
    key: Text
    relation: Literal["new", "continue", "refine", "followup"] = "new"
    task_id: str | None = None
    initial_requirements: Initial | None = None
    step_spec: StepSpec
    depends_on: list[str] = Field(default_factory=list)
    task_patch: RequirementPatch = Field(default_factory=RequirementPatch)
    input_locations: list[LocationIntent] = Field(default_factory=list)
    output_target: LocationIntent | None = None

    @model_validator(mode="after")
    def check_initial(self):
        fresh = self.relation in ("new", "followup")
        if (
            fresh != bool(self.initial_requirements)
            or fresh
            and self.task_patch.operations
        ):
            raise ValueError(
                "new/followup requires initial_requirements; persistent patches apply to existing tasks"
            )
        return self


class Plan(PlanObject):
    schema_version: Literal[2] = 2
    kind: Literal["chat", "tasks", "clarify"]
    question: str = ""
    items: list[Item] = Field(default_factory=list, max_length=6)


class Delivery(PlanObject):
    artifact_id: Text
    role: Text


class Milestone(PlanObject):
    key: Text
    text: Text
    evidence_refs: list[Text] = Field(default_factory=lambda: ["response"], min_length=1)


class Blocker(PlanObject):
    op: Literal["open", "resolve"]
    id: Text
    text: Text
    evidence_refs: list[Text] = Field(default_factory=lambda: ["response"], min_length=1)


class Claim(PlanObject):
    id: Text
    claim: Literal["met", "partial", "unmet"]
    reason: Text
    artifact_id: Text | None = Field(default=None, description="Published delivery for a program check or explicit file review only")


class FinishStep(PlanObject):
    schema_version: Literal[2] = 2
    step_outcome: Literal["completed", "partial", "blocked", "failed"]
    final_text: str = Field(min_length=1, max_length=100000)
    delivery_summary: Text | None = None
    deliveries: list[Delivery] = Field(default_factory=list, max_length=30)
    milestones: list[Milestone] = Field(default_factory=list, max_length=30)
    blocker_changes: list[Blocker] = Field(default_factory=list, max_length=30)
    criterion_claims: list[Claim] = Field(default_factory=list, max_length=30)
