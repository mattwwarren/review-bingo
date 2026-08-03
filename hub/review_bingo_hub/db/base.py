"""Model imports for SQLModel metadata discovery.

Alembic autogenerate uses SQLModel.metadata. Importing this module registers all
table models by importing their modules, so keep new models listed here.
"""

from review_bingo_hub.models.activity_log import ActivityLog, ActivityLogArchive
from review_bingo_hub.models.document import Document
from review_bingo_hub.models.membership import Membership
from review_bingo_hub.models.organization import Organization
from review_bingo_hub.models.user import User

__all__ = [
    "ActivityLog",
    "ActivityLogArchive",
    "Document",
    "Membership",
    "Organization",
    "User",
]
