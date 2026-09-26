from __future__ import annotations

import pytest


def _login(client, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password, "client_label": "tests"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def approvers(client, admin):
    users = []
    for index in (1, 2):
        created = client.post(
            "/api/users",
            headers=admin["headers"],
            json={
                "username": f"approver{index}",
                "password": f"Approver!234{index}",
                "display_name": f"家族审批人{index}",
                "role_codes": ["approver"],
            },
        )
        assert created.status_code == 201, created.text
        users.append(_login(client, f"approver{index}", f"Approver!234{index}"))
    return users


def _import_app(client, headers, *, number: str, jurisdiction: str = "CN", **overrides):
    payload = {
        "jurisdiction": jurisdiction,
        "application_number": number,
        "title": f"申请 {number}",
        "filing_date": "2025-01-10",
        "priority_claim_date": None,
    }
    payload.update(overrides)
    response = client.post("/api/patent-families/applications", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["application"]


def _two_approvals(client, approvers, request_id: int):
    first = client.post(
        f"/api/patent-families/changes/{request_id}/decisions",
        headers=approvers[0],
        json={"decision": "approve", "comment": "同意"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["request"]["status"] == "pending"
    second = client.post(
        f"/api/patent-families/changes/{request_id}/decisions",
        headers=approvers[1],
        json={"decision": "approve", "comment": "同意"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["request"]["status"] == "executed"
    return second.json()


def test_duplicate_import_keeps_original_member(client, admin):
    first = _import_app(client, admin["headers"], number="202510001")
    again = _import_app(client, admin["headers"], number="202510001", title="其他标题")
    assert again["id"] == first["id"]
    # 重复导入不应覆盖原成员的标题。
    detail = client.get(f"/api/patent-families/applications/{first['id']}", headers=admin["headers"])
    assert detail.json()["application"]["title"] == "申请 202510001"


def test_published_application_cannot_be_secret_asset(client, admin):
    response = client.post(
        "/api/patent-families/applications",
        headers=admin["headers"],
        json={
            "jurisdiction": "US",
            "application_number": "18/001",
            "title": "已公开件",
            "filing_date": "2025-02-01",
            "application_status": "published",
            "publication_date": "2025-08-01",
            "secret_asset": True,
        },
    )
    assert response.status_code == 422
    # 未公开件允许标记为秘密资产。
    secret = _import_app(
        client, admin["headers"], number="18/002", jurisdiction="US",
        application_status="filed", secret_asset=True,
    )
    assert secret["secret_asset"] == 1


def test_relation_type_rules(client, admin, approvers):
    cn = _import_app(client, admin["headers"], number="CN-A")
    us = _import_app(client, admin["headers"], number="US-A", jurisdiction="US")
    later = _import_app(
        client, admin["headers"], number="CN-B",
        filing_date="2024-01-01",
    )
    # continuation 必须同辖区
    bad_jurisdiction = client.post(
        "/api/patent-families/changes/links",
        headers=admin["headers"],
        json={"child_application_id": us["id"], "parent_application_id": cn["id"], "relation_type": "continuation"},
    )
    assert bad_jurisdiction.status_code == 422
    # national_phase 必须跨辖区
    bad_national = client.post(
        "/api/patent-families/changes/links",
        headers=admin["headers"],
        json={"child_application_id": cn["id"], "parent_application_id": later["id"], "relation_type": "national_phase"},
    )
    assert bad_national.status_code == 422
    # 分案申请日不能早于母案
    bad_divisional = client.post(
        "/api/patent-families/changes/links",
        headers=admin["headers"],
        json={"child_application_id": later["id"], "parent_application_id": cn["id"], "relation_type": "divisional"},
    )
    assert bad_divisional.status_code == 422


def test_change_requires_two_distinct_approvers_and_requester_excluded(client, admin, approvers):
    parent = _import_app(client, admin["headers"], number="CN-P")
    child = _import_app(client, admin["headers"], number="CN-C")
    request = client.post(
        "/api/patent-families/changes/links",
        headers=admin["headers"],
        json={"child_application_id": child["id"], "parent_application_id": parent["id"], "relation_type": "continuation"},
    )
    assert request.status_code == 201
    request_id = request.json()["request"]["id"]
    # 申请人不能自审
    self_approval = client.post(
        f"/api/patent-families/changes/{request_id}/decisions",
        headers=admin["headers"],
        json={"decision": "approve"},
    )
    assert self_approval.status_code == 422
    # 单人审批不生效
    first = client.post(
        f"/api/patent-families/changes/{request_id}/decisions",
        headers=approvers[0],
        json={"decision": "approve"},
    )
    assert first.json()["request"]["status"] == "pending"
    # 同一审批人重复表决被拒
    duplicate = client.post(
        f"/api/patent-families/changes/{request_id}/decisions",
        headers=approvers[0],
        json={"decision": "approve"},
    )
    assert duplicate.status_code == 409
    # 第二名审批人通过后生效
    executed = _two_approvals_partial(client, approvers, request_id, first_already=True)
    assert executed["request"]["status"] == "executed"


def _two_approvals_partial(client, approvers, request_id: int, *, first_already: bool):
    if not first_already:
        client.post(
            f"/api/patent-families/changes/{request_id}/decisions",
            headers=approvers[0], json={"decision": "approve"},
        )
    second = client.post(
        f"/api/patent-families/changes/{request_id}/decisions",
        headers=approvers[1], json={"decision": "approve"},
    )
    assert second.status_code == 200, second.text
    return second.json()


def test_rejected_change_has_no_effect(client, admin, approvers):
    parent = _import_app(client, admin["headers"], number="CN-RP")
    child = _import_app(client, admin["headers"], number="CN-RC")
    request = client.post(
        "/api/patent-families/changes/links",
        headers=admin["headers"],
        json={"child_application_id": child["id"], "parent_application_id": parent["id"], "relation_type": "continuation"},
    )
    request_id = request.json()["request"]["id"]
    client.post(
        f"/api/patent-families/changes/{request_id}/decisions",
        headers=approvers[0], json={"decision": "reject", "comment": "资料不全"},
    )
    detail = client.get(f"/api/patent-families/changes/{request_id}", headers=admin["headers"]).json()
    assert detail["status"] == "rejected"
    family = client.get(f"/api/patent-families/{parent['family_id']}", headers=admin["headers"]).json()
    assert family["active_links"] == []


def test_cyclic_relation_is_rejected(client, admin, approvers):
    a = _import_app(client, admin["headers"], number="CYC-A")
    b = _import_app(client, admin["headers"], number="CYC-B")
    c = _import_app(client, admin["headers"], number="CYC-C")

    def link(child, parent, relation="priority"):
        return client.post(
            "/api/patent-families/changes/links",
            headers=admin["headers"],
            json={"child_application_id": child, "parent_application_id": parent, "relation_type": relation},
        )

    first = link(b["id"], a["id"])
    _two_approvals(client, approvers, first.json()["request"]["id"])
    second = link(c["id"], b["id"])
    _two_approvals(client, approvers, second.json()["request"]["id"])
    closing = link(a["id"], c["id"])
    assert closing.status_code == 409
    assert closing.json()["error"]["context"]["cycle"]


def test_priority_date_inheritance_after_approval(client, admin, approvers):
    grandparent = _import_app(
        client, admin["headers"], number="PRI-G",
        filing_date="2023-01-05", priority_claim_date="2023-01-05",
    )
    parent = _import_app(client, admin["headers"], number="PRI-P", filing_date="2024-02-01")
    child = _import_app(client, admin["headers"], number="PRI-C", jurisdiction="US", filing_date="2025-03-01")

    def link(child_id, parent_id, relation):
        request = client.post(
            "/api/patent-families/changes/links",
            headers=admin["headers"],
            json={"child_application_id": child_id, "parent_application_id": parent_id, "relation_type": relation},
        )
        assert request.status_code == 201, request.text
        return request.json()["request"]["id"]

    _two_approvals(client, approvers, link(parent["id"], grandparent["id"], "priority"))
    _two_approvals(client, approvers, link(child["id"], parent["id"], "national_phase"))

    detail = client.get(f"/api/patent-families/applications/{child['id']}", headers=admin["headers"]).json()
    assert detail["effective_priority_date"] == "2023-01-05"
    family = client.get(f"/api/patent-families/{grandparent['family_id']}", headers=admin["headers"]).json()
    assert family["topological_order"] == [grandparent["id"], parent["id"], child["id"]]


def test_pending_change_idempotent_submission(client, admin, approvers):
    parent = _import_app(client, admin["headers"], number="IDM-P")
    child = _import_app(client, admin["headers"], number="IDM-C")
    body = {
        "child_application_id": child["id"],
        "parent_application_id": parent["id"],
        "relation_type": "continuation",
        "idempotency_key": "link-idm-0001",
    }
    first = client.post("/api/patent-families/changes/links", headers=admin["headers"], json=body)
    second = client.post("/api/patent-families/changes/links", headers=admin["headers"], json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["request"]["id"] == second.json()["request"]["id"]
    assert second.json()["replayed"] is True


def test_merge_blocked_when_secret_member_joins_public_target(client, admin):
    secret = _import_app(
        client, admin["headers"], number="MRG-S",
        application_status="filed", secret_asset=True,
        filing_date="2025-05-01",
    )
    public_target = _import_app(
        client, admin["headers"], number="MRG-T",
        application_status="published", publication_date="2026-01-01",
        filing_date="2025-06-01",
    )
    response = client.post(
        "/api/patent-families/changes/merges",
        headers=admin["headers"],
        json={
            "source_application_ids": [secret["id"]],
            "target_application_id": public_target["id"],
            "reason": "误录为两件，需要合并",
        },
    )
    assert response.status_code == 409
    codes = {item["code"] for item in response.json()["error"]["context"]["conflicts"]}
    assert "secret_into_public" in codes


def test_merge_preview_conflicts_and_execution_rewires_links(client, admin, approvers):
    # 家族：root -> dup(source) 与 root -> target，合并 dup 入 target 时应识别重复边
    root = _import_app(client, admin["headers"], number="MRG2-R", filing_date="2023-01-01", priority_claim_date="2023-01-01")
    dup = _import_app(client, admin["headers"], number="MRG2-D", filing_date="2024-09-09")
    target = _import_app(client, admin["headers"], number="MRG2-T", filing_date="2024-02-02")
    outside = _import_app(client, admin["headers"], number="MRG2-O", filing_date="2024-03-03")

    def link(child_id, parent_id, relation="priority"):
        request = client.post(
            "/api/patent-families/changes/links",
            headers=admin["headers"],
            json={"child_application_id": child_id, "parent_application_id": parent_id, "relation_type": relation},
        )
        assert request.status_code == 201, request.text
        _two_approvals(client, approvers, request.json()["request"]["id"])

    link(dup["id"], root["id"])
    link(target["id"], root["id"])
    link(outside["id"], dup["id"])

    preview_response = client.post(
        "/api/patent-families/changes/merges",
        headers=admin["headers"],
        json={
            "source_application_ids": [dup["id"]],
            "target_application_id": target["id"],
            "reason": "同一申请重复建档，合并保留 MRG2-T",
        },
    )
    # 仅有 warning（filing_date 不一致），允许提交
    assert preview_response.status_code == 201, preview_response.text
    payload = preview_response.json()["request"]["payload"]
    effects = {item["effect"] for item in payload["preview"]["link_impacts"]}
    assert "duplicate_superseded" in effects
    assert "rewired_to_target" in effects

    _two_approvals(client, approvers, preview_response.json()["request"]["id"])

    merged_app = client.get(f"/api/patent-families/applications/{dup['id']}", headers=admin["headers"]).json()
    assert merged_app["application"]["application_status"] == "merged"
    assert merged_app["application"]["merged_into_id"] == target["id"]

    family = client.get(f"/api/patent-families/{root['family_id']}", headers=admin["headers"]).json()
    active_pairs = {(link["child_application_id"], link["parent_application_id"]) for link in family["active_links"]}
    assert (target["id"], root["id"]) in active_pairs
    assert (outside["id"], target["id"]) in active_pairs
    assert (dup["id"], root["id"]) not in active_pairs
    assert family["excluded_links"], "被取代的关系应在汇总中排除但可追溯"


def test_revoke_impact_preview_and_exclusion_from_summary(client, admin, approvers):
    root = _import_app(client, admin["headers"], number="RVK-R", filing_date="2023-01-01", priority_claim_date="2023-01-01")
    middle = _import_app(client, admin["headers"], number="RVK-M", filing_date="2024-01-01")
    leaf = _import_app(client, admin["headers"], number="RVK-L", jurisdiction="US", filing_date="2025-01-01")

    def link(child_id, parent_id, relation):
        request = client.post(
            "/api/patent-families/changes/links",
            headers=admin["headers"],
            json={"child_application_id": child_id, "parent_application_id": parent_id, "relation_type": relation},
        )
        _two_approvals(client, approvers, request.json()["request"]["id"])
        return request

    link(middle["id"], root["id"], "priority")
    second = link(leaf["id"], middle["id"], "national_phase")

    family = client.get(f"/api/patent-families/{root['family_id']}", headers=admin["headers"]).json()
    assert family["member_count"] == 3
    bridge = next(
        link for link in family["active_links"]
        if link["child_application_id"] == middle["id"] and link["parent_application_id"] == root["id"]
    )

    impact = client.get(f"/api/patent-families/links/{bridge['id']}/impact", headers=admin["headers"]).json()
    assert impact["family_would_split"] is True
    changed = {item["application_id"]: item for item in impact["priority_date_changes"]}
    assert changed[middle["id"]]["after"] == "2024-01-01"
    assert changed[leaf["id"]]["after"] == "2024-01-01"

    revoke = client.post(
        "/api/patent-families/changes/revocations",
        headers=admin["headers"],
        json={"link_id": bridge["id"], "reason": "优先权主张补录有误，法务复核后撤销"},
    )
    assert revoke.status_code == 201, revoke.text
    _two_approvals(client, approvers, revoke.json()["request"]["id"])

    family_after = client.get(f"/api/patent-families/{root['family_id']}", headers=admin["headers"]).json()
    assert all(link["id"] != bridge["id"] for link in family_after["active_links"])
    assert family_after["active_links"] == []
    # 分裂后 middle/leaf 成为独立家族，撤销边在该家族的排除清单中可追溯
    middle_detail = client.get(f"/api/patent-families/applications/{middle['id']}", headers=admin["headers"]).json()
    split_family = client.get(
        f"/api/patent-families/{middle_detail['family_id']}", headers=admin["headers"]
    ).json()
    assert {member["id"] for member in split_family["members"]} == {middle["id"], leaf["id"]}
    assert any(link["id"] == bridge["id"] and link["status"] == "revoked" for link in split_family["excluded_links"])
    assert {(link["child_application_id"], link["parent_application_id"]) for link in split_family["active_links"]} == {(leaf["id"], middle["id"])}

    # 不能重复撤销
    duplicate = client.post(
        "/api/patent-families/changes/revocations",
        headers=admin["headers"],
        json={"link_id": bridge["id"], "reason": "再次撤销"},
    )
    assert duplicate.status_code == 409


def test_change_execution_fails_when_structure_changed_during_approval(client, admin, approvers):
    child = _import_app(client, admin["headers"], number="DRF-C")
    parent = _import_app(client, admin["headers"], number="DRF-P")
    survivor = _import_app(client, admin["headers"], number="DRF-S")

    link_request = client.post(
        "/api/patent-families/changes/links",
        headers=admin["headers"],
        json={"child_application_id": child["id"], "parent_application_id": parent["id"], "relation_type": "continuation"},
    )
    link_id = link_request.json()["request"]["id"]
    # 第一名审批人先通过关系提案
    first = client.post(
        f"/api/patent-families/changes/{link_id}/decisions",
        headers=approvers[0], json={"decision": "approve"},
    )
    assert first.status_code == 200

    # 审批期间：父申请被合并进其他成员
    merge_request = client.post(
        "/api/patent-families/changes/merges",
        headers=admin["headers"],
        json={
            "source_application_ids": [parent["id"]],
            "target_application_id": survivor["id"],
            "reason": "父申请重复建档，先行合并",
        },
    )
    assert merge_request.status_code == 201, merge_request.text
    _two_approvals(client, approvers, merge_request.json()["request"]["id"])

    # 第二名审批人再通过原关系提案：审批成立，但执行期重校验失败，不写入脏关系
    second = client.post(
        f"/api/patent-families/changes/{link_id}/decisions",
        headers=approvers[1], json={"decision": "approve"},
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["request"]["status"] == "failed"
    assert body["execution_error"]
    detail = client.get(f"/api/patent-families/changes/{link_id}", headers=admin["headers"]).json()
    assert detail["status"] == "failed"
    assert detail["execution_error"]
    # 两位审批人的决定仍然保留
    assert len(detail["decisions"]) == 2

    family = client.get(f"/api/patent-families/{child['family_id']}", headers=admin["headers"]).json()
    assert family["active_links"] == []


def test_topology_and_audit_order_are_stable_after_restart(client, admin, approvers):
    apps = [
        _import_app(client, admin["headers"], number=f"DET-{i}", filing_date=f"2025-01-{i:02d}")
        for i in range(1, 5)
    ]
    # 链式：0<-2<-3, 0<-1, 制造需要稳定决断的分叉
    for child_id, parent_id in ((apps[2]["id"], apps[0]["id"]), (apps[3]["id"], apps[2]["id"]), (apps[1]["id"], apps[0]["id"])):
        request = client.post(
            "/api/patent-families/changes/links",
            headers=admin["headers"],
            json={"child_application_id": child_id, "parent_application_id": parent_id, "relation_type": "priority"},
        )
        _two_approvals(client, approvers, request.json()["request"]["id"])

    family_url = f"/api/patent-families/{apps[0]['family_id']}"
    timeline_url = f"{family_url}/timeline"
    before = client.get(family_url, headers=admin["headers"]).json()
    timeline_before = client.get(timeline_url, headers=admin["headers"]).json()

    # 模拟服务重启：丢弃线程内连接，下一次请求重新打开同一个数据库文件。
    from app.database import close_connection
    close_connection()

    after = client.get(family_url, headers=admin["headers"]).json()
    timeline_after = client.get(timeline_url, headers=admin["headers"]).json()
    assert after["topological_order"] == before["topological_order"]
    assert [m["id"] for m in after["members"]] == [m["id"] for m in before["members"]]
    assert timeline_after["audit_order"] == timeline_before["audit_order"]
    # 审计顺序严格递增、无重复
    assert timeline_after["audit_order"] == sorted(timeline_after["audit_order"])
    assert len(set(timeline_after["audit_order"])) == len(timeline_after["audit_order"])
