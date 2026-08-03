"""API router composition for the service."""

from fastapi import APIRouter

from review_bingo_hub.api import (
    clients,
    documents,
    health,
    jobs,
    memberships,
    organizations,
    ping,
    policies,
    users,
    webhooks,
)

router = APIRouter()
router.include_router(health.router)
router.include_router(ping.router)
router.include_router(organizations.router)
router.include_router(users.router)
router.include_router(memberships.router)
router.include_router(documents.router)

# review-bingo grid
router.include_router(webhooks.router)
router.include_router(clients.router)
router.include_router(jobs.router)
router.include_router(policies.router)
