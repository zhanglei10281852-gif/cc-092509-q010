from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.families.schemas import (
    ApplicationCreate,
    ApplicationPatch,
    ChangeDecision,
    LinkRelationRequest,
    MergeMembersRequest,
    MergePreviewRequest,
    RevokeRelationRequest,
)
from app.families.service import PatentFamilyService

router = APIRouter(prefix="/api/patent-families", tags=["专利家族"])


@router.post("/applications", status_code=status.HTTP_201_CREATED)
def import_application(payload: ApplicationCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PatentFamilyService(connection).import_application(principal, payload.model_dump())


@router.patch("/applications/{application_id}")
def patch_application(application_id: int, payload: ApplicationPatch, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PatentFamilyService(connection).patch_application(principal, application_id, payload.model_dump(exclude_unset=True))


@router.get("/applications/{application_id}")
def application_detail(application_id: int, principal: Principal = Depends(current_principal)):
    return PatentFamilyService(get_connection()).application_detail(principal, application_id)


@router.post("/changes/links", status_code=status.HTTP_201_CREATED)
def request_link(payload: LinkRelationRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PatentFamilyService(connection).request_link(principal, payload.model_dump())


@router.post("/changes/merges", status_code=status.HTTP_201_CREATED)
def request_merge(payload: MergeMembersRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PatentFamilyService(connection).request_merge(principal, payload.model_dump())


@router.post("/merge-preview")
def preview_merge(payload: MergePreviewRequest, principal: Principal = Depends(current_principal)):
    return PatentFamilyService(get_connection()).preview_merge(principal, payload.model_dump())


@router.post("/changes/revocations", status_code=status.HTTP_201_CREATED)
def request_revoke(payload: RevokeRelationRequest, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PatentFamilyService(connection).request_revoke(principal, payload.model_dump())


@router.get("/changes/pending")
def list_pending_changes(principal: Principal = Depends(current_principal)):
    return PatentFamilyService(get_connection()).list_pending_changes(principal)


@router.get("/changes/{request_id}")
def change_detail(request_id: int, principal: Principal = Depends(current_principal)):
    return PatentFamilyService(get_connection()).change_detail(principal, request_id)


@router.post("/changes/{request_id}/decisions")
def decide_change(request_id: int, payload: ChangeDecision, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return PatentFamilyService(connection).decide(principal, request_id, payload.model_dump())


@router.get("/{family_id}")
def family_summary(family_id: int, principal: Principal = Depends(current_principal)):
    return PatentFamilyService(get_connection()).family_summary(principal, family_id)


@router.get("/{family_id}/timeline")
def family_timeline(family_id: int, principal: Principal = Depends(current_principal)):
    return PatentFamilyService(get_connection()).family_timeline(principal, family_id)


@router.get("/links/{link_id}/impact")
def revoke_impact(link_id: int, principal: Principal = Depends(current_principal)):
    return PatentFamilyService(get_connection()).revoke_impact(principal, link_id)
