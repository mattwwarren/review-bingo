"""Grid client endpoints: register, check in, check out.

Machines, not humans: clients authenticate with the bearer token minted at
registration, not the user auth stack. Registration is open in v1 — anyone
who can reach the hub can join the grid (see PITCH.md open questions for
where this tightens up).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from review_bingo_hub.db.session import SessionDep
from review_bingo_hub.models.review_client import (
    ClientStatus,
    ReviewClient,
    ReviewClientCreate,
    ReviewClientRead,
    ReviewClientRegistered,
)
from review_bingo_hub.services.client_service import (
    get_client_by_token,
    list_clients,
    register_client,
    set_client_status,
)

router = APIRouter(prefix="/clients", tags=["clients"])

_bearer = HTTPBearer(auto_error=False)


async def get_current_client(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ReviewClient:
    """Resolve the calling grid client from its bearer token."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Client token required")
    client = await get_client_by_token(session, credentials.credentials)
    if client is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown client token")
    return client


ClientDep = Annotated[ReviewClient, Depends(get_current_client)]


@router.post("", response_model=ReviewClientRegistered, status_code=status.HTTP_201_CREATED)
async def register_client_endpoint(payload: ReviewClientCreate, session: SessionDep) -> ReviewClientRegistered:
    """Join the grid. The returned token is shown exactly once — store it."""
    client, token = await register_client(session, payload)
    await session.commit()
    return ReviewClientRegistered(client=ReviewClientRead.model_validate(client), token=token)


@router.post("/check-in", response_model=ReviewClientRead)
async def check_in_endpoint(session: SessionDep, client: ClientDep) -> ReviewClientRead:
    """Declare availability: 'I've got tokens — plug me in for a round.'"""
    client = await set_client_status(session, client, ClientStatus.CHECKED_IN)
    await session.commit()
    return ReviewClientRead.model_validate(client)


@router.post("/check-out", response_model=ReviewClientRead)
async def check_out_endpoint(session: SessionDep, client: ClientDep) -> ReviewClientRead:
    """Leave the grid; in-flight leases simply expire and requeue."""
    client = await set_client_status(session, client, ClientStatus.CHECKED_OUT)
    await session.commit()
    return ReviewClientRead.model_validate(client)


@router.get("", response_model=list[ReviewClientRead])
async def list_clients_endpoint(session: SessionDep, offset: int = 0, limit: int = 100) -> list[ReviewClientRead]:
    """Roster for the dashboard: who's plugged in, with what capabilities."""
    clients = await list_clients(session, offset=offset, limit=limit)
    return [ReviewClientRead.model_validate(c) for c in clients]
