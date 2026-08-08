"""Grid client endpoints: register, check in, check out.

Machines, not humans: clients authenticate with the bearer token minted at
registration, not the user auth stack. Admission is derived from GitHub: the
client obtains a user access token through the App's device flow and
presents it here; the hub reads identity and repo access from that token
and then discards it — no GitHub credential is persisted. In dev mode
(CLIENT_ENROLMENT_MODE=dev), a shared secret substitutes for the GitHub
token so the grid can be exercised offline.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from review_bingo_hub.db.session import SessionDep
from review_bingo_hub.models.review_client import (
    CheckInRequest,
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
from review_bingo_hub.services.github_identity_service import GithubIdentityServiceDep
from review_bingo_hub.services.identity_service import (
    DETAIL_UNKNOWN_CALLER,
    EnrolmentDeniedError,
    EnrolmentUnavailableError,
    PolicyCallerUnauthenticatedError,
    ScopedCaller,
    reattest_identity,
    resolve_enrolment_credential,
    resolve_scoped_caller,
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=DETAIL_UNKNOWN_CALLER)
    return client


ClientDep = Annotated[ReviewClient, Depends(get_current_client)]


async def get_scoped_caller(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ScopedCaller:
    """Resolve a *read* caller: a grid client's token, or a dashboard session's.

    A sibling of get_current_client rather than a widening of it, following the
    same reasoning get_enrolment_credential already documents: the header looks
    identical in all three cases and the thing inside it is not, so one function
    that "resolves whatever this bearer is" would be one function away from
    accepting any of them at any endpoint.
    """
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Client or session token required")
    try:
        return await resolve_scoped_caller(session, credentials.credentials)
    except PolicyCallerUnauthenticatedError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.detail) from exc


# Reads only. `PUT /policies/{owner}/{repo}` deliberately does NOT resolve
# through this dependency: it keeps going through api/policies.py's RepoAdminDep
# -> authorize_policy_write -> _resolve_caller_client, which accepts a
# registered client's credential and nothing else.
#
# The distinction is worth a sentence because it is invisible at the call site.
# Unifying "which bearer token authenticates a read" is a safe and useful
# widening — the two callers resolve to the same GitHub identity and are scoped
# by the same access set. Applying the same reasoning to a *write* would not be:
# a dashboard session is a browser tab someone left open, and repo policy is the
# one knob that decides which models may review a repo's code. Swapping
# ScopedCallerDep onto that endpoint out of habit would widen who can lower a
# model floor, and nothing at the call site would look wrong.
#
# So the swap is deliberately made to cost something: it requires deleting this
# comment first.
ScopedCallerDep = Annotated[ScopedCaller, Depends(get_scoped_caller)]


async def get_enrolment_credential(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """Extract the credential a caller is enrolling with.

    Deliberately a sibling of get_current_client rather than a reuse of it:
    the header looks identical but the thing inside it is not a hub-minted
    client token, and one function that "resolves whatever this bearer is"
    would be one function away from accepting either at either endpoint.
    """
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Enrolment credential required")
    return credentials.credentials


EnrolmentCredentialDep = Annotated[str, Depends(get_enrolment_credential)]


@router.post("", response_model=ReviewClientRegistered, status_code=status.HTTP_201_CREATED)
async def register_client_endpoint(
    payload: ReviewClientCreate,
    session: SessionDep,
    credential: EnrolmentCredentialDep,
    github: GithubIdentityServiceDep,
) -> ReviewClientRegistered:
    """Join the grid. The returned token is shown exactly once — store it.

    Admission is resolved before anything is written, so a refused enrolment
    leaves no client row behind.
    """
    try:
        identity_id = await resolve_enrolment_credential(session, credential, github)
    except EnrolmentDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.detail) from exc
    except EnrolmentUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.detail) from exc
    client, token = await register_client(session, payload, identity_id=identity_id)
    await session.commit()
    return ReviewClientRegistered(client=ReviewClientRead.model_validate(client), token=token)


@router.post("/check-in", response_model=ReviewClientRead)
async def check_in_endpoint(
    session: SessionDep,
    client: ClientDep,
    github: GithubIdentityServiceDep,
    payload: CheckInRequest | None = None,
) -> ReviewClientRead:
    """Declare availability: 'I've got tokens — plug me in for a round.'

    An optional `github_token` in the body re-attests the caller's cached
    GitHub repo access, refreshing `access_refreshed_at`. Omit it (or send a
    falsy value) and check-in is unchanged — a plain heartbeat, no GitHub
    call. A token that fails validation, or resolves to a different GitHub
    account than this client is already linked to, is rejected with 401 and
    check-in does not proceed.
    """
    if payload is not None and payload.github_token:
        try:
            await reattest_identity(session, client, github_token=payload.github_token, github=github)
        except EnrolmentUnavailableError:
            # Lenient on purpose, unlike enrolment's 503. Refusing a first
            # enrolment over a GitHub outage costs the caller nothing it had;
            # refusing every check-in would take the whole grid offline for the
            # length of a GitHub incident.
            #
            # Safe only because of two things, and both must stay true. A
            # revoked or rejected token arrives as EnrolmentDeniedError below,
            # never here, so the fail-closed path is untouched. And this branch
            # writes nothing at all — in particular it must never bump
            # access_refreshed_at, which would let a swallowed outage extend the
            # staleness clock past a revocation the hub never got to see.
            pass
        except EnrolmentDeniedError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.detail) from exc
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
async def list_clients_endpoint(
    session: SessionDep,
    caller: ScopedCallerDep,
    offset: int = 0,
    limit: int = 100,
) -> list[ReviewClientRead]:
    """Roster for the dashboard: who's plugged in on repos this caller can see."""
    clients = await list_clients(
        session, identity_id=caller.identity_id, own_client_id=caller.client_id, offset=offset, limit=limit
    )
    return [ReviewClientRead.model_validate(c) for c in clients]
