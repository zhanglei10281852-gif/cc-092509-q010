"""专利家族、申请、关系与变更的持久化访问。

所有列表查询都带显式排序，保证服务重启后拓扑与审计顺序确定。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import ConflictError, NotFoundError


def _row(row: sqlite3.Row | None, message: str = "记录不存在") -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return dict(row)


class FamilyRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, family_code: str, title: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patent_families(family_code,title,created_at,updated_at) VALUES(?,?,?,?)",
            (family_code, title, now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, family_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM patent_families WHERE id=?", (family_id,)).fetchone(),
            "专利家族不存在",
        )

    def by_code(self, family_code: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM patent_families WHERE family_code=?", (family_code,)
        ).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT f.*,
                      (SELECT COUNT(*) FROM patent_applications a WHERE a.family_id=f.id) AS member_count
               FROM patent_families f ORDER BY f.id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_merged(self, family_id: int, target_family_id: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """UPDATE patent_families SET status='merged',merged_into_family_id=?,updated_at=?
               WHERE id=? AND status='active'""",
            (target_family_id, now, family_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("来源家族状态已变化，无法合并")
        return self.get(family_id)

    def move_members(self, source_family_id: int, target_family_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE patent_applications SET family_id=?,updated_at=? WHERE family_id=?",
            (target_family_id, now, source_family_id),
        )
        self.connection.execute(
            "UPDATE patent_family_relations SET family_id=?,updated_at=? WHERE family_id=?",
            (target_family_id, now, source_family_id),
        )


class ApplicationRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], family_id: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO patent_applications(
                   application_number,jurisdiction,title,applicant,filing_date,declared_priority_date,
                   publication_status,is_secret_asset,family_id,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["application_number"], data["jurisdiction"], data["title"], data.get("applicant", ""),
                data["filing_date"], data.get("declared_priority_date"), data["publication_status"],
                int(data.get("is_secret_asset", False)), family_id, now, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, application_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute(
                "SELECT * FROM patent_applications WHERE id=?", (application_id,)
            ).fetchone(),
            "专利申请不存在",
        )

    def by_number(self, application_number: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM patent_applications WHERE application_number=?", (application_number,)
        ).fetchone()
        return dict(row) if row else None

    def members(self, family_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM patent_applications WHERE family_id=? ORDER BY id", (family_id,)
        ).fetchall()
        return [dict(row) for row in rows]


class RelationRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(
        self,
        family_id: int,
        parent_application_id: int,
        child_application_id: int,
        relation_type: str,
        change_id: int,
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO patent_family_relations(
                   family_id,parent_application_id,child_application_id,relation_type,change_id,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (family_id, parent_application_id, child_application_id, relation_type, change_id, now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, relation_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute(
                "SELECT * FROM patent_family_relations WHERE id=?", (relation_id,)
            ).fetchone(),
            "专利家族关系不存在",
        )

    def active_between(self, parent_application_id: int, child_application_id: int, relation_type: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM patent_family_relations
               WHERE parent_application_id=? AND child_application_id=? AND relation_type=? AND status='active'""",
            (parent_application_id, child_application_id, relation_type),
        ).fetchone()
        return dict(row) if row else None

    def family_relations(self, family_id: int, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                "SELECT * FROM patent_family_relations WHERE family_id=? AND status=? ORDER BY id",
                (family_id, status),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM patent_family_relations WHERE family_id=? ORDER BY id", (family_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke(self, relation_id: int, change_id: int, reason: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """UPDATE patent_family_relations
               SET status='revoked',revoked_at=?,revoke_reason=?,revoke_change_id=?,updated_at=?
               WHERE id=? AND status='active'""",
            (now, reason, change_id, now, relation_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("关系状态已变化，无法撤销")
        return self.get(relation_id)


class ChangeRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, change_code: str, family_id: int, change_type: str, payload: dict[str, Any], requested_by: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO patent_family_changes(change_code,family_id,change_type,payload_json,requested_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (change_code, family_id, change_type, json.dumps(payload, ensure_ascii=False), requested_by, now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, change_id: int) -> dict[str, Any]:
        row = _row(
            self.connection.execute(
                "SELECT * FROM patent_family_changes WHERE id=?", (change_id,)
            ).fetchone(),
            "专利家族变更不存在",
        )
        row["payload"] = json.loads(row.pop("payload_json"))
        return row

    def list(self, *, state: str | None = None, family_id: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if family_id:
            clauses.append("family_id=?")
            params.append(family_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM patent_family_changes" + where + " ORDER BY id", tuple(params)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def pending_link(self, family_id: int, parent_application_id: int, child_application_id: int, relation_type: str) -> dict[str, Any] | None:
        rows = self.connection.execute(
            """SELECT * FROM patent_family_changes
               WHERE family_id=? AND change_type='link' AND state IN ('pending','approved') ORDER BY id""",
            (family_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if (
                payload.get("parent_application_id") == parent_application_id
                and payload.get("child_application_id") == child_application_id
                and payload.get("relation_type") == relation_type
            ):
                item = dict(row)
                item["payload"] = payload
                return item
        return None

    def pending_for_family(self, family_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM patent_family_changes
               WHERE family_id=? AND state IN ('pending','approved') ORDER BY id""",
            (family_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def transition(self, change_id: int, expected_states: tuple[str, ...], state: str, now: str, **fields: Any) -> dict[str, Any]:
        assignments = ["state=?", "updated_at=?"]
        params: list[Any] = [state, now]
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            params.append(value)
        placeholders = ",".join("?" for _ in expected_states)
        params.extend([change_id, *expected_states])
        cursor = self.connection.execute(
            f"UPDATE patent_family_changes SET {','.join(assignments)} WHERE id=? AND state IN ({placeholders})",
            tuple(params),
        )
        if cursor.rowcount != 1:
            raise ConflictError("变更状态已变化，请刷新后重试")
        return self.get(change_id)


class FamilyEventRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def append(self, family_id: int, event_type: str, actor_user_id: int | None, now: str, details: dict[str, Any] | None = None) -> None:
        self.connection.execute(
            """INSERT INTO patent_family_events(family_id,event_type,actor_user_id,details_json,occurred_at)
               VALUES(?,?,?,?,?)""",
            (family_id, event_type, actor_user_id, json.dumps(details or {}, ensure_ascii=False), now),
        )

    def list(self, family_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM patent_family_events WHERE family_id=? ORDER BY id", (family_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result
