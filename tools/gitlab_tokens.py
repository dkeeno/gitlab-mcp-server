"""GitLab access token management — group, project, and personal access tokens.

Wraps GitLab API endpoints under /groups/:id/access_tokens, /projects/:id/access_tokens,
and /personal_access_tokens. Auth uses the same GITLAB_TOKEN as other tools — that token
must have sufficient scope/role to manage the target tokens (typically Owner on the group
or project, or admin for personal_access_tokens of other users).

Security notes:
- Created tokens return the actual token VALUE in the response. This is the only time GitLab
  exposes the value — store it immediately. Subsequent list calls return metadata only (no value).
- Revoke is destructive and irreversible. Confirm token_id carefully before calling.
"""
import gitlab
from mcp.server.fastmcp import FastMCP
import config as cfg


# GitLab role → numeric access level (used by access_tokens.create)
ACCESS_LEVEL = {
    "guest": 10,
    "reporter": 20,
    "developer": 30,
    "maintainer": 40,
    "owner": 50,
}

# Common token scopes — exposed for documentation; GitLab accepts any string the API recognises
KNOWN_SCOPES = {
    "api",
    "read_api",
    "read_user",
    "read_repository",
    "write_repository",
    "read_registry",
    "write_registry",
    "create_runner",
    "manage_runner",
    "k8s_proxy",
    "ai_features",
}


def _gl():
    return gitlab.Gitlab(cfg.GITLAB_URL, private_token=cfg.GITLAB_TOKEN)


def _group(gl: gitlab.Gitlab, group_id: str):
    try:
        return gl.groups.get(group_id)
    except gitlab.exceptions.GitlabGetError as e:
        raise ValueError(f"Group '{group_id}' not found: {e}")


def _project(gl: gitlab.Gitlab, project_id: str):
    try:
        return gl.projects.get(project_id)
    except gitlab.exceptions.GitlabGetError as e:
        raise ValueError(f"Project '{project_id}' not found: {e}")


def _resolve_access_level(role: str) -> int:
    role_lc = (role or "developer").lower()
    if role_lc not in ACCESS_LEVEL:
        raise ValueError(
            f"Invalid role '{role}'. Valid: {', '.join(ACCESS_LEVEL.keys())}"
        )
    return ACCESS_LEVEL[role_lc]


def _serialize_token(t) -> dict:
    """Serialize a token object to plain dict. Includes 'token' field only when present
    (i.e. on create responses, never on list/get)."""
    base = {
        "id": getattr(t, "id", None),
        "name": getattr(t, "name", None),
        "scopes": list(getattr(t, "scopes", []) or []),
        "active": getattr(t, "active", None),
        "revoked": getattr(t, "revoked", None),
        "created_at": getattr(t, "created_at", None),
        "expires_at": getattr(t, "expires_at", None),
        "last_used_at": getattr(t, "last_used_at", None),
        "user_id": getattr(t, "user_id", None),
        "access_level": getattr(t, "access_level", None),
    }
    token_value = getattr(t, "token", None)
    if token_value:
        base["token"] = token_value
        base["_warning"] = (
            "Token value is shown ONLY in this create response. "
            "Store it now — GitLab will not expose it again."
        )
    return base


def register(mcp: FastMCP):

    # ============================================================
    # GROUP ACCESS TOKENS
    # ============================================================

    @mcp.tool()
    def gitlab_list_group_access_tokens(group_id: str) -> list[dict]:
        """List all access tokens on a group. Token values are NOT returned — only metadata
        (id, name, scopes, expiry, active/revoked status). Use the id to revoke later.

        Args:
            group_id: Group ID (numeric) or full path (e.g. 'my-org/my-group').
        """
        gl = _gl()
        group = _group(gl, group_id)
        tokens = group.access_tokens.list(get_all=True)
        return [_serialize_token(t) for t in tokens]

    @mcp.tool()
    def gitlab_create_group_access_token(
        group_id: str,
        name: str,
        scopes: list[str],
        role: str = "developer",
        expires_at: str = "",
    ) -> dict:
        """Create a group access token. Returns the token VALUE in the 'token' field —
        capture it immediately, GitLab does not expose it again.

        Args:
            group_id: Group ID or full path.
            name: Token name (e.g. 'sbx-gitops-token').
            scopes: List of scopes (e.g. ['write_repository']). Common: api, read_api,
                    read_repository, write_repository, read_registry, write_registry.
            role: Member role granted by the token. One of: guest, reporter, developer,
                  maintainer, owner. Default: developer.
            expires_at: Expiry in 'YYYY-MM-DD' format. GitLab.com requires expiry on
                        free tier (max ~1 year). Empty = use GitLab default.
        """
        if not scopes:
            raise ValueError("scopes must not be empty (e.g. ['write_repository'])")
        gl = _gl()
        group = _group(gl, group_id)
        payload = {
            "name": name,
            "scopes": scopes,
            "access_level": _resolve_access_level(role),
        }
        if expires_at:
            payload["expires_at"] = expires_at
        new_token = group.access_tokens.create(payload)
        return _serialize_token(new_token)

    @mcp.tool()
    def gitlab_revoke_group_access_token(group_id: str, token_id: int) -> dict:
        """Revoke (delete) a group access token. IRREVERSIBLE — once revoked, the token
        cannot be reactivated; create a new one if needed. Get the token_id from
        gitlab_list_group_access_tokens first.

        Args:
            group_id: Group ID or full path the token belongs to.
            token_id: Numeric token ID (from gitlab_list_group_access_tokens output).
        """
        gl = _gl()
        group = _group(gl, group_id)
        group.access_tokens.delete(token_id)
        return {
            "status": "revoked",
            "group_id": group_id,
            "token_id": token_id,
        }

    # ============================================================
    # PROJECT ACCESS TOKENS
    # ============================================================

    @mcp.tool()
    def gitlab_list_project_access_tokens(project_id: str) -> list[dict]:
        """List all access tokens on a project. Token values are NOT returned — only metadata.

        Args:
            project_id: Project ID or full path (e.g. 'namespace/repo').
        """
        gl = _gl()
        project = _project(gl, project_id)
        tokens = project.access_tokens.list(get_all=True)
        return [_serialize_token(t) for t in tokens]

    @mcp.tool()
    def gitlab_create_project_access_token(
        project_id: str,
        name: str,
        scopes: list[str],
        role: str = "developer",
        expires_at: str = "",
    ) -> dict:
        """Create a project access token. Returns the token VALUE in the 'token' field —
        capture it immediately.

        Args:
            project_id: Project ID or full path.
            name: Token name.
            scopes: List of scopes (e.g. ['read_repository']).
            role: Member role: guest, reporter, developer, maintainer, owner. Default: developer.
            expires_at: 'YYYY-MM-DD' format; required on GitLab.com free tier.
        """
        if not scopes:
            raise ValueError("scopes must not be empty")
        gl = _gl()
        project = _project(gl, project_id)
        payload = {
            "name": name,
            "scopes": scopes,
            "access_level": _resolve_access_level(role),
        }
        if expires_at:
            payload["expires_at"] = expires_at
        new_token = project.access_tokens.create(payload)
        return _serialize_token(new_token)

    @mcp.tool()
    def gitlab_revoke_project_access_token(project_id: str, token_id: int) -> dict:
        """Revoke (delete) a project access token. IRREVERSIBLE.

        Args:
            project_id: Project ID or full path.
            token_id: Numeric token ID (from gitlab_list_project_access_tokens output).
        """
        gl = _gl()
        project = _project(gl, project_id)
        project.access_tokens.delete(token_id)
        return {
            "status": "revoked",
            "project_id": project_id,
            "token_id": token_id,
        }

    # ============================================================
    # PERSONAL ACCESS TOKENS
    # ============================================================

    @mcp.tool()
    def gitlab_list_personal_access_tokens() -> list[dict]:
        """List YOUR OWN personal access tokens (the user the GITLAB_TOKEN authenticates as).
        Token values are NOT returned — only metadata. Useful for auditing what tokens you
        have and revoking unused ones.
        """
        gl = _gl()
        tokens = gl.personal_access_tokens.list(get_all=True)
        return [_serialize_token(t) for t in tokens]

    @mcp.tool()
    def gitlab_revoke_personal_access_token(token_id: int) -> dict:
        """Revoke a personal access token by ID. IRREVERSIBLE. Get the token_id from
        gitlab_list_personal_access_tokens first. Be careful: revoking the token currently
        used as GITLAB_TOKEN will lock this MCP server out.

        Args:
            token_id: Numeric token ID (from gitlab_list_personal_access_tokens output).
        """
        gl = _gl()
        gl.personal_access_tokens.delete(token_id)
        return {
            "status": "revoked",
            "token_id": token_id,
        }
