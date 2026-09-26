"""专利家族关系维护的请求模型。

同一项技术会在多个国家提交申请，继续申请、分案与优先权主张会在不同
时间补录，因此模型同时覆盖单条登记、批量导入与变更审批三种入口。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

RELATION_TYPES = ("priority", "continuation", "divisional", "continuation_in_part")


class FamilyCreate(BaseModel):
    family_code: str = Field(min_length=3, max_length=64)
    title: str = Field(min_length=2, max_length=200)


class ApplicationCreate(BaseModel):
    family_code: str = Field(min_length=3, max_length=64)
    application_number: str = Field(min_length=3, max_length=80)
    jurisdiction: str = Field(min_length=2, max_length=30)
    title: str = Field(min_length=2, max_length=300)
    applicant: str = Field(default="", max_length=200)
    filing_date: str = Field(min_length=10, max_length=10)
    declared_priority_date: str | None = Field(default=None, min_length=10, max_length=10)
    publication_status: Literal["unpublished", "published"] = "unpublished"
    is_secret_asset: bool = False
    family_title: str | None = Field(default=None, min_length=2, max_length=200)


class ImportApplication(BaseModel):
    application_number: str = Field(min_length=3, max_length=80)
    jurisdiction: str = Field(min_length=2, max_length=30)
    title: str = Field(min_length=2, max_length=300)
    applicant: str = Field(default="", max_length=200)
    filing_date: str = Field(min_length=10, max_length=10)
    declared_priority_date: str | None = Field(default=None, min_length=10, max_length=10)
    publication_status: Literal["unpublished", "published"] = "unpublished"
    is_secret_asset: bool = False


class ImportRelation(BaseModel):
    parent_application_number: str = Field(min_length=3, max_length=80)
    child_application_number: str = Field(min_length=3, max_length=80)
    relation_type: Literal["priority", "continuation", "divisional", "continuation_in_part"]


class FamilyImport(BaseModel):
    family_code: str = Field(min_length=3, max_length=64)
    family_title: str | None = Field(default=None, min_length=2, max_length=200)
    applications: list[ImportApplication] = Field(min_length=1, max_length=500)
    relations: list[ImportRelation] = Field(default_factory=list, max_length=1000)


class LinkRelationPayload(BaseModel):
    parent_application_id: int = Field(gt=0)
    child_application_id: int = Field(gt=0)
    relation_type: Literal["priority", "continuation", "divisional", "continuation_in_part"]


class MergeLinkPayload(BaseModel):
    parent_application_id: int = Field(gt=0)
    child_application_id: int = Field(gt=0)
    relation_type: Literal["priority", "continuation", "divisional", "continuation_in_part"]


class MergeRehearse(BaseModel):
    source_family_id: int = Field(gt=0)
    target_family_id: int = Field(gt=0)
    link: MergeLinkPayload | None = None


class ChangeCreate(BaseModel):
    change_type: Literal["link", "revoke", "merge"]
    family_id: int = Field(gt=0)
    link: LinkRelationPayload | None = None
    relation_id: int | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, min_length=2, max_length=500)
    source_family_id: int | None = Field(default=None, gt=0)
    merge_link: MergeLinkPayload | None = None

    @model_validator(mode="after")
    def ensure_payload(self):
        if self.change_type == "link" and self.link is None:
            raise ValueError("link 变更必须提供 link 关系内容")
        if self.change_type == "revoke" and (self.relation_id is None or not self.reason):
            raise ValueError("revoke 变更必须提供 relation_id 与 reason")
        if self.change_type == "merge" and self.source_family_id is None:
            raise ValueError("merge 变更必须提供 source_family_id")
        return self


class ChangeDecision(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)
