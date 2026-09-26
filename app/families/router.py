from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.families.schemas import (
    ApplicationCreate,
    ChangeCreate,
    ChangeDecision,
    FamilyCreate,
    FamilyImport,
    MergeRehearse,
)
from app.families.service import (
    ApplicationService,
    FamilyChangeService,
    FamilyService,
    RelationQueryService,
)

router = APIRouter(prefix="/api/patent-families", tags=["专利家族"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_family(payload: FamilyCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return FamilyService(connection).create(principal, payload.model_dump())


@router.get("")
def list_families(principal: Principal = Depends(current_principal)):
    return FamilyService(get_connection()).list(principal)


@router.post("/import", status_code=status.HTTP_201_CREATED)
def import_family(payload: FamilyImport, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ApplicationService(connection).import_batch(principal, payload.model_dump())


@router.post("/applications", status_code=status.HTTP_201_CREATED)
def register_application(payload: ApplicationCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return ApplicationService(connection).register(principal, payload.model_dump())


@router.get("/applications/{application_id}")
def get_application(application_id: int, principal: Principal = Depends(current_principal)):
    return ApplicationService(get_connection()).detail(principal, application_id)


@router.get("/applications/{application_id}/lineage")
def application_lineage(application_id: int, principal: Principal = Depends(current_principal)):
    return ApplicationService(get_connection()).lineage(principal, application_id)


@router.post("/changes", status_code=status.HTTP_201_CREATED)
def create_change(payload: ChangeCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return FamilyChangeService(connection).create(principal, payload.model_dump())


@router.get("/changes")
def list_changes(
    state: str | None = Query(default=None),
    family_id: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return FamilyChangeService(get_connection()).list(principal, state, family_id)


@router.get("/changes/{change_id}")
def get_change(change_id: int, principal: Principal = Depends(current_principal)):
    return FamilyChangeService(get_connection()).detail(principal, change_id)


@router.post("/changes/{change_id}/decisions")
def decide_change(change_id: int, payload: ChangeDecision, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return FamilyChangeService(connection).decide(principal, change_id, payload.model_dump())


@router.post("/changes/{change_id}/apply")
def apply_change(change_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return FamilyChangeService(connection).apply(principal, change_id)


@router.post("/changes/{change_id}/cancel")
def cancel_change(change_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return FamilyChangeService(connection).cancel(principal, change_id)


@router.post("/merge/rehearse")
def rehearse_merge(payload: MergeRehearse, principal: Principal = Depends(current_principal)):
    return FamilyChangeService(get_connection()).rehearse_merge(
        principal, payload.source_family_id, payload.target_family_id,
        payload.link.model_dump() if payload.link else None,
    )


@router.get("/relations/{relation_id}/impact")
def relation_impact(relation_id: int, principal: Principal = Depends(current_principal)):
    return RelationQueryService(get_connection()).impact(principal, relation_id)


@router.get("/{family_id}")
def get_family(family_id: int, principal: Principal = Depends(current_principal)):
    return FamilyService(get_connection()).detail(principal, family_id)


@router.get("/{family_id}/topology")
def family_topology(family_id: int, principal: Principal = Depends(current_principal)):
    return FamilyService(get_connection()).topology(principal, family_id)


@router.get("/{family_id}/summary")
def family_summary(family_id: int, principal: Principal = Depends(current_principal)):
    return FamilyService(get_connection()).summary(principal, family_id)


@router.get("/{family_id}/events")
def family_events(family_id: int, principal: Principal = Depends(current_principal)):
    return FamilyService(get_connection()).event_log(principal, family_id)
