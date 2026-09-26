"""专利家族关系维护的领域服务。

覆盖关系类型校验、优先权日期继承、成员合并前的冲突预演、撤销后的
影响查询，以及"变更通过审核后才生效"的审批流转。所有写操作由路由
层在 IMMEDIATE 事务中调用，保证图状态与审计事件同生共死。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.families.graph import (
    connected_components,
    effective_priority_dates,
    reachable,
    topological_order,
)
from app.families.repository import (
    ApplicationRepository,
    ChangeRepository,
    FamilyEventRepository,
    FamilyRepository,
    RelationRepository,
)
from app.services.audit import AuditService

SAME_JURISDICTION_TYPES = {"continuation", "divisional", "continuation_in_part"}
RELATION_TYPE_LABELS = {
    "priority": "优先权主张",
    "continuation": "继续申请",
    "divisional": "分案申请",
    "continuation_in_part": "部分继续申请",
}


def _require_date(value: str, field: str) -> str:
    """校验 YYYY-MM-DD 日期并返回规范化文本。"""
    try:
        parsed = date.fromisoformat(value.strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field}必须是 YYYY-MM-DD 格式的有效日期") from exc
    return parsed.isoformat()


def _normalize_number(value: str) -> str:
    return value.strip().upper()


def _check_secrecy(publication_status: str, is_secret_asset: bool) -> None:
    if publication_status == "published" and is_secret_asset:
        raise ValidationError("已公开申请不能登记为秘密资产")


class FamilyService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.families = FamilyRepository(connection)
        self.applications = ApplicationRepository(connection)
        self.relations = RelationRepository(connection)
        self.changes = ChangeRepository(connection)
        self.events = FamilyEventRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_family.write")
        family_code = _normalize_number(data["family_code"])
        if self.families.by_code(family_code):
            raise ConflictError("家族编码已经存在")
        now = to_storage(self.clock.now())
        family = self.families.create(family_code, data["title"], now)
        self.events.append(family["id"], "family.created", principal.user_id, now, {"family_code": family_code})
        self.audit.record(principal, "patent_family.create", "patent_family", str(family["id"]), after=family)
        return family

    def list(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("patent_family.read")
        return self.families.list()

    def detail(self, principal: Principal, family_id: int) -> dict[str, Any]:
        principal.require("patent_family.read")
        family = self.families.get(family_id)
        family["members"] = self.applications.members(family_id)
        return family

    def _graph_inputs(self, family_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        members = self.applications.members(family_id)
        active = self.relations.family_relations(family_id, status="active")
        return members, active

    def topology(self, principal: Principal, family_id: int) -> dict[str, Any]:
        principal.require("patent_family.read")
        family = self.families.get(family_id)
        members, active = self._graph_inputs(family_id)
        order = topological_order([item["id"] for item in members], active)
        effective, source, _ = effective_priority_dates(members, active)
        child_ids = {edge["child_application_id"] for edge in active}
        parent_ids = {edge["parent_application_id"] for edge in active}
        nodes = []
        for member in members:
            nodes.append(
                {
                    **member,
                    "effective_priority_date": effective[member["id"]],
                    "priority_source_application_id": source[member["id"]],
                }
            )
        return {
            "family": family,
            "nodes": nodes,
            "edges": active,
            "topological_order": order,
            "roots": [item["id"] for item in members if item["id"] not in child_ids],
            "leaves": [item["id"] for item in members if item["id"] not in parent_ids],
            "revoked_relation_count": len(self.relations.family_relations(family_id, status="revoked")),
        }

    def summary(self, principal: Principal, family_id: int) -> dict[str, Any]:
        principal.require("patent_family.read")
        family = self.families.get(family_id)
        members, active = self._graph_inputs(family_id)
        by_jurisdiction: dict[str, int] = {}
        by_publication: dict[str, int] = {}
        secret_asset_count = 0
        for member in members:
            by_jurisdiction[member["jurisdiction"]] = by_jurisdiction.get(member["jurisdiction"], 0) + 1
            by_publication[member["publication_status"]] = by_publication.get(member["publication_status"], 0) + 1
            if member["is_secret_asset"] and member["publication_status"] == "unpublished":
                secret_asset_count += 1
        relations_by_type: dict[str, int] = {}
        for edge in active:
            relations_by_type[edge["relation_type"]] = relations_by_type.get(edge["relation_type"], 0) + 1
        effective, _, _ = effective_priority_dates(members, active) if members else ({}, {}, {})
        revoked = self.relations.family_relations(family_id, status="revoked")
        pending = self.changes.pending_for_family(family_id)
        return {
            "family": family,
            "member_count": len(members),
            "by_jurisdiction": dict(sorted(by_jurisdiction.items())),
            "by_publication_status": dict(sorted(by_publication.items())),
            "secret_asset_count": secret_asset_count,
            "active_relation_count": len(active),
            "relations_by_type": dict(sorted(relations_by_type.items())),
            "revoked_relation_count": len(revoked),
            "pending_change_count": len(pending),
            "earliest_effective_priority_date": min(effective.values()) if effective else None,
        }

    def event_log(self, principal: Principal, family_id: int) -> list[dict[str, Any]]:
        principal.require("patent_family.read")
        self.families.get(family_id)
        return self.events.list(family_id)


class ApplicationService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.families = FamilyRepository(connection)
        self.applications = ApplicationRepository(connection)
        self.relations = RelationRepository(connection)
        self.changes = ChangeRepository(connection)
        self.events = FamilyEventRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def _ensure_family(self, family_code: str, family_title: str | None, now: str, actor_user_id: int | None) -> dict[str, Any]:
        code = _normalize_number(family_code)
        family = self.families.by_code(code)
        if family:
            return family
        family = self.families.create(code, family_title or code, now)
        self.events.append(family["id"], "family.created", actor_user_id, now, {"family_code": code})
        return family

    def _validate_application_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        values = dict(data)
        values["application_number"] = _normalize_number(values["application_number"])
        values["jurisdiction"] = _normalize_number(values["jurisdiction"])
        values["filing_date"] = _require_date(values["filing_date"], "申请日")
        if values.get("declared_priority_date"):
            values["declared_priority_date"] = _require_date(values["declared_priority_date"], "声明优先权日")
            if values["declared_priority_date"] > values["filing_date"]:
                raise ValidationError("声明优先权日不能晚于申请日")
        _check_secrecy(values["publication_status"], bool(values.get("is_secret_asset")))
        return values

    def register(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_family.write")
        values = self._validate_application_payload(data)
        existing = self.applications.by_number(values["application_number"])
        if existing:
            return {"application": existing, "family": self.families.get(existing["family_id"]), "replayed": True}
        now = to_storage(self.clock.now())
        family = self._ensure_family(values["family_code"], values.get("family_title"), now, principal.user_id)
        if family["status"] != "active":
            raise ConflictError("家族已合并，不能继续登记申请")
        application = self.applications.create(values, family["id"], now)
        self.events.append(
            family["id"], "application.registered", principal.user_id, now,
            {"application_id": application["id"], "application_number": application["application_number"]},
        )
        self.audit.record(principal, "patent_application.register", "patent_application", str(application["id"]), after=application)
        return {"application": application, "family": family, "replayed": False}

    def import_batch(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_family.write")
        now = to_storage(self.clock.now())
        family = self._ensure_family(data["family_code"], data.get("family_title"), now, principal.user_id)
        created: list[dict[str, Any]] = []
        replayed: list[dict[str, Any]] = []
        for item in data["applications"]:
            values = self._validate_application_payload(item)
            existing = self.applications.by_number(values["application_number"])
            if existing:
                # 重复导入保持原成员关系，不迁移到本次导入的家族
                replayed.append(existing)
                continue
            if family["status"] != "active":
                raise ConflictError("家族已合并，不能继续导入申请")
            application = self.applications.create(values, family["id"], now)
            created.append(application)
            self.events.append(
                family["id"], "application.imported", principal.user_id, now,
                {"application_id": application["id"], "application_number": application["application_number"]},
            )
        relation_changes: list[dict[str, Any]] = []
        skipped_relations: list[dict[str, Any]] = []
        change_service = FamilyChangeService(self.connection, self.clock)
        for item in data.get("relations", []):
            parent = self.applications.by_number(_normalize_number(item["parent_application_number"]))
            child = self.applications.by_number(_normalize_number(item["child_application_number"]))
            label = f"{item['parent_application_number']}->{item['child_application_number']}({item['relation_type']})"
            if not parent or not child:
                skipped_relations.append({"relation": label, "reason": "application_missing"})
                continue
            if parent["family_id"] != child["family_id"]:
                skipped_relations.append({"relation": label, "reason": "cross_family_requires_merge"})
                continue
            try:
                change = change_service.propose_link(
                    principal,
                    parent["family_id"],
                    {
                        "parent_application_id": parent["id"],
                        "child_application_id": child["id"],
                        "relation_type": item["relation_type"],
                    },
                )
            except (ValidationError, ConflictError) as exc:
                skipped_relations.append({"relation": label, "reason": exc.message})
                continue
            if change.pop("_replayed", False):
                skipped_relations.append({"relation": label, "reason": "duplicate_pending"})
            else:
                relation_changes.append(change)
        self.audit.record(
            principal, "patent_family.import", "patent_family", str(family["id"]),
            metadata={"created": len(created), "replayed": len(replayed), "relation_changes": len(relation_changes)},
        )
        return {
            "family": family,
            "created": created,
            "replayed": replayed,
            "relation_changes": relation_changes,
            "skipped_relations": skipped_relations,
        }

    def detail(self, principal: Principal, application_id: int) -> dict[str, Any]:
        principal.require("patent_family.read")
        application = self.applications.get(application_id)
        application["family"] = self.families.get(application["family_id"])
        return application

    def lineage(self, principal: Principal, application_id: int) -> dict[str, Any]:
        principal.require("patent_family.read")
        application = self.applications.get(application_id)
        family_id = application["family_id"]
        members, active = FamilyService(self.connection, self.clock)._graph_inputs(family_id)
        member_ids = {item["id"] for item in members}
        effective, source, via = effective_priority_dates(members, active)
        # 优先权继承链：从贡献来源申请沿 via 指针回到本申请
        chain_ids = [application_id]
        cursor = application_id
        while via[cursor] is not None:
            cursor = via[cursor]
            chain_ids.append(cursor)
        chain_ids.reverse()
        edge_by_pair = {
            (edge["parent_application_id"], edge["child_application_id"]): edge for edge in active
        }
        priority_chain = []
        for left, right in zip(chain_ids, chain_ids[1:]):
            edge = edge_by_pair[(left, right)]
            priority_chain.append(
                {
                    "relation_id": edge["id"],
                    "relation_type": edge["relation_type"],
                    "parent_application_id": left,
                    "child_application_id": right,
                }
            )
        parents = [edge for edge in active if edge["child_application_id"] == application_id]
        children = [edge for edge in active if edge["parent_application_id"] == application_id]
        descendants = reachable(application_id, active)
        reversed_edges = [
            {**edge, "parent_application_id": edge["child_application_id"], "child_application_id": edge["parent_application_id"]}
            for edge in active
        ]
        ancestors = reachable(application_id, reversed_edges)
        return {
            "application": application,
            "family_id": family_id,
            "effective_priority_date": effective[application_id],
            "priority_source_application_id": source[application_id],
            "priority_chain": priority_chain,
            "parents": parents,
            "children": children,
            "ancestors": sorted(ancestors & member_ids),
            "descendants": sorted(descendants & member_ids),
        }


class RelationQueryService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.families = FamilyRepository(connection)
        self.relations = RelationRepository(connection)
        self.applications = ApplicationRepository(connection)

    def impact(self, principal: Principal, relation_id: int) -> dict[str, Any]:
        """撤销影响查询：对生效中的关系给出预演，对已撤销的关系给出回溯。"""
        principal.require("patent_family.read")
        relation = self.relations.get(relation_id)
        members = self.applications.members(relation["family_id"])
        active = self.relations.family_relations(relation["family_id"], status="active")
        if relation["status"] == "active":
            with_edge = active
            without_edge = [edge for edge in active if edge["id"] != relation_id]
        else:
            with_edge = sorted([*active, relation], key=lambda item: item["id"])
            without_edge = active
        member_ids = [item["id"] for item in members]
        effective_with, _, _ = effective_priority_dates(members, with_edge)
        effective_without, _, _ = effective_priority_dates(members, without_edge)
        affected = [
            {
                "application_id": application_id,
                "priority_date_with_relation": effective_with[application_id],
                "priority_date_without_relation": effective_without[application_id],
            }
            for application_id in member_ids
            if effective_with[application_id] != effective_without[application_id]
        ]
        return {
            "relation": relation,
            "relation_status": relation["status"],
            "affected_applications": affected,
            "component_count_with_relation": connected_components(member_ids, with_edge),
            "component_count_without_relation": connected_components(member_ids, without_edge),
        }


class FamilyChangeService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.families = FamilyRepository(connection)
        self.applications = ApplicationRepository(connection)
        self.relations = RelationRepository(connection)
        self.changes = ChangeRepository(connection)
        self.events = FamilyEventRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ---- 关系类型校验 ----

    def _validate_link(
        self,
        parent: dict[str, Any],
        child: dict[str, Any],
        relation_type: str,
        active_edges: list[dict[str, Any]],
    ) -> None:
        if parent["id"] == child["id"]:
            raise ValidationError("关系的两件申请不能相同")
        label = RELATION_TYPE_LABELS.get(relation_type, relation_type)
        if relation_type in SAME_JURISDICTION_TYPES and parent["jurisdiction"] != child["jurisdiction"]:
            raise ValidationError(f"{label}要求两件申请属于同一国家或地区")
        if parent["filing_date"] > child["filing_date"]:
            raise ValidationError(f"{label}的在先申请申请日不能晚于在后申请")
        if parent["id"] in reachable(child["id"], active_edges):
            raise ConflictError("该关系会形成循环，禁止建立")

    def propose_link(self, principal: Principal, family_id: int, link: dict[str, Any]) -> dict[str, Any]:
        family = self.families.get(family_id)
        if family["status"] != "active":
            raise ConflictError("家族已合并，不能继续补录关系")
        parent = self.applications.get(link["parent_application_id"])
        child = self.applications.get(link["child_application_id"])
        if parent["family_id"] != family_id or child["family_id"] != family_id:
            raise ValidationError("关系的两件申请必须属于同一家族，跨家族请先合并")
        active = self.relations.family_relations(family_id, status="active")
        self._validate_link(parent, child, link["relation_type"], active)
        if self.relations.active_between(parent["id"], child["id"], link["relation_type"]):
            raise ConflictError("相同的生效关系已经存在")
        pending = self.changes.pending_link(family_id, parent["id"], child["id"], link["relation_type"])
        if pending:
            pending["_replayed"] = True
            return pending
        now = to_storage(self.clock.now())
        change = self.changes.create(
            f"PFC-{uuid.uuid4().hex[:12]}", family_id, "link",
            {
                "parent_application_id": parent["id"],
                "child_application_id": child["id"],
                "relation_type": link["relation_type"],
            },
            principal.user_id, now,
        )
        self.events.append(family_id, "change.requested", principal.user_id, now, {"change_id": change["id"], "change_type": "link"})
        self.audit.record(principal, "patent_family_change.request", "patent_family_change", str(change["id"]), after=change)
        return change

    def _propose_revoke(self, principal: Principal, family_id: int, relation_id: int, reason: str) -> dict[str, Any]:
        relation = self.relations.get(relation_id)
        if relation["family_id"] != family_id:
            raise ValidationError("关系不属于指定家族")
        if relation["status"] != "active":
            raise ConflictError("关系已经撤销")
        now = to_storage(self.clock.now())
        change = self.changes.create(
            f"PFC-{uuid.uuid4().hex[:12]}", family_id, "revoke",
            {"relation_id": relation_id, "reason": reason},
            principal.user_id, now,
        )
        self.events.append(family_id, "change.requested", principal.user_id, now, {"change_id": change["id"], "change_type": "revoke"})
        self.audit.record(principal, "patent_family_change.request", "patent_family_change", str(change["id"]), after=change)
        return change

    # ---- 合并冲突预演 ----

    def rehearse_merge(
        self,
        principal: Principal,
        source_family_id: int,
        target_family_id: int,
        link: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        principal.require("patent_family.read")
        return self._rehearse(source_family_id, target_family_id, link)

    def _rehearse(
        self,
        source_family_id: int,
        target_family_id: int,
        link: dict[str, Any] | None,
        exclude_change_id: int | None = None,
    ) -> dict[str, Any]:
        conflicts: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        if source_family_id == target_family_id:
            conflicts.append({"code": "same_family", "message": "来源家族与目标家族相同"})
        source = self.families.get(source_family_id)
        target = self.families.get(target_family_id)
        if source["status"] != "active":
            conflicts.append({"code": "source_not_active", "message": "来源家族已合并"})
        if target["status"] != "active":
            conflicts.append({"code": "target_not_active", "message": "目标家族已合并"})
        for family_id, label in ((source_family_id, "来源"), (target_family_id, "目标")):
            pending = [
                item for item in self.changes.pending_for_family(family_id)
                if item["id"] != exclude_change_id
            ]
            if pending:
                conflicts.append(
                    {"code": "pending_changes", "message": f"{label}家族存在 {len(pending)} 条未生效变更，需先处理"}
                )
        source_members = self.applications.members(source_family_id)
        target_members = self.applications.members(target_family_id)
        source_secret = [item for item in source_members if item["is_secret_asset"] and item["publication_status"] == "unpublished"]
        target_secret = [item for item in target_members if item["is_secret_asset"] and item["publication_status"] == "unpublished"]
        source_published = [item for item in source_members if item["publication_status"] == "published"]
        target_published = [item for item in target_members if item["publication_status"] == "published"]
        if (source_secret and target_published) or (target_secret and source_published):
            warnings.append(
                {
                    "code": "secrecy_exposure",
                    "message": "合并后家族同时包含未公开秘密资产与已公开申请，请复核秘密资产标记，避免误把已公开技术当作秘密资产",
                }
            )
        combined_edges = (
            self.relations.family_relations(source_family_id, status="active")
            + self.relations.family_relations(target_family_id, status="active")
        )
        if link:
            parent = self.applications.get(link["parent_application_id"])
            child = self.applications.get(link["child_application_id"])
            endpoint_families = {parent["family_id"], child["family_id"]}
            if endpoint_families != {source_family_id, target_family_id}:
                conflicts.append({"code": "link_endpoint_family", "message": "合并连接关系的两件申请必须分别属于来源与目标家族"})
            else:
                try:
                    self._validate_link(parent, child, link["relation_type"], combined_edges)
                except (ValidationError, ConflictError) as exc:
                    conflicts.append({"code": "link_invalid", "message": exc.message})
                if self.relations.active_between(parent["id"], child["id"], link["relation_type"]):
                    conflicts.append({"code": "link_duplicate", "message": "合并连接关系与现有生效关系重复"})
        return {
            "source_family_id": source_family_id,
            "target_family_id": target_family_id,
            "conflicts": conflicts,
            "warnings": warnings,
            "members_to_move": len(source_members),
            "relations_to_move": len(self.relations.family_relations(source_family_id, status="active")),
            "resulting_member_count": len(source_members) + len(target_members),
            "mergeable": not conflicts,
        }

    def _propose_merge(self, principal: Principal, family_id: int, source_family_id: int, merge_link: dict[str, Any] | None) -> dict[str, Any]:
        rehearsal = self._rehearse(source_family_id, family_id, merge_link)
        if rehearsal["conflicts"]:
            raise ConflictError("合并预演发现冲突", context={"conflicts": rehearsal["conflicts"]})
        now = to_storage(self.clock.now())
        payload: dict[str, Any] = {"source_family_id": source_family_id}
        if merge_link:
            payload["link"] = merge_link
        change = self.changes.create(f"PFC-{uuid.uuid4().hex[:12]}", family_id, "merge", payload, principal.user_id, now)
        self.events.append(family_id, "change.requested", principal.user_id, now, {"change_id": change["id"], "change_type": "merge"})
        self.audit.record(principal, "patent_family_change.request", "patent_family_change", str(change["id"]), after=change)
        return change

    # ---- 变更审批流转 ----

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_family.write")
        change_type = data["change_type"]
        if change_type == "link":
            change = self.propose_link(principal, data["family_id"], data["link"])
            change.pop("_replayed", None)
            return change
        if change_type == "revoke":
            return self._propose_revoke(principal, data["family_id"], data["relation_id"], data["reason"])
        return self._propose_merge(principal, data["family_id"], data["source_family_id"], data.get("merge_link"))

    def list(self, principal: Principal, state: str | None, family_id: int | None) -> list[dict[str, Any]]:
        principal.require("patent_family.read")
        return self.changes.list(state=state, family_id=family_id)

    def detail(self, principal: Principal, change_id: int) -> dict[str, Any]:
        principal.require("patent_family.read")
        return self.changes.get(change_id)

    def decide(self, principal: Principal, change_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_family.review")
        change = self.changes.get(change_id)
        if change["state"] != "pending":
            raise ConflictError("变更不在待审核状态")
        if change["requested_by"] == principal.user_id:
            raise ValidationError("申请人不能审核自己的变更")
        now = to_storage(self.clock.now())
        state = "approved" if data["decision"] == "approve" else "rejected"
        result = self.changes.transition(
            change_id, ("pending",), state, now,
            decided_by=principal.user_id, decided_at=now, decision_comment=data.get("comment", ""),
        )
        self.events.append(
            result["family_id"], "change.decided", principal.user_id, now,
            {"change_id": change_id, "decision": data["decision"]},
        )
        self.audit.record(principal, "patent_family_change.decide", "patent_family_change", str(change_id), before=change, after=result)
        return result

    def cancel(self, principal: Principal, change_id: int) -> dict[str, Any]:
        principal.require("patent_family.write")
        change = self.changes.get(change_id)
        if change["requested_by"] != principal.user_id and not principal.can("patent_family.review"):
            raise ValidationError("只有申请人或审核人可以撤回变更")
        now = to_storage(self.clock.now())
        result = self.changes.transition(change_id, ("pending",), "cancelled", now)
        self.audit.record(principal, "patent_family_change.cancel", "patent_family_change", str(change_id), before=change, after=result)
        return result

    def apply(self, principal: Principal, change_id: int) -> dict[str, Any]:
        principal.require("patent_family.write")
        change = self.changes.get(change_id)
        if change["state"] == "applied":
            raise ConflictError("变更已经生效")
        if change["state"] != "approved":
            raise ConflictError("变更尚未通过审核，不能生效")
        now = to_storage(self.clock.now())
        if change["change_type"] == "link":
            outcome = self._apply_link(change, principal, now)
        elif change["change_type"] == "revoke":
            outcome = self._apply_revoke(change, principal, now)
        else:
            outcome = self._apply_merge(change, principal, now)
        result = self.changes.transition(change_id, ("approved",), "applied", now, applied_at=now)
        self.audit.record(
            principal, "patent_family_change.apply", "patent_family_change", str(change_id),
            before=change, after=result, metadata={"change_type": change["change_type"]},
        )
        return {"change": result, "outcome": outcome}

    def _apply_link(self, change: dict[str, Any], principal: Principal, now: str) -> dict[str, Any]:
        payload = change["payload"]
        family_id = change["family_id"]
        family = self.families.get(family_id)
        if family["status"] != "active":
            raise ConflictError("家族已合并，不能继续补录关系")
        parent = self.applications.get(payload["parent_application_id"])
        child = self.applications.get(payload["child_application_id"])
        if parent["family_id"] != family_id or child["family_id"] != family_id:
            raise ConflictError("申请所属家族已变化，请重新提交变更")
        active = self.relations.family_relations(family_id, status="active")
        self._validate_link(parent, child, payload["relation_type"], active)
        if self.relations.active_between(parent["id"], child["id"], payload["relation_type"]):
            raise ConflictError("相同的生效关系已经存在")
        relation = self.relations.create(
            family_id, parent["id"], child["id"], payload["relation_type"], change["id"], now
        )
        self.events.append(
            family_id, "relation.linked", principal.user_id, now,
            {
                "relation_id": relation["id"], "change_id": change["id"],
                "parent_application_id": parent["id"], "child_application_id": child["id"],
                "relation_type": payload["relation_type"],
            },
        )
        return {"relation": relation}

    def _apply_revoke(self, change: dict[str, Any], principal: Principal, now: str) -> dict[str, Any]:
        payload = change["payload"]
        relation = self.relations.get(payload["relation_id"])
        if relation["family_id"] != change["family_id"]:
            raise ConflictError("关系所属家族已变化，请重新提交变更")
        revoked = self.relations.revoke(relation["id"], change["id"], payload["reason"], now)
        self.events.append(
            change["family_id"], "relation.revoked", principal.user_id, now,
            {"relation_id": relation["id"], "change_id": change["id"], "reason": payload["reason"]},
        )
        return {"relation": revoked}

    def _apply_merge(self, change: dict[str, Any], principal: Principal, now: str) -> dict[str, Any]:
        payload = change["payload"]
        target_family_id = change["family_id"]
        source_family_id = payload["source_family_id"]
        link = payload.get("link")
        rehearsal = self._rehearse(source_family_id, target_family_id, link, exclude_change_id=change["id"])
        if rehearsal["conflicts"]:
            raise ConflictError("合并预演发现冲突", context={"conflicts": rehearsal["conflicts"]})
        self.families.move_members(source_family_id, target_family_id, now)
        merged = self.families.mark_merged(source_family_id, target_family_id, now)
        relation = None
        if link:
            relation = self.relations.create(
                target_family_id, link["parent_application_id"], link["child_application_id"],
                link["relation_type"], change["id"], now,
            )
        self.events.append(
            source_family_id, "family.merged_out", principal.user_id, now,
            {"change_id": change["id"], "target_family_id": target_family_id},
        )
        self.events.append(
            target_family_id, "family.merged_in", principal.user_id, now,
            {
                "change_id": change["id"], "source_family_id": source_family_id,
                "members_moved": rehearsal["members_to_move"],
                "link_relation_id": relation["id"] if relation else None,
            },
        )
        return {"source_family": merged, "target_family": self.families.get(target_family_id), "link_relation": relation}
