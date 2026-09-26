from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

RelationType = Literal[
    "priority", "continuation", "divisional", "continuation_in_part", "national_phase"
]
ApplicationStatus = Literal["draft", "filed", "published", "granted", "abandoned", "merged"]


class ApplicationCreate(BaseModel):
    jurisdiction: str = Field(min_length=2, max_length=30)
    application_number: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    application_status: ApplicationStatus = "filed"
    filing_date: str = Field(min_length=8, max_length=40, description="ISO 日期或日期时间")
    publication_date: str | None = Field(default=None, min_length=8, max_length=40)
    grant_date: str | None = Field(default=None, min_length=8, max_length=40)
    priority_claim_date: str | None = Field(default=None, min_length=8, max_length=40)
    secret_asset: bool = False
    dossier_id: int | None = Field(default=None, gt=0)


class ApplicationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    application_status: ApplicationStatus | None = None
    filing_date: str | None = Field(default=None, min_length=8, max_length=40)
    publication_date: str | None = Field(default=None, min_length=8, max_length=40)
    grant_date: str | None = Field(default=None, min_length=8, max_length=40)
    priority_claim_date: str | None = Field(default=None, min_length=8, max_length=40)
    secret_asset: bool | None = None


class LinkRelationRequest(BaseModel):
    change_code: str | None = Field(default=None, min_length=3, max_length=64)
    child_application_id: int = Field(gt=0)
    parent_application_id: int = Field(gt=0)
    relation_type: RelationType
    claimed_priority_date: str | None = Field(default=None, min_length=8, max_length=40)
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=100)
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _distinct_endpoints(self) -> "LinkRelationRequest":
        if self.child_application_id == self.parent_application_id:
            raise ValueError("关系的两端不能是同一份申请")
        return self


class MergePreviewRequest(BaseModel):
    source_application_ids: list[int] = Field(min_length=1, max_length=100)
    target_application_id: int = Field(gt=0)

    @model_validator(mode="after")
    def _target_not_in_sources(self) -> "MergePreviewRequest":
        if len(set(self.source_application_ids)) != len(self.source_application_ids):
            raise ValueError("待合并成员存在重复")
        if self.target_application_id in self.source_application_ids:
            raise ValueError("保留成员不能同时出现在被合并成员中")
        return self


class MergeMembersRequest(MergePreviewRequest):
    change_code: str | None = Field(default=None, min_length=3, max_length=64)
    reason: str = Field(min_length=4, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=100)


class RevokeRelationRequest(BaseModel):
    change_code: str | None = Field(default=None, min_length=3, max_length=64)
    link_id: int = Field(gt=0)
    reason: str = Field(min_length=4, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=4, max_length=100)


class ChangeDecision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)
