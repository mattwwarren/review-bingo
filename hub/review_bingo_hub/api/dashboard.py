"""Serve the dashboard from the hub.

The page polls /jobs and /clients, so it has to share an origin with them —
opened from the filesystem, every poll would be a blocked cross-origin
request. Serving the file here keeps the demo to one process and no build
step; the dashboard itself stays a viewer with no privileged access.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

router = APIRouter(tags=["dashboard"])

# hub/review_bingo_hub/api/dashboard.py -> repo root -> dashboard/index.html
DASHBOARD_INDEX = Path(__file__).resolve().parents[3] / "dashboard" / "index.html"


@router.get("/dashboard", include_in_schema=False)
async def dashboard_page() -> FileResponse:
    """The command center: work arriving, clients plugged in, a job to pick."""
    if not DASHBOARD_INDEX.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard is not deployed ({DASHBOARD_INDEX} is missing from this install)",
        )
    return FileResponse(DASHBOARD_INDEX, media_type="text/html")
