"""Explicit, one-use permission approvals for Personal Assistant tool calls."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException

from api.services.permission_broker import get_permission_broker

router = APIRouter(prefix="/api/permissions", tags=["permissions"])


def _public(req):
    return req.public_dict()


def _require_approval_header(request_id: str, header_value: str | None) -> None:
    """Require a non-simple custom header on approval/denial POSTs.

    LifeOS is normally same-origin and has an explicit CORS allowlist. Requiring
    this header also prevents a random web page from approving a local request
    with a plain cross-origin HTML form, which cannot set custom headers.
    """
    supplied = header_value or ""
    if not hmac.compare_digest(supplied, request_id):
        raise HTTPException(status_code=403, detail="Missing or invalid permission approval header")


@router.get("")
async def list_pending_permission_requests(conversation_id: str | None = None):
    broker = get_permission_broker()
    requests = (
        broker.pending_for_conversation(conversation_id)
        if conversation_id
        else broker.pending()
    )
    return {"requests": [_public(req) for req in requests]}


@router.get("/{request_id}")
async def get_permission_request(request_id: str):
    req = get_permission_broker().get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Permission request not found")
    return _public(req)


@router.post("/{request_id}/approve")
async def approve_permission_request(
    request_id: str,
    approval_header: str | None = Header(default=None, alias="X-LifeOS-Permission-Approval"),
):
    _require_approval_header(request_id, approval_header)
    broker = get_permission_broker()
    try:
        req = broker.approve(request_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Permission request not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _public(req)


@router.post("/{request_id}/deny")
async def deny_permission_request(
    request_id: str,
    approval_header: str | None = Header(default=None, alias="X-LifeOS-Permission-Approval"),
):
    _require_approval_header(request_id, approval_header)
    broker = get_permission_broker()
    try:
        req = broker.deny(request_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Permission request not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _public(req)
