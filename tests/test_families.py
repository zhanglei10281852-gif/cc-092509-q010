from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _make_user(client, admin, username, role_codes):
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Review!23456", "display_name": username, "role_codes": role_codes},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Review!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


@pytest.fixture()
def approver(client, admin):
    return _make_user(client, admin, "family.approver", ["approver"])


def _register_app(client, headers, family_code, number, jurisdiction, filing_date, **extra):
    payload = {
        "family_code": family_code,
        "application_number": number,
        "jurisdiction": jurisdiction,
        "title": f"申请{number}",
        "filing_date": filing_date,
        **extra,
    }
    response = client.post("/api/patent-families/applications", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["application"]


def _propose_link(client, headers, family_id, parent_id, child_id, relation_type):
    return client.post(
        "/api/patent-families/changes",
        headers=headers,
        json={
            "change_type": "link",
            "family_id": family_id,
            "link": {
                "parent_application_id": parent_id,
                "child_application_id": child_id,
                "relation_type": relation_type,
            },
        },
    )


def _approve_and_apply(client, change_id, approver_headers, admin_headers):
    decision = client.post(
        f"/api/patent-families/changes/{change_id}/decisions",
        headers=approver_headers,
        json={"decision": "approve"},
    )
    assert decision.status_code == 200, decision.text
    applied = client.post(f"/api/patent-families/changes/{change_id}/apply", headers=admin_headers)
    assert applied.status_code == 200, applied.text
    return applied.json()


def _link_and_apply(client, admin, approver, family_id, parent_id, child_id, relation_type):
    change = _propose_link(client, admin["headers"], family_id, parent_id, child_id, relation_type)
    assert change.status_code == 201, change.text
    return _approve_and_apply(client, change.json()["id"], approver, admin["headers"])


def test_link_takes_effect_only_after_approval(client, admin, approver):
    app_a = _register_app(client, admin["headers"], "FAM-EFF", "CN1001", "CN", "2024-01-01")
    app_b = _register_app(client, admin["headers"], "FAM-EFF", "US1001", "US", "2024-06-01")
    family_id = app_a["family_id"]

    change = _propose_link(client, admin["headers"], family_id, app_a["id"], app_b["id"], "priority")
    assert change.status_code == 201, change.text
    assert change.json()["state"] == "pending"

    topology = client.get(f"/api/patent-families/{family_id}/topology", headers=admin["headers"])
    assert topology.json()["edges"] == []

    _approve_and_apply(client, change.json()["id"], approver, admin["headers"])

    topology = client.get(f"/api/patent-families/{family_id}/topology", headers=admin["headers"])
    body = topology.json()
    assert len(body["edges"]) == 1
    assert body["edges"][0]["relation_type"] == "priority"
    assert body["topological_order"] == [app_a["id"], app_b["id"]]
    assert body["roots"] == [app_a["id"]]
    nodes = {node["id"]: node for node in body["nodes"]}
    assert nodes[app_b["id"]]["effective_priority_date"] == "2024-01-01"
    assert nodes[app_b["id"]]["priority_source_application_id"] == app_a["id"]


def test_priority_date_inherits_transitively(client, admin, approver):
    app_a = _register_app(client, admin["headers"], "FAM-INH", "CN2001", "CN", "2024-01-01")
    app_b = _register_app(client, admin["headers"], "FAM-INH", "US2001", "US", "2024-06-01")
    app_c = _register_app(client, admin["headers"], "FAM-INH", "EP2001", "EP", "2024-07-01", declared_priority_date="2024-07-01")
    family_id = app_a["family_id"]
    _link_and_apply(client, admin, approver, family_id, app_a["id"], app_b["id"], "priority")
    _link_and_apply(client, admin, approver, family_id, app_b["id"], app_c["id"], "priority")

    lineage = client.get(f"/api/patent-families/applications/{app_c['id']}/lineage", headers=admin["headers"])
    body = lineage.json()
    assert body["effective_priority_date"] == "2024-01-01"
    assert body["priority_source_application_id"] == app_a["id"]
    assert [step["relation_type"] for step in body["priority_chain"]] == ["priority", "priority"]
    assert [(step["parent_application_id"], step["child_application_id"]) for step in body["priority_chain"]] == [
        (app_a["id"], app_b["id"]),
        (app_b["id"], app_c["id"]),
    ]
    assert body["ancestors"] == sorted([app_a["id"], app_b["id"]])

    lineage_a = client.get(f"/api/patent-families/applications/{app_a['id']}/lineage", headers=admin["headers"])
    assert lineage_a.json()["descendants"] == sorted([app_b["id"], app_c["id"]])


def test_relation_type_validation(client, admin, approver):
    app_a = _register_app(client, admin["headers"], "FAM-TYPE", "CN3001", "CN", "2024-01-01")
    app_b = _register_app(client, admin["headers"], "FAM-TYPE", "US3001", "US", "2024-06-01")
    family_id = app_a["family_id"]

    cross_continuation = _propose_link(client, admin["headers"], family_id, app_a["id"], app_b["id"], "continuation")
    assert cross_continuation.status_code == 422
    assert "同一国家或地区" in cross_continuation.json()["error"]["message"]

    cross_divisional = _propose_link(client, admin["headers"], family_id, app_a["id"], app_b["id"], "divisional")
    assert cross_divisional.status_code == 422

    cross_priority = _propose_link(client, admin["headers"], family_id, app_a["id"], app_b["id"], "priority")
    assert cross_priority.status_code == 201, cross_priority.text

    late_parent = _register_app(client, admin["headers"], "FAM-TYPE", "CN3002", "CN", "2024-09-01")
    temporal = _propose_link(client, admin["headers"], family_id, late_parent["id"], app_b["id"], "priority")
    assert temporal.status_code == 422
    assert "申请日" in temporal.json()["error"]["message"]

    self_loop = _propose_link(client, admin["headers"], family_id, app_a["id"], app_a["id"], "priority")
    assert self_loop.status_code == 422

    unknown_type = client.post(
        "/api/patent-families/changes",
        headers=admin["headers"],
        json={
            "change_type": "link",
            "family_id": family_id,
            "link": {"parent_application_id": app_a["id"], "child_application_id": app_b["id"], "relation_type": "cousin"},
        },
    )
    assert unknown_type.status_code == 422


def test_cycle_is_rejected(client, admin, approver):
    app_a = _register_app(client, admin["headers"], "FAM-CYC", "CN4001", "CN", "2024-01-01")
    app_b = _register_app(client, admin["headers"], "FAM-CYC", "CN4002", "CN", "2024-01-01")
    app_c = _register_app(client, admin["headers"], "FAM-CYC", "CN4003", "CN", "2024-01-01")
    family_id = app_a["family_id"]
    _link_and_apply(client, admin, approver, family_id, app_a["id"], app_b["id"], "continuation")
    _link_and_apply(client, admin, approver, family_id, app_b["id"], app_c["id"], "continuation")

    cycle = _propose_link(client, admin["headers"], family_id, app_c["id"], app_a["id"], "continuation")
    assert cycle.status_code == 409
    assert "循环" in cycle.json()["error"]["message"]


def test_published_application_cannot_be_secret_asset(client, admin):
    response = client.post(
        "/api/patent-families/applications",
        headers=admin["headers"],
        json={
            "family_code": "FAM-SEC",
            "application_number": "CN5001",
            "jurisdiction": "CN",
            "title": "已公开技术",
            "filing_date": "2024-01-01",
            "publication_status": "published",
            "is_secret_asset": True,
        },
    )
    assert response.status_code == 422
    assert "秘密资产" in response.json()["error"]["message"]


def test_requester_cannot_approve_own_change(client, admin):
    app_a = _register_app(client, admin["headers"], "FAM-SELF", "CN6001", "CN", "2024-01-01")
    app_b = _register_app(client, admin["headers"], "FAM-SELF", "CN6002", "CN", "2024-02-01")
    change = _propose_link(client, admin["headers"], app_a["family_id"], app_a["id"], app_b["id"], "continuation")
    assert change.status_code == 201
    own = client.post(
        f"/api/patent-families/changes/{change.json()['id']}/decisions",
        headers=admin["headers"],
        json={"decision": "approve"},
    )
    assert own.status_code == 422


def test_import_is_idempotent_and_keeps_original_members(client, admin, approver):
    payload = {
        "family_code": "FAM-IMP",
        "family_title": "导入家族",
        "applications": [
            {"application_number": "CN7001", "jurisdiction": "CN", "title": "在先申请", "filing_date": "2024-01-01"},
            {"application_number": "US7001", "jurisdiction": "US", "title": "在后申请", "filing_date": "2024-06-01"},
        ],
        "relations": [
            {"parent_application_number": "CN7001", "child_application_number": "US7001", "relation_type": "priority"},
        ],
    }
    first = client.post("/api/patent-families/import", headers=admin["headers"], json=payload)
    assert first.status_code == 201, first.text
    assert len(first.json()["created"]) == 2
    assert len(first.json()["relation_changes"]) == 1

    second = client.post("/api/patent-families/import", headers=admin["headers"], json=payload)
    assert second.status_code == 201, second.text
    assert second.json()["created"] == []
    assert [item["application_number"] for item in second.json()["replayed"]] == ["CN7001", "US7001"]
    assert second.json()["relation_changes"] == []
    assert {item["reason"] for item in second.json()["skipped_relations"]} == {"duplicate_pending"}

    change_id = first.json()["relation_changes"][0]["id"]
    _approve_and_apply(client, change_id, approver, admin["headers"])

    third = client.post("/api/patent-families/import", headers=admin["headers"], json=payload)
    assert third.json()["created"] == []
    assert len(third.json()["replayed"]) == 2
    assert "相同的生效关系已经存在" in {item["reason"] for item in third.json()["skipped_relations"]}

    other = client.post(
        "/api/patent-families/import",
        headers=admin["headers"],
        json={"family_code": "FAM-OTHER", "applications": [payload["applications"][0]]},
    )
    assert other.status_code == 201, other.text
    assert len(other.json()["replayed"]) == 1
    kept = client.get(f"/api/patent-families/applications/{other.json()['replayed'][0]['id']}", headers=admin["headers"])
    assert kept.json()["family"]["family_code"] == "FAM-IMP"


def test_revoke_excludes_relation_from_summary_and_topology(client, admin, approver):
    app_a = _register_app(client, admin["headers"], "FAM-REV", "CN8001", "CN", "2024-01-01")
    app_b = _register_app(client, admin["headers"], "FAM-REV", "CN8002", "CN", "2024-02-01")
    family_id = app_a["family_id"]
    applied = _link_and_apply(client, admin, approver, family_id, app_a["id"], app_b["id"], "continuation")
    relation_id = applied["outcome"]["relation"]["id"]

    revoke = client.post(
        "/api/patent-families/changes",
        headers=admin["headers"],
        json={"change_type": "revoke", "family_id": family_id, "relation_id": relation_id, "reason": "补录错误"},
    )
    assert revoke.status_code == 201, revoke.text
    _approve_and_apply(client, revoke.json()["id"], approver, admin["headers"])

    topology = client.get(f"/api/patent-families/{family_id}/topology", headers=admin["headers"])
    assert topology.json()["edges"] == []
    assert topology.json()["revoked_relation_count"] == 1

    summary = client.get(f"/api/patent-families/{family_id}/summary", headers=admin["headers"])
    body = summary.json()
    assert body["active_relation_count"] == 0
    assert body["relations_by_type"] == {}
    assert body["revoked_relation_count"] == 1


def test_revocation_impact_query_before_and_after(client, admin, approver):
    app_a = _register_app(client, admin["headers"], "FAM-IMP2", "CN9001", "CN", "2024-01-01")
    app_b = _register_app(client, admin["headers"], "FAM-IMP2", "US9001", "US", "2024-06-01")
    app_c = _register_app(client, admin["headers"], "FAM-IMP2", "EP9001", "EP", "2024-07-01")
    family_id = app_a["family_id"]
    first = _link_and_apply(client, admin, approver, family_id, app_a["id"], app_b["id"], "priority")
    _link_and_apply(client, admin, approver, family_id, app_b["id"], app_c["id"], "priority")
    relation_id = first["outcome"]["relation"]["id"]

    impact = client.get(f"/api/patent-families/relations/{relation_id}/impact", headers=admin["headers"])
    body = impact.json()
    assert body["relation_status"] == "active"
    affected = {item["application_id"]: item for item in body["affected_applications"]}
    assert set(affected) == {app_b["id"], app_c["id"]}
    assert affected[app_b["id"]]["priority_date_with_relation"] == "2024-01-01"
    assert affected[app_b["id"]]["priority_date_without_relation"] == "2024-06-01"
    assert affected[app_c["id"]]["priority_date_without_relation"] == "2024-06-01"
    assert body["component_count_with_relation"] == 1
    assert body["component_count_without_relation"] == 2

    revoke = client.post(
        "/api/patent-families/changes",
        headers=admin["headers"],
        json={"change_type": "revoke", "family_id": family_id, "relation_id": relation_id, "reason": "优先权主张撤回"},
    )
    _approve_and_apply(client, revoke.json()["id"], approver, admin["headers"])

    retrospective = client.get(f"/api/patent-families/relations/{relation_id}/impact", headers=admin["headers"])
    after = retrospective.json()
    assert after["relation_status"] == "revoked"
    assert {item["application_id"] for item in after["affected_applications"]} == {app_b["id"], app_c["id"]}

    topology = client.get(f"/api/patent-families/{family_id}/topology", headers=admin["headers"])
    nodes = {node["id"]: node for node in topology.json()["nodes"]}
    assert nodes[app_b["id"]]["effective_priority_date"] == "2024-06-01"
    assert nodes[app_c["id"]]["effective_priority_date"] == "2024-06-01"


def test_merge_rehearsal_conflicts_and_merge_flow(client, admin, approver):
    secret_app = _register_app(
        client, admin["headers"], "FAM-SRC", "CN1101", "CN", "2024-01-01", is_secret_asset=True
    )
    published_app = _register_app(
        client, admin["headers"], "FAM-DST", "US1101", "US", "2024-06-01", publication_status="published"
    )
    source_id, target_id = secret_app["family_id"], published_app["family_id"]

    pending = _propose_link(client, admin["headers"], source_id, secret_app["id"], secret_app["id"], "priority")
    assert pending.status_code == 422  # 自关系校验先于其他结果

    blocker = _register_app(client, admin["headers"], "FAM-SRC", "CN1102", "CN", "2024-02-01")
    pending_change = _propose_link(client, admin["headers"], source_id, secret_app["id"], blocker["id"], "continuation")
    assert pending_change.status_code == 201

    rehearsal = client.post(
        "/api/patent-families/merge/rehearse",
        headers=admin["headers"],
        json={"source_family_id": source_id, "target_family_id": target_id},
    )
    body = rehearsal.json()
    assert body["mergeable"] is False
    assert {item["code"] for item in body["conflicts"]} == {"pending_changes"}
    assert {item["code"] for item in body["warnings"]} == {"secrecy_exposure"}

    cancel = client.post(f"/api/patent-families/changes/{pending_change.json()['id']}/cancel", headers=admin["headers"])
    assert cancel.status_code == 200, cancel.text

    rehearsal = client.post(
        "/api/patent-families/merge/rehearse",
        headers=admin["headers"],
        json={
            "source_family_id": source_id,
            "target_family_id": target_id,
            "link": {
                "parent_application_id": secret_app["id"],
                "child_application_id": published_app["id"],
                "relation_type": "priority",
            },
        },
    )
    body = rehearsal.json()
    assert body["mergeable"] is True
    assert body["members_to_move"] == 2
    assert body["resulting_member_count"] == 3

    bad_link = client.post(
        "/api/patent-families/merge/rehearse",
        headers=admin["headers"],
        json={
            "source_family_id": source_id,
            "target_family_id": target_id,
            "link": {
                "parent_application_id": secret_app["id"],
                "child_application_id": blocker["id"],
                "relation_type": "continuation",
            },
        },
    )
    assert {item["code"] for item in bad_link.json()["conflicts"]} == {"link_endpoint_family"}

    merge = client.post(
        "/api/patent-families/changes",
        headers=admin["headers"],
        json={
            "change_type": "merge",
            "family_id": target_id,
            "source_family_id": source_id,
            "merge_link": {
                "parent_application_id": secret_app["id"],
                "child_application_id": published_app["id"],
                "relation_type": "priority",
            },
        },
    )
    assert merge.status_code == 201, merge.text
    outcome = _approve_and_apply(client, merge.json()["id"], approver, admin["headers"])
    assert outcome["outcome"]["source_family"]["status"] == "merged"
    assert outcome["outcome"]["link_relation"]["relation_type"] == "priority"

    topology = client.get(f"/api/patent-families/{target_id}/topology", headers=admin["headers"])
    body = topology.json()
    assert len(body["nodes"]) == 3
    assert len(body["edges"]) == 1
    nodes = {node["id"]: node for node in body["nodes"]}
    assert nodes[published_app["id"]]["effective_priority_date"] == "2024-01-01"

    source_detail = client.get(f"/api/patent-families/{source_id}", headers=admin["headers"])
    assert source_detail.json()["status"] == "merged"
    assert source_detail.json()["merged_into_family_id"] == target_id


def test_summary_counts_exclude_revoked_and_published_secrets(client, admin, approver):
    app_a = _register_app(client, admin["headers"], "FAM-SUM", "CN1201", "CN", "2024-01-01", is_secret_asset=True)
    app_b = _register_app(client, admin["headers"], "FAM-SUM", "CN1202", "CN", "2024-02-01")
    _register_app(client, admin["headers"], "FAM-SUM", "US1201", "US", "2024-03-01", publication_status="published")
    family_id = app_a["family_id"]
    applied = _link_and_apply(client, admin, approver, family_id, app_a["id"], app_b["id"], "continuation")
    relation_id = applied["outcome"]["relation"]["id"]

    summary = client.get(f"/api/patent-families/{family_id}/summary", headers=admin["headers"])
    body = summary.json()
    assert body["member_count"] == 3
    assert body["by_jurisdiction"] == {"CN": 2, "US": 1}
    assert body["by_publication_status"] == {"published": 1, "unpublished": 2}
    assert body["secret_asset_count"] == 1
    assert body["relations_by_type"] == {"continuation": 1}
    assert body["earliest_effective_priority_date"] == "2024-01-01"

    revoke = client.post(
        "/api/patent-families/changes",
        headers=admin["headers"],
        json={"change_type": "revoke", "family_id": family_id, "relation_id": relation_id, "reason": "关系登记错误"},
    )
    _approve_and_apply(client, revoke.json()["id"], approver, admin["headers"])
    summary = client.get(f"/api/patent-families/{family_id}/summary", headers=admin["headers"])
    assert summary.json()["active_relation_count"] == 0
    assert summary.json()["revoked_relation_count"] == 1


def test_restart_preserves_deterministic_topology_and_audit_order(client, admin, approver):
    app_a = _register_app(client, admin["headers"], "FAM-RST", "CN1301", "CN", "2024-01-01")
    app_b = _register_app(client, admin["headers"], "FAM-RST", "CN1302", "CN", "2024-01-01")
    app_c = _register_app(client, admin["headers"], "FAM-RST", "CN1303", "CN", "2024-01-02")
    family_id = app_a["family_id"]
    _link_and_apply(client, admin, approver, family_id, app_a["id"], app_c["id"], "continuation")
    _link_and_apply(client, admin, approver, family_id, app_b["id"], app_c["id"], "continuation")

    topology_before = client.get(f"/api/patent-families/{family_id}/topology", headers=admin["headers"]).json()
    events_before = client.get(f"/api/patent-families/{family_id}/events", headers=admin["headers"]).json()
    assert topology_before["topological_order"] == [app_a["id"], app_b["id"], app_c["id"]]

    from app.database import close_connection
    from app.main import app

    close_connection()
    with TestClient(app) as restarted:
        topology_after = restarted.get(f"/api/patent-families/{family_id}/topology", headers=admin["headers"]).json()
        events_after = restarted.get(f"/api/patent-families/{family_id}/events", headers=admin["headers"]).json()
    assert topology_after == topology_before
    assert events_after == events_before
    assert [event["id"] for event in events_after] == sorted(event["id"] for event in events_after)
    event_types = [event["event_type"] for event in events_after]
    assert event_types[0] == "family.created"
    assert event_types.count("relation.linked") == 2
