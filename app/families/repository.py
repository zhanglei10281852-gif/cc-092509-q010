from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from app.core.errors import ConflictError, NotFoundError


class PatentApplicationRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, application_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM patent_applications WHERE id=?", (application_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("专利申请不存在")
        return dict(row)

    def find(self, jurisdiction: str, application_number: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM patent_applications WHERE jurisdiction=? AND application_number=?",
            (jurisdiction.strip().upper(), application_number.strip().upper()),
        ).fetchone()
        return dict(row) if row else None

    def get_many(self, application_ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = sorted(set(application_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM patent_applications WHERE id IN ({placeholders})", ids
        ).fetchall()
        return [dict(row) for row in rows]

    def list_family(self, family_id: int | None) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM patent_applications WHERE family_id=? ORDER BY id",
            (family_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_all(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM patent_applications ORDER BY family_id, id"
        ).fetchall()
        return [dict(row) for row in rows]

    def create(self, data: dict[str, Any], created_by: int | None, now: str) -> dict[str, Any]:
        jurisdiction = data["jurisdiction"].strip().upper()
        application_number = data["application_number"].strip().upper()
        if data.get("dossier_id") is not None:
            dossier = self.connection.execute(
                "SELECT id FROM dossiers WHERE id=?", (data["dossier_id"],)
            ).fetchone()
            if not dossier:
                raise NotFoundError("关联的技术秘密档案不存在")
        cursor = self.connection.execute(
            """INSERT INTO patent_applications(
                   jurisdiction,application_number,family_id,dossier_id,title,application_status,
                   filing_date,publication_date,grant_date,priority_claim_date,secret_asset,
                   import_fingerprint,version,created_by,created_at,updated_at
               ) VALUES(?,?,NULL,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (
                jurisdiction,
                application_number,
                data.get("dossier_id"),
                data["title"],
                data.get("application_status", "filed"),
                data["filing_date"],
                data.get("publication_date"),
                data.get("grant_date"),
                data.get("priority_claim_date"),
                1 if data.get("secret_asset") else 0,
                f"{jurisdiction}|{application_number}",
                created_by,
                now,
                now,
            ),
        )
        application_id = cursor.lastrowid
        self.connection.execute(
            "UPDATE patent_applications SET family_id=? WHERE id=?",
            (application_id, application_id),
        )
        return self.get(application_id)

    def assign_family(self, application_ids: Iterable[int], family_id: int, now: str) -> None:
        ids = sorted(set(application_ids))
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.connection.execute(
            f"UPDATE patent_applications SET family_id=?,version=version+1,updated_at=? WHERE id IN ({placeholders})",
            (family_id, now, *ids),
        )

    def mark_merged(self, application_ids: Iterable[int], target_id: int, now: str) -> None:
        ids = sorted(set(application_ids))
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.connection.execute(
            f"""UPDATE patent_applications
                SET application_status='merged',merged_into_id=?,family_id=?,version=version+1,updated_at=?
                WHERE id IN ({placeholders})""",
            (target_id, target_id, now, *ids),
        )


class FamilyLinkRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, link_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM patent_family_links WHERE id=?", (link_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("家族关系不存在")
        return dict(row)

    def find_active(self, child_id: int, parent_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM patent_family_links
               WHERE child_application_id=? AND parent_application_id=? AND status='active'""",
            (child_id, parent_id),
        ).fetchone()
        return dict(row) if row else None

    def list_for_family(self, family_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT l.* FROM patent_family_links l
               WHERE l.child_application_id IN (SELECT id FROM patent_applications WHERE family_id=?)
                  OR l.parent_application_id IN (SELECT id FROM patent_applications WHERE family_id=?)
               ORDER BY l.id""",
            (family_id, family_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_for_application(self, application_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM patent_family_links
               WHERE child_application_id=? OR parent_application_id=?
               ORDER BY id""",
            (application_id, application_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_for_applications(self, application_ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = sorted(set(application_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"""SELECT * FROM patent_family_links
                WHERE child_application_id IN ({placeholders})
                   OR parent_application_id IN ({placeholders})
                ORDER BY id""",
            (*ids, *ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def create(self, data: dict[str, Any], created_by: int | None, change_request_id: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO patent_family_links(
                   child_application_id,parent_application_id,relation_type,claimed_priority_date,
                   status,change_request_id,created_by,created_at
               ) VALUES(?,?,?,?,'active',?,?,?)""",
            (
                data["child_application_id"],
                data["parent_application_id"],
                data["relation_type"],
                data.get("claimed_priority_date"),
                change_request_id,
                created_by,
                now,
            ),
        )
        return self.get(cursor.lastrowid)

    def revoke(self, link_id: int, reason: str, revoked_by: int | None, now: str) -> dict[str, Any]:
        updated = self.connection.execute(
            """UPDATE patent_family_links
               SET status='revoked',revoke_reason=?,revoked_at=?,revoked_by=?
               WHERE id=? AND status='active'""",
            (reason, now, revoked_by, link_id),
        )
        if updated.rowcount != 1:
            raise ConflictError("关系已被撤销或取代，不能重复撤销")
        return self.get(link_id)

    def supersede_for_child(self, child_id: int, link_id: int, now: str) -> None:
        self.connection.execute(
            """UPDATE patent_family_links SET status='superseded',superseded_by_link_id=?
               WHERE child_application_id=? AND status='active' AND id<>?""",
            (link_id, child_id, link_id),
        )


class FamilyChangeRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, request_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM patent_family_change_requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("家族变更提案不存在")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["decisions"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT * FROM patent_family_change_decisions WHERE request_id=? ORDER BY id",
                (request_id,),
            ).fetchall()
        ]
        return result

    def list_pending(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM patent_family_change_requests WHERE status='pending' ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_pending_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM patent_family_change_requests WHERE idempotency_key=? AND status='pending'",
            (key,),
        ).fetchone()
        return dict(row) if row else None

    def create(
        self,
        *,
        change_type: str,
        payload: dict[str, Any],
        requested_by: int,
        required_approvals: int,
        now: str,
        change_code: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO patent_family_change_requests(
                   change_code,change_type,status,payload_json,requested_by,
                   required_approvals,idempotency_key,created_at,updated_at
               ) VALUES(?,?,'pending',?,?,?,?,?,?)""",
            (
                change_code,
                change_type,
                json.dumps(payload, ensure_ascii=False),
                requested_by,
                required_approvals,
                idempotency_key,
                now,
                now,
            ),
        )
        return self.get(cursor.lastrowid)

    def add_decision(
        self, request_id: int, approver_user_id: int, decision: str, comment: str, now: str
    ) -> None:
        self.connection.execute(
            """INSERT INTO patent_family_change_decisions(request_id,approver_user_id,decision,comment,decided_at)
               VALUES(?,?,?,?,?)""",
            (request_id, approver_user_id, decision, comment, now),
        )

    def set_status(self, request_id: int, status: str, now: str, *, execution_error: str | None = None) -> None:
        decided = status in {"approved", "rejected", "cancelled"}
        self.connection.execute(
            """UPDATE patent_family_change_requests
               SET status=?,execution_error=COALESCE(?,execution_error),
                   decided_at=CASE WHEN ?=1 THEN ? ELSE decided_at END,
                   executed_at=CASE WHEN ?='executed' THEN ? ELSE executed_at END,
                   updated_at=?
               WHERE id=?""",
            (status, execution_error, 1 if decided else 0, now, status, now, now, request_id),
        )


class FamilyEventRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def append(
        self,
        *,
        event_type: str,
        now: str,
        family_id: int | None = None,
        application_id: int | None = None,
        link_id: int | None = None,
        change_request_id: int | None = None,
        actor_user_id: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """INSERT INTO patent_family_events(
                   family_id,application_id,link_id,change_request_id,event_type,
                   actor_user_id,details_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                family_id,
                application_id,
                link_id,
                change_request_id,
                event_type,
                actor_user_id,
                json.dumps(details or {}, ensure_ascii=False),
                now,
            ),
        )
        return cursor.lastrowid

    def list_for_family(self, family_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM patent_family_events WHERE family_id=? ORDER BY id",
            (family_id,),
        ).fetchall()
        return self._decode(rows)

    def list_for_application(self, application_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM patent_family_events WHERE application_id=? ORDER BY id",
            (application_id,),
        ).fetchall()
        return self._decode(rows)

    @staticmethod
    def _decode(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result
