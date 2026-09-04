from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from relayagents.api.auth import current_principal, get_services, human_principal
from relayagents.core import approvals
from relayagents.core.models import ApprovalRow
from relayagents.tools.context import Principal, Services

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


def _out(r: ApprovalRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "status": r.status,
        "action": r.action,
        "action_type": r.action_type,
        "requester": r.requester_actor_id,
        "requested_of": r.requested_of,
        "created_at": r.created_at,
        "resolved_at": r.resolved_at,
        "resolved_by": r.resolved_by,
        "edited_action": r.edited_action,
        "event_id": r.event_id,
    }


@router.get("")
async def list_approvals(
    principal: Annotated[Principal, Depends(current_principal)],
    services: Annotated[Services, Depends(get_services)],
    status: str = "pending",
) -> list[dict[str, Any]]:
    async with services.db.session() as session:
        stmt = select(ApprovalRow).where(ApprovalRow.requested_of == principal.user_id)
        if status != "all":
            stmt = stmt.where(ApprovalRow.status == status)
        rows = (
            await session.scalars(stmt.order_by(ApprovalRow.created_at.desc()).limit(100))
        ).all()
    return [_out(r) for r in rows]


class ResolveIn(BaseModel):
    decision: str
    edited_action: str | None = None
    note: str | None = None


@router.post("/{approval_id}/resolve")
async def resolve_approval(
    approval_id: str,
    body: ResolveIn,
    principal: Annotated[Principal, Depends(human_principal)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, Any]:
    """Resolve from the CLI/REST (Slack buttons are the usual path). Only the human it was requested of may resolve."""
    if body.decision not in ("approved", "denied"):
        raise HTTPException(400, "decision must be approved or denied")
    async with services.db.session() as session:
        try:
            row = await approvals.resolve(
                session,
                approval_id=approval_id,
                decision=body.decision,
                resolved_by=principal.user_id,
                edited_action=body.edited_action,
                note=body.note,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such approval") from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        await session.commit()
        return _out(row)
