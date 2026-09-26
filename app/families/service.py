from __future__ import annotations

import sqlite3
import uuid
from collections import defaultdict, deque
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, DomainError, NotFoundError, ValidationError
from app.core.security import Principal
from app.families import topology
from app.families.repository import (
    FamilyChangeRepository,
    FamilyEventRepository,
    FamilyLinkRepository,
    PatentApplicationRepository,
)
from app.services.audit import AuditService

SAME_JURISDICTION_TYPES = {"continuation", "continuation_in_part", "divisional"}


class PatentFamilyService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.applications = PatentApplicationRepository(connection)
        self.links = FamilyLinkRepository(connection)
        self.changes = FamilyChangeRepository(connection)
        self.events = FamilyEventRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 导入

    def import_application(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_families.write")
        existing = self.applications.find(data["jurisdiction"], data["application_number"])
        if existing:
            return {"application": existing, "replayed": True}
        self._validate_dates(data)
        self._validate_secret_status(data.get("application_status", "filed"), bool(data.get("secret_asset")))
        now = to_storage(self.clock.now())
        application = self.applications.create(data, principal.user_id, now)
        self.events.append(
            event_type="application.imported",
            family_id=application["family_id"],
            application_id=application["id"],
            actor_user_id=principal.user_id,
            details={"jurisdiction": application["jurisdiction"], "application_number": application["application_number"]},
            now=now,
        )
        self.audit.record(
            principal, "patent_family.application.import", "patent_application", str(application["id"]),
            after=application,
        )
        return {"application": application, "replayed": False}

    def patch_application(self, principal: Principal, application_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_families.write")
        before = self.applications.get(application_id)
        updatable = {
            "title", "application_status", "filing_date", "publication_date",
            "grant_date", "priority_claim_date", "secret_asset",
        }
        fields = {key: value for key, value in data.items() if key in updatable}
        if not fields:
            raise ValidationError("没有可更新的字段")
        if before["application_status"] == "merged" and fields.get("application_status") not in {None, "merged"}:
            raise ConflictError("已合并成员不能改回其他状态")
        if "secret_asset" in fields:
            fields["secret_asset"] = 1 if fields["secret_asset"] else 0
        merged_data = {
            key: before[key]
            for key in ("filing_date", "publication_date", "grant_date", "priority_claim_date")
        }
        merged_data.update({key: value for key, value in fields.items() if key.endswith("_date")})
        self._validate_dates(merged_data)
        new_status = fields.get("application_status", before["application_status"])
        self._validate_secret_status(
            new_status,
            bool(fields.get("secret_asset", before["secret_asset"])),
        )
        now = to_storage(self.clock.now())
        assignments = ", ".join(f"{key}=?" for key in fields)
        cursor = self.connection.execute(
            f"""UPDATE patent_applications SET {assignments},version=version+1,updated_at=?
                WHERE id=? AND version=?""",
            (*fields.values(), now, application_id, before["version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("申请记录已被他人修改，请刷新后重试")
        after = self.applications.get(application_id)
        self.events.append(
            event_type="application.updated",
            family_id=after["family_id"],
            application_id=application_id,
            actor_user_id=principal.user_id,
            details={"changed_fields": sorted(fields)},
            now=now,
        )
        self.audit.record(principal, "patent_family.application.update", "patent_application", str(application_id), before=before, after=after)
        return after

    @staticmethod
    def _validate_dates(data: dict[str, Any]) -> None:
        filing = data.get("filing_date")
        publication = data.get("publication_date")
        grant = data.get("grant_date")
        priority = data.get("priority_claim_date")
        if filing and publication and publication < filing:
            raise ValidationError("公开日期不能早于申请日")
        if filing and grant and grant < filing:
            raise ValidationError("授权日期不能早于申请日")
        if filing and priority and priority > filing:
            raise ValidationError("优先权日不能晚于申请日")

    @staticmethod
    def _validate_secret_status(application_status: str, secret_asset: bool) -> None:
        # 已公开/授权的申请不再是技术秘密，防止公开申请被误登为秘密资产。
        if secret_asset and application_status in {"published", "granted"}:
            raise ValidationError("已公开或授权的申请不能标记为技术秘密资产")

    # ------------------------------------------------------------ 关系提案

    def request_link(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_families.write")
        child = self.applications.get(data["child_application_id"])
        parent = self.applications.get(data["parent_application_id"])
        self._ensure_active_member(child, "子申请")
        self._ensure_active_member(parent, "父申请")
        relation_type = data["relation_type"]
        if relation_type in SAME_JURISDICTION_TYPES and child["jurisdiction"] != parent["jurisdiction"]:
            raise ValidationError(f"{relation_type} 关系要求两端属于同一司法辖区")
        if relation_type == "national_phase" and child["jurisdiction"] == parent["jurisdiction"]:
            raise ValidationError("national_phase 关系要求两端属于不同司法辖区")
        if relation_type == "divisional" and parent["filing_date"] > child["filing_date"]:
            raise ValidationError("分案申请的申请日不能早于母案申请日")
        claimed = data.get("claimed_priority_date")
        if claimed and claimed > child["filing_date"]:
            raise ValidationError("主张的优先权日不能晚于子申请的申请日")
        if self.links.find_active(child["id"], parent["id"]):
            raise ConflictError("这两份申请之间已存在活跃关系")
        nodes, links = self._family_snapshot({child["id"], parent["id"]})
        cycle = topology.would_create_cycle(nodes, links, child["id"], parent["id"])
        if cycle:
            raise ConflictError("该关系会形成循环引用", context={"cycle": cycle})
        preview = self._link_preview(nodes, links, child, parent, data)
        return self._create_change(
            principal,
            "link_relation",
            {**data, "preview": preview},
            idempotency_key=data.get("idempotency_key"),
            change_code=data.get("change_code"),
        )

    def _link_preview(
        self,
        nodes: list[dict[str, Any]],
        links: list[dict[str, Any]],
        child: dict[str, Any],
        parent: dict[str, Any],
        data: dict[str, Any],
    ) -> dict[str, Any]:
        candidate = [
            *links,
            {
                "child_application_id": child["id"],
                "parent_application_id": parent["id"],
                "relation_type": data["relation_type"],
                "claimed_priority_date": data.get("claimed_priority_date"),
                "status": "active",
            },
        ]
        before_dates = topology.effective_priority_dates(nodes, links)
        after_dates = topology.effective_priority_dates(nodes, candidate)
        affected = [
            {"application_id": node_id, "before": before_dates.get(node_id), "after": after_dates.get(node_id)}
            for node_id in after_dates
            if before_dates.get(node_id) != after_dates.get(node_id)
        ]
        order = topology.topological_order(nodes, candidate)
        return {
            "kind": "link_relation",
            "would_join_families": child["family_id"] != parent["family_id"],
            "effective_priority_changes": sorted(affected, key=lambda item: item["application_id"]),
            "topological_order": order,
        }

    # ------------------------------------------------------------ 合并提案

    def request_merge(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_families.write")
        target, sources = self._load_merge_parties(data["target_application_id"], data["source_application_ids"])
        preview = self.merge_preview(target["id"], [item["id"] for item in sources])
        blocking = [item for item in preview["conflicts"] if item["severity"] == "blocking"]
        if blocking:
            raise ConflictError("成员合并存在阻断性冲突，已生成预演但不能提交", context={"preview": preview, "conflicts": blocking})
        payload = {
            "target_application_id": target["id"],
            "source_application_ids": [item["id"] for item in sources],
            "reason": data["reason"],
            "preview": preview,
        }
        return self._create_change(
            principal, "merge_members", payload,
            idempotency_key=data.get("idempotency_key"), change_code=data.get("change_code"),
        )

    def preview_merge(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_families.read")
        target, sources = self._load_merge_parties(data["target_application_id"], data["source_application_ids"])
        return self.merge_preview(target["id"], [item["id"] for item in sources])

    def _load_merge_parties(
        self, target_id: int, source_ids: list[int]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if len(set(source_ids)) != len(source_ids):
            raise ValidationError("待合并成员存在重复")
        sources = self.applications.get_many(source_ids)
        if len(sources) != len(set(source_ids)):
            raise ValidationError("部分待合并成员不存在")
        target = self.applications.get(target_id)
        for label, member in (("保留成员", target), *[("被合并成员", item) for item in sources]):
            self._ensure_active_member(member, label)
        return target, sources

    def merge_preview(self, target_id: int, source_ids: list[int]) -> dict[str, Any]:
        target = self.applications.get(target_id)
        sources = self.applications.get_many(source_ids)
        conflicts: list[dict[str, Any]] = []
        for source in sources:
            if source["secret_asset"] and not target["secret_asset"]:
                conflicts.append({
                    "severity": "blocking",
                    "code": "secret_into_public",
                    "message": "技术秘密成员并入未标记秘密资产的保留成员，可能导致秘密属性丢失",
                    "source_application_id": source["id"],
                    "target_application_id": target_id,
                })
            if target["secret_asset"] and not source["secret_asset"] and source["application_status"] == "published":
                conflicts.append({
                    "severity": "warning",
                    "code": "public_into_secret",
                    "message": "已公开成员并入秘密资产成员，请确认公开状态登记无误",
                    "source_application_id": source["id"],
                    "target_application_id": target_id,
                })
            for field, label in (("priority_claim_date", "优先权日"), ("filing_date", "申请日")):
                source_value = source.get(field)
                target_value = target.get(field)
                if source_value and target_value and source_value != target_value:
                    conflicts.append({
                        "severity": "warning",
                        "code": f"{field}_mismatch",
                        "message": f"{label}不一致，合并后以保留成员为准",
                        "source_application_id": source["id"],
                        "source_value": source_value,
                        "target_value": target_value,
                    })
        source_set = set(source_ids)
        links = sorted(
            self.links.list_for_applications({target_id, *source_ids}),
            key=lambda item: item["id"],
        )
        # 按 id 顺序在内存中模拟执行，保证预演与真正执行的归并/去重判断一致。
        planned_pairs: set[tuple[int, int]] = set()
        collapsed: list[dict[str, Any]] = []
        for link in links:
            if link["status"] != "active":
                continue
            involves_child = link["child_application_id"] in source_set
            involves_parent = link["parent_application_id"] in source_set
            if not involves_child and not involves_parent:
                continue
            new_child = target_id if involves_child else link["child_application_id"]
            new_parent = target_id if involves_parent else link["parent_application_id"]
            endpoints = self._link_endpoints(link)
            if new_child == new_parent:
                collapsed.append({"link_id": link["id"], "effect": "edge_collapsed", **endpoints})
                continue
            pair = (new_child, new_parent)
            existing = self.links.find_active(new_child, new_parent)
            if existing is not None:
                collapsed.append({
                    "link_id": link["id"], "effect": "duplicate_superseded",
                    "kept_link_id": existing["id"], "within_batch": False, **endpoints,
                })
            elif pair in planned_pairs:
                collapsed.append({
                    "link_id": link["id"], "effect": "duplicate_superseded",
                    "kept_link_id": None, "within_batch": True, **endpoints,
                })
            else:
                planned_pairs.add(pair)
                collapsed.append({
                    "link_id": link["id"], "effect": "rewired_to_target",
                    "new_child_application_id": new_child,
                    "new_parent_application_id": new_parent,
                    **endpoints,
                })
        return {
            "kind": "merge_members",
            "target_application_id": target_id,
            "source_application_ids": sorted(source_ids),
            "conflicts": conflicts,
            "link_impacts": sorted(collapsed, key=lambda item: item["link_id"]),
            "effective_priority_date_after": min(
                (value for value in [target.get("priority_claim_date") or target.get("filing_date"),
                                    *[(s.get("priority_claim_date") or s.get("filing_date")) for s in sources]] if value),
                default=None,
            ),
        }

    @staticmethod
    def _link_endpoints(link: dict[str, Any]) -> dict[str, Any]:
        return {
            "child_application_id": link["child_application_id"],
            "parent_application_id": link["parent_application_id"],
            "relation_type": link["relation_type"],
        }

    # ------------------------------------------------------------ 撤销提案

    def request_revoke(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_families.write")
        link = self.links.get(data["link_id"])
        if link["status"] != "active":
            raise ConflictError("只能撤销当前活跃的关系")
        impact = self.revoke_impact(principal, data["link_id"])
        payload = {"link_id": link["id"], "reason": data["reason"], "preview": impact}
        return self._create_change(
            principal, "revoke_relation", payload,
            idempotency_key=data.get("idempotency_key"), change_code=data.get("change_code"),
        )

    def revoke_impact(self, principal: Principal, link_id: int) -> dict[str, Any]:
        principal.require("patent_families.read")
        link = self.links.get(link_id)
        members = self._component_members({link["child_application_id"], link["parent_application_id"]})
        nodes = self.applications.get_many(members)
        all_links = self.links.list_for_applications(members)
        before_dates = topology.effective_priority_dates(nodes, [l for l in all_links if l["status"] == "active"])
        remaining = [
            l for l in all_links
            if l["status"] == "active" and l["id"] != link_id
        ]
        after_dates = topology.effective_priority_dates(nodes, remaining)
        date_changes = [
            {"application_id": node_id, "before": before_dates.get(node_id), "after": after_dates.get(node_id)}
            for node_id in after_dates
            if before_dates.get(node_id) != after_dates.get(node_id)
        ]
        components_before = self._components(nodes, [l for l in all_links if l["status"] == "active"])
        components_after = self._components(nodes, remaining)
        return {
            "kind": "revoke_relation",
            "link_id": link_id,
            "active": link["status"] == "active",
            "priority_date_changes": sorted(date_changes, key=lambda item: item["application_id"]),
            "family_would_split": len(components_after) > len(components_before),
            "components_after": sorted((sorted(component) for component in components_after), key=lambda item: item[0]),
        }

    # ------------------------------------------------------------ 审批执行

    def decide(self, principal: Principal, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("patent_families.approve")
        request = self.changes.get(request_id)
        if request["status"] != "pending":
            raise ConflictError("该变更提案已经结束")
        if request["requested_by"] == principal.user_id:
            raise ValidationError("申请人不能审批自己提交的家族变更")
        now = to_storage(self.clock.now())
        try:
            self.changes.add_decision(request_id, principal.user_id, data["decision"], data.get("comment", ""), now)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一审批人不能重复表决") from exc
        if data["decision"] == "reject":
            self.changes.set_status(request_id, "rejected", now)
            self.audit.record(principal, "patent_family.change.reject", "patent_family_change", str(request_id), after=request)
            return {"request": self.changes.get(request_id)}
        decisions = self.changes.get(request_id)["decisions"]
        if len(decisions) < request["required_approvals"]:
            self.audit.record(principal, "patent_family.change.approve", "patent_family_change", str(request_id), metadata={"approvals": len(decisions)})
            return {"request": self.changes.get(request_id)}
        self.changes.set_status(request_id, "approved", now)
        self.connection.execute("SAVEPOINT family_change_execute")
        try:
            outcome = self._execute(request_id, principal)
        except DomainError as exc:
            # 执行期校验失败：回滚执行段的所有写入，但保留审批决定与 failed 留痕。
            self.connection.execute("ROLLBACK TO SAVEPOINT family_change_execute")
            self.connection.execute("RELEASE SAVEPOINT family_change_execute")
            message = getattr(exc, "message", str(exc))
            context = getattr(exc, "context", {})
            self.changes.set_status(request_id, "failed", now, execution_error=message)
            failed_request = self.changes.get(request_id)
            self.audit.record(
                principal, "patent_family.change.execute_failed", "patent_family_change", str(request_id),
                outcome="failure", metadata={"error": message, "context": context},
            )
            return {"request": failed_request, "execution_error": message, "context": context}
        else:
            self.connection.execute("RELEASE SAVEPOINT family_change_execute")
        self.changes.set_status(request_id, "executed", now)
        self.audit.record(
            principal, "patent_family.change.execute", "patent_family_change", str(request_id),
            after={"request": self.changes.get(request_id), "outcome": outcome},
        )
        return {"request": self.changes.get(request_id), "outcome": outcome}

    def _execute(self, request_id: int, principal: Principal) -> dict[str, Any]:
        request = self.changes.get(request_id)
        now = to_storage(self.clock.now())
        if request["change_type"] == "link_relation":
            outcome = self._execute_link(request, principal, now)
        elif request["change_type"] == "merge_members":
            outcome = self._execute_merge(request, principal, now)
        else:
            outcome = self._execute_revoke(request, principal, now)
        self._recompute_families(now)
        return outcome

    def _execute_link(self, request: dict[str, Any], principal: Principal, now: str) -> dict[str, Any]:
        payload = request["payload"]
        child = self.applications.get(payload["child_application_id"])
        parent = self.applications.get(payload["parent_application_id"])
        self._ensure_active_member(child, "子申请")
        self._ensure_active_member(parent, "父申请")
        if self.links.find_active(child["id"], parent["id"]):
            raise ConflictError("这两份申请之间已存在活跃关系")
        nodes, links = self._family_snapshot({child["id"], parent["id"]})
        cycle = topology.would_create_cycle(nodes, links, child["id"], parent["id"])
        if cycle:
            raise ConflictError("审批期间家族结构已变化，新增关系会形成循环", context={"cycle": cycle})
        link = self.links.create(payload, principal.user_id, request["id"], now)
        self.events.append(
            event_type="relation.linked", family_id=child["family_id"], application_id=child["id"],
            link_id=link["id"], change_request_id=request["id"], actor_user_id=principal.user_id,
            details={"parent_application_id": parent["id"], "relation_type": link["relation_type"]}, now=now,
        )
        return {"link": link}

    def _execute_merge(self, request: dict[str, Any], principal: Principal, now: str) -> dict[str, Any]:
        payload = request["payload"]
        target_id = payload["target_application_id"]
        source_ids = list(payload["source_application_ids"])
        live_preview = self.merge_preview(target_id, source_ids)
        blocking = [item for item in live_preview["conflicts"] if item["severity"] == "blocking"]
        if blocking:
            raise ConflictError("审批期间数据发生变化，合并仍存在阻断性冲突", context={"conflicts": blocking})
        target = self.applications.get(target_id)
        links = sorted(self.links.list_for_applications({target_id, *source_ids}), key=lambda item: item["id"])
        source_set = set(source_ids)
        rewired: list[int] = []
        superseded: list[int] = []
        for link in links:
            if link["status"] != "active":
                continue
            involves_child = link["child_application_id"] in source_set
            involves_parent = link["parent_application_id"] in source_set
            if not involves_child and not involves_parent:
                continue
            new_child = target_id if involves_child else link["child_application_id"]
            new_parent = target_id if involves_parent else link["parent_application_id"]
            if new_child == new_parent:
                self.connection.execute(
                    "UPDATE patent_family_links SET status='superseded',superseded_by_link_id=NULL WHERE id=? AND status='active'",
                    (link["id"],),
                )
                superseded.append(link["id"])
                continue
            # 本批刚改写产生的新边同样立即可见，可一并去重。
            duplicate = self.links.find_active(new_child, new_parent)
            if duplicate:
                self.connection.execute(
                    "UPDATE patent_family_links SET status='superseded',superseded_by_link_id=? WHERE id=? AND status='active'",
                    (duplicate["id"], link["id"]),
                )
                superseded.append(link["id"])
                continue
            new_link = self.links.create(
                {
                    "child_application_id": new_child,
                    "parent_application_id": new_parent,
                    "relation_type": link["relation_type"],
                    "claimed_priority_date": link["claimed_priority_date"],
                },
                principal.user_id,
                request["id"],
                now,
            )
            self.connection.execute(
                "UPDATE patent_family_links SET status='superseded',superseded_by_link_id=? WHERE id=?",
                (new_link["id"], link["id"]),
            )
            rewired.append(new_link["id"])
        self.applications.mark_merged(source_ids, target_id, now)
        for source_id in sorted(source_set):
            self.events.append(
                event_type="member.merged", family_id=target["family_id"], application_id=source_id,
                change_request_id=request["id"], actor_user_id=principal.user_id,
                details={"target_application_id": target_id, "reason": payload.get("reason", "")}, now=now,
            )
        self.events.append(
            event_type="member.merge_completed", family_id=target["family_id"], application_id=target_id,
            change_request_id=request["id"], actor_user_id=principal.user_id,
            details={"source_application_ids": sorted(source_set), "rewired_link_ids": rewired, "superseded_link_ids": superseded}, now=now,
        )
        return {"target_application_id": target_id, "merged": sorted(source_set), "rewired_link_ids": rewired, "superseded_link_ids": superseded}

    def _execute_revoke(self, request: dict[str, Any], principal: Principal, now: str) -> dict[str, Any]:
        payload = request["payload"]
        link = self.links.get(payload["link_id"])
        if link["status"] != "active":
            raise ConflictError("关系在审批期间已被撤销或取代")
        revoked = self.links.revoke(link["id"], payload["reason"], principal.user_id, now)
        self.events.append(
            event_type="relation.revoked", application_id=link["child_application_id"],
            link_id=link["id"], change_request_id=request["id"], actor_user_id=principal.user_id,
            details={"reason": payload["reason"], "parent_application_id": link["parent_application_id"]}, now=now,
        )
        return {"link": revoked}

    # ------------------------------------------------------------ 查询视图

    def family_summary(self, principal: Principal, family_id: int) -> dict[str, Any]:
        principal.require("patent_families.read")
        members = self.applications.list_family(family_id)
        if not members:
            raise NotFoundError("专利家族不存在")
        links = [link for link in self.links.list_for_family(family_id)]
        active = [link for link in links if link["status"] == "active"]
        order = topology.topological_order(members, active)
        dates = topology.effective_priority_dates(members, active)
        index = {member["id"]: member for member in members}
        ordered_members = []
        status_counts: dict[str, int] = defaultdict(int)
        for node_id in order:
            member = dict(index[node_id])
            member["effective_priority_date"] = dates.get(node_id)
            ordered_members.append(member)
            status_counts[member["application_status"]] += 1
        anchor = min(member["id"] for member in members)
        return {
            "family_id": family_id,
            "anchor_application_id": anchor,
            "member_count": len(members),
            "secret_asset_count": sum(1 for m in members if m["secret_asset"]),
            "published_count": sum(1 for m in members if m["application_status"] == "published"),
            "status_counts": dict(sorted(status_counts.items())),
            "members": ordered_members,
            "active_links": sorted(active, key=lambda link: link["id"]),
            "excluded_links": sorted(
                (link for link in links if link["status"] != "active"),
                key=lambda link: link["id"],
            ),
            "topological_order": order,
        }

    def application_detail(self, principal: Principal, application_id: int) -> dict[str, Any]:
        principal.require("patent_families.read")
        application = self.applications.get(application_id)
        members = self.applications.list_family(application["family_id"])
        links = self.links.list_for_family(application["family_id"])
        active = [link for link in links if link["status"] == "active"]
        dates = topology.effective_priority_dates(members, active)
        related = [
            link for link in links
            if application_id in {link["child_application_id"], link["parent_application_id"]}
        ]
        return {
            "application": application,
            "effective_priority_date": dates.get(application_id),
            "family_id": application["family_id"],
            "relations": sorted(related, key=lambda link: link["id"]),
            "family_topological_order": topology.topological_order(members, active),
            "events": self.events.list_for_application(application_id),
        }

    def change_detail(self, principal: Principal, request_id: int) -> dict[str, Any]:
        principal.require("patent_families.read")
        return self.changes.get(request_id)

    def list_pending_changes(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("patent_families.read")
        return self.changes.list_pending()

    def family_timeline(self, principal: Principal, family_id: int) -> dict[str, Any]:
        principal.require("patent_families.read")
        members = self.applications.list_family(family_id)
        if not members:
            raise NotFoundError("专利家族不存在")
        ids = [member["id"] for member in members]
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"""SELECT * FROM patent_family_events
                WHERE family_id=? OR application_id IN ({placeholders})
                ORDER BY id""",
            (family_id, *ids),
        ).fetchall()
        events = FamilyEventRepository._decode(rows)
        return {"family_id": family_id, "audit_order": [event["id"] for event in events], "events": events}

    # ------------------------------------------------------------ 内部工具

    def _create_change(
        self,
        principal: Principal,
        change_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None,
        change_code: str | None,
    ) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        if idempotency_key:
            pending = self.changes.find_pending_by_idempotency_key(idempotency_key)
            if pending:
                return {"request": pending, "replayed": True}
        code = change_code or f"FMC-{uuid.uuid4().hex[:12]}"
        request = self.changes.create(
            change_type=change_type,
            payload=payload,
            requested_by=principal.user_id,
            required_approvals=2,
            now=now,
            change_code=code,
            idempotency_key=idempotency_key,
        )
        self.events.append(
            event_type="change.requested",
            application_id=payload.get("child_application_id") or payload.get("target_application_id"),
            link_id=payload.get("link_id"),
            change_request_id=request["id"],
            actor_user_id=principal.user_id,
            details={"change_type": change_type, "change_code": code},
            now=now,
        )
        self.audit.record(principal, f"patent_family.change.request.{change_type}", "patent_family_change", str(request["id"]), after=request)
        return {"request": request, "replayed": False}

    @staticmethod
    def _ensure_active_member(member: dict[str, Any], label: str) -> None:
        if member["application_status"] == "merged":
            raise ConflictError(f"{label}已被合并，不能再建立或变更关系")

    def _family_snapshot(self, seed_ids: set[int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        member_ids = self._component_members(seed_ids)
        nodes = self.applications.get_many(member_ids)
        links = [
            link for link in self.links.list_for_applications(member_ids)
            if link["status"] == "active"
        ]
        return nodes, links

    def _component_members(self, seed_ids: set[int]) -> set[int]:
        rows = self.connection.execute(
            """SELECT child_application_id,parent_application_id FROM patent_family_links
               WHERE status='active'"""
        ).fetchall()
        adjacency: dict[int, set[int]] = defaultdict(set)
        for child, parent in ((row[0], row[1]) for row in rows):
            adjacency[child].add(parent)
            adjacency[parent].add(child)
        seen: set[int] = set()
        queue = deque(seed_ids)
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(adjacency.get(current, set()) - seen)
        return seen

    def _components(self, nodes: list[dict[str, Any]], active_links: list[dict[str, Any]]) -> list[set[int]]:
        adjacency: dict[int, set[int]] = defaultdict(set)
        for link in active_links:
            adjacency[link["child_application_id"]].add(link["parent_application_id"])
            adjacency[link["parent_application_id"]].add(link["child_application_id"])
        remaining = {node["id"] for node in nodes}
        components: list[set[int]] = []
        while remaining:
            seed = min(remaining)
            seen: set[int] = set()
            queue = deque([seed])
            while queue:
                current = queue.popleft()
                if current in seen:
                    continue
                seen.add(current)
                queue.extend(adjacency.get(current, set()) - seen)
            components.append(seen)
            remaining -= seen
        return components

    def _recompute_families(self, now: str) -> None:
        """按活跃边的无向连通分量重算 family_id，取分量内最小申请 id。

        已合并成员没有活跃边，跟随其 merged_into_id 所属分量，
        避免撤销/合并后家族编号发生漂移。
        """
        nodes = self.applications.list_all()
        node_ids = [node["id"] for node in nodes]
        links = [
            link for link in self.links.list_for_applications(node_ids)
            if link["status"] == "active"
        ]
        components = self._components(nodes, links)
        anchor_of: dict[int, int] = {}
        for component in components:
            anchor = min(component)
            for node_id in component:
                anchor_of[node_id] = anchor
        # 已合并成员可能本身孤立，补算其跟随目标的锚点（支持链式跟随）。
        merged = {node["id"]: node["merged_into_id"] for node in nodes if node["application_status"] == "merged"}
        for node_id in sorted(merged):
            seen: set[int] = set()
            current = node_id
            while current in merged and current not in seen:
                seen.add(current)
                current = merged[current]
            anchor_of[node_id] = anchor_of.get(current, current)
        grouped: dict[int, list[int]] = defaultdict(list)
        for node_id, anchor in anchor_of.items():
            grouped[anchor].append(node_id)
        for anchor, member_ids in grouped.items():
            self.applications.assign_family(member_ids, anchor, now)
