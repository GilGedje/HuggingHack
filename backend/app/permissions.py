"""What each role may do. The backend enforces this table and the web UI reads it
from /api/auth/status, so there is a single definition of every permission."""

from __future__ import annotations

from typing import Any


CAPABILITIES: dict[str, str] = {
    "models.browse": "Browse models, files, commit history, and pull models",
    "models.save": "Save models and organize personal collections",
    "tokens.manage": "Create personal API tokens",
    "repos.create": "Create repositories and upload models",
    "repos.edit_own": "Upload changes to repositories they own",
    "repos.edit_any": "Upload changes to any repository",
    "library.scan": "Rescan storage for new or changed models",
    "library.cache": "Restore S3 models to the local cache or remove local copies",
    "hub.download": "Browse and download models from Hugging Face",
    "runtimes.use": "Send models to Ollama or vLLM runtimes",
    "storage.view": "View every storage location and its models",
    "users.manage": "Manage accounts, roles, and access",
    "orgs.manage": "Create and delete organizations and manage any organization",
    "settings.view": "View server configuration",
}

_VIEWER = {"models.browse", "models.save", "tokens.manage"}
_MEMBER = _VIEWER | {
    "repos.create",
    "repos.edit_own",
    "library.scan",
    "library.cache",
    "hub.download",
}
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "viewer": frozenset(_VIEWER),
    "member": frozenset(_MEMBER),
    "admin": frozenset(CAPABILITIES),
}
ROLE_DESCRIPTIONS = {
    "admin": "Full control of the server, storage, runtimes, and accounts.",
    "member": "Uploads and maintains their own models.",
    "viewer": "Read-only: browses, saves, and pulls models, including shared uploads.",
}
ROLES = tuple(ROLE_CAPABILITIES)


def capabilities_for(user: dict[str, Any] | None) -> frozenset[str]:
    if not user:
        return frozenset()
    explicit = user.get("capabilities")
    if isinstance(explicit, (set, frozenset, list, tuple)):
        return frozenset(explicit)
    return ROLE_CAPABILITIES.get(user.get("role") or "", frozenset())


def can(user: dict[str, Any] | None, capability: str) -> bool:
    if capability not in CAPABILITIES:
        raise KeyError(f"Unknown capability {capability!r}")
    return capability in capabilities_for(user)


def permission_matrix(hidden: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The role table, leaving out capabilities for features this server has off."""
    return {
        "roles": [
            {
                "id": role,
                "description": ROLE_DESCRIPTIONS[role],
                "capabilities": sorted(ROLE_CAPABILITIES[role] - hidden),
            }
            for role in ROLES
        ],
        "capabilities": [
            {"id": capability, "description": description}
            for capability, description in CAPABILITIES.items()
            if capability not in hidden
        ],
    }
