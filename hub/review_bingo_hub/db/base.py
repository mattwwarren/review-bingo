"""Model imports for SQLModel metadata discovery.

Alembic autogenerate uses SQLModel.metadata. Importing this module registers all
table models by importing their modules, so keep new models listed here.
"""

from review_bingo_hub.models.activity_log import ActivityLog, ActivityLogArchive
from review_bingo_hub.models.dashboard_session import DashboardSession
from review_bingo_hub.models.document import Document
from review_bingo_hub.models.github_identity import GithubIdentity, IdentityRepoAccess
from review_bingo_hub.models.membership import Membership
from review_bingo_hub.models.organization import Organization
from review_bingo_hub.models.repo_policy import RepoPolicy
from review_bingo_hub.models.review_client import ReviewClient
from review_bingo_hub.models.review_job import ReviewJob
from review_bingo_hub.models.user import User

__all__ = [
    "ActivityLog",
    "ActivityLogArchive",
    "DashboardSession",
    "Document",
    "GithubIdentity",
    "IdentityRepoAccess",
    "Membership",
    "Organization",
    "RepoPolicy",
    "ReviewClient",
    "ReviewJob",
    "User",
]
