"""API router composition for the service."""

from fastapi import APIRouter

from review_bingo_hub.api import (
    auth,
    clients,
    dashboard,
    events,
    health,
    jobs,
    ping,
    policies,
    users,
    webhooks,
)

router = APIRouter()
router.include_router(health.router)
router.include_router(ping.router)
router.include_router(users.router)

# review-bingo grid
router.include_router(webhooks.router)
router.include_router(auth.router)
router.include_router(clients.router)
router.include_router(jobs.router)
router.include_router(events.router)
router.include_router(policies.router)
router.include_router(dashboard.router)
