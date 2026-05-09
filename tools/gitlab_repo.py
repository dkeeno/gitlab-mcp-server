import base64
import os
import subprocess
from urllib.parse import urlparse

import gitlab
from mcp.server.fastmcp import FastMCP
import config as cfg


def _gl():
    return gitlab.Gitlab(cfg.GITLAB_URL, private_token=cfg.GITLAB_TOKEN)


def _project(gl: gitlab.Gitlab, project_id: str):
    try:
        return gl.projects.get(project_id)
    except gitlab.exceptions.GitlabGetError as e:
        raise ValueError(f"Project '{project_id}' not found: {e}")


def register(mcp: FastMCP):

    @mcp.tool()
    def gitlab_list_projects(search: str = "", owned: bool = True) -> list[dict]:
        """List GitLab projects you have access to.

        Args:
            search: Optional name filter.
            owned: If True, only return projects you own.
        """
        gl = _gl()
        kwargs = {"owned": owned, "order_by": "last_activity_at", "per_page": 50}
        if search:
            kwargs["search"] = search
        projects = gl.projects.list(**kwargs)
        return [
            {
                "id": p.id,
                "path_with_namespace": p.path_with_namespace,
                "visibility": p.visibility,
                "default_branch": p.default_branch,
                "last_activity_at": p.last_activity_at,
                "web_url": p.web_url,
            }
            for p in projects
        ]

    @mcp.tool()
    def gitlab_get_project(project_id: str) -> dict:
        """Get detailed information about a GitLab project.

        Args:
            project_id: Project ID (integer) or path e.g. 'namespace/repo'.
        """
        gl = _gl()
        p = _project(gl, project_id)
        return {
            "id": p.id,
            "path_with_namespace": p.path_with_namespace,
            "description": p.description,
            "visibility": p.visibility,
            "default_branch": p.default_branch,
            "ssh_url_to_repo": p.ssh_url_to_repo,
            "http_url_to_repo": p.http_url_to_repo,
            "web_url": p.web_url,
            "ci_config_path": getattr(p, "ci_config_path", ".gitlab-ci.yml") or ".gitlab-ci.yml",
            "open_issues_count": p.open_issues_count,
        }

    @mcp.tool()
    def gitlab_create_project(
        name: str,
        namespace: str = "",
        description: str = "",
        visibility: str = "private",
        initialize_with_readme: bool = True,
        default_branch: str = "main",
    ) -> dict:
        """Create a new GitLab repository.

        Args:
            name: Project name.
            namespace: Group path to create under (empty = personal namespace).
            description: Project description.
            visibility: 'private', 'internal', or 'public'.
            initialize_with_readme: Create with a default README.
            default_branch: Default branch name.
        """
        gl = _gl()
        data: dict = {
            "name": name,
            "description": description,
            "visibility": visibility,
            "initialize_with_readme": initialize_with_readme,
            "default_branch": default_branch,
        }
        if namespace:
            try:
                group = gl.groups.get(namespace)
                data["namespace_id"] = group.id
            except gitlab.exceptions.GitlabGetError:
                raise ValueError(f"Namespace '{namespace}' not found.")
        p = gl.projects.create(data)
        return {"id": p.id, "path_with_namespace": p.path_with_namespace, "web_url": p.web_url}

    @mcp.tool()
    def gitlab_list_branches(project_id: str, search: str = "") -> list[dict]:
        """List all branches in a project with protection status and last commit info.

        Args:
            project_id: Project ID or path.
            search: Optional branch name filter.
        """
        gl = _gl()
        p = _project(gl, project_id)
        kwargs: dict = {"per_page": 100}
        if search:
            kwargs["search"] = search
        branches = p.branches.list(**kwargs)
        return [
            {
                "name": b.name,
                "protected": b.protected,
                "merged": b.merged,
                "default": b.default,
                "last_commit_id": b.commit["id"][:8],
                "last_commit_message": b.commit["message"].split("\n")[0],
                "last_commit_date": b.commit["committed_date"],
                "web_url": b.web_url,
            }
            for b in branches
        ]

    @mcp.tool()
    def gitlab_create_branch(project_id: str, branch_name: str, ref: str = "") -> dict:
        """Create a new branch from a ref.

        Recommended naming conventions:
          feature/<short-description>
          fix/<short-description>
          release/<version>
          hotfix/<issue>

        Args:
            project_id: Project ID or path.
            branch_name: New branch name.
            ref: Source branch or commit SHA (defaults to default branch).
        """
        gl = _gl()
        p = _project(gl, project_id)
        source = ref or p.default_branch
        b = p.branches.create({"branch": branch_name, "ref": source})
        return {
            "name": b.name,
            "ref": source,
            "web_url": b.web_url,
            "commit_id": b.commit["id"][:8],
        }

    @mcp.tool()
    def gitlab_delete_branch(project_id: str, branch_name: str) -> dict:
        """Delete a branch. Refuses to delete protected branches.

        Args:
            project_id: Project ID or path.
            branch_name: Branch to delete.
        """
        gl = _gl()
        p = _project(gl, project_id)
        b = p.branches.get(branch_name)
        if b.protected:
            raise ValueError(f"Branch '{branch_name}' is protected. Unprotect it first in GitLab settings.")
        p.branches.delete(branch_name)
        return {"deleted": branch_name, "project": project_id}

    @mcp.tool()
    def gitlab_clone_repo(
        project_id: str,
        destination: str = "",
        branch: str = "",
        depth: int = 0,
    ) -> dict:
        """Clone a GitLab repository to a local directory using `git clone`.

        Authenticates by injecting GITLAB_TOKEN as an oauth2 token in the clone URL.
        After clone completes, the origin URL is rewritten to remove the token, so the
        token is NOT persisted in the cloned repo's .git/config. Subsequent push/pull
        operations against the cloned repo will need their own credential setup
        (credential helper, SSH, or re-injected token in URL).

        Safety:
        - Refuses to overwrite a destination that exists and is non-empty
        - Sanitizes the token from any error output before raising
        - Does NOT include the token in the returned dict

        Args:
            project_id: Project ID (numeric) or full path (e.g. 'namespace/repo').
            destination: Absolute path where to clone. If empty, clones to a directory
                         named after the project's path-basename in the current working
                         directory. Use an absolute path to avoid surprises.
            branch: Optional branch/tag/ref to checkout after clone. If empty, the
                    repo's default branch is checked out.
            depth: Optional shallow-clone depth. 0 (default) means full history.
                   Use a small value (e.g. 1) for read-only inspection to save time/space.

        Returns:
            Dict with status, destination (absolute path), branch, project_path,
            web_url, and remote_url (the cleaned URL set on `origin` after clone).
        """
        # Resolve the project so we have its canonical http_url_to_repo and default_branch.
        # This call also serves as a permission check — if the token can't see the project,
        # we bail before invoking git.
        gl = _gl()
        p = _project(gl, project_id)

        # The clean (un-authenticated) URL we'll set on origin after a successful clone.
        clean_url = p.http_url_to_repo

        # Build the authenticated URL by injecting the token in the userinfo segment:
        #   https://oauth2:<TOKEN>@gitlab.com/group/project.git
        # GitLab accepts 'oauth2' as the username when a personal/group/project access
        # token is provided as the password.
        parsed = urlparse(clean_url)
        auth_url = f"{parsed.scheme}://oauth2:{cfg.GITLAB_TOKEN}@{parsed.netloc}{parsed.path}"

        # Resolve destination. If caller didn't pass one, derive it from the project's
        # path basename (e.g. 'sbx-02-cluster-iac') under the current working directory.
        # Always normalise to an absolute path so behaviour is independent of CWD changes
        # later in the conversation.
        if not destination:
            destination = os.path.join(os.getcwd(), p.path)
        destination = os.path.abspath(destination)

        # Refuse to clone into an existing non-empty directory. A common foot-gun is to
        # clone over an in-progress edit; we error early instead of merging into it.
        if os.path.isdir(destination) and os.listdir(destination):
            raise ValueError(
                f"Destination '{destination}' already exists and is non-empty. "
                f"Refusing to overwrite. Remove it or pick a different destination."
            )

        # Build the git clone command. We deliberately do NOT use `--quiet` so users
        # can see progress when they re-run by hand; we capture output for sanitisation.
        cmd = ["git", "clone"]
        if branch:
            cmd.extend(["--branch", branch])
        if depth and depth > 0:
            cmd.extend(["--depth", str(depth)])
        cmd.extend([auth_url, destination])

        # Run git clone with a generous timeout. Most clones finish in seconds; we set
        # 300s as a hard ceiling to avoid hanging the MCP if network is broken.
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"git clone timed out after 300s. Check network connectivity to "
                f"{parsed.netloc} and the repo size."
            )
        except FileNotFoundError:
            raise RuntimeError(
                "`git` binary not found in PATH. Install git or ensure PATH is set "
                "correctly for the MCP server's shell environment."
            )

        # If clone failed, sanitize the token out of stderr before raising — git will
        # echo the URL back in error messages, which would leak the token otherwise.
        if result.returncode != 0:
            sanitized_stderr = (result.stderr or "").replace(cfg.GITLAB_TOKEN, "***REDACTED***")
            sanitized_stdout = (result.stdout or "").replace(cfg.GITLAB_TOKEN, "***REDACTED***")
            raise RuntimeError(
                f"git clone failed (exit {result.returncode}).\n"
                f"stdout: {sanitized_stdout}\nstderr: {sanitized_stderr}"
            )

        # Clone succeeded. Rewrite the `origin` remote URL to the clean (un-authenticated)
        # form so the token is not persisted in the local .git/config. Without this step,
        # anyone reading .git/config could harvest the token.
        try:
            subprocess.run(
                ["git", "-C", destination, "remote", "set-url", "origin", clean_url],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as e:
            # Clone worked but URL rewrite failed — surface a warning but don't fail the
            # operation, since the clone is usable. Caller can manually fix with:
            #   git -C <destination> remote set-url origin <clean_url>
            return {
                "status": "cloned_with_warning",
                "destination": destination,
                "branch": branch or p.default_branch,
                "project_path": p.path_with_namespace,
                "web_url": p.web_url,
                "remote_url": auth_url.replace(cfg.GITLAB_TOKEN, "***REDACTED***"),
                "warning": (
                    f"Clone succeeded but failed to scrub token from origin URL: "
                    f"{e.stderr.strip()}. Run `git -C {destination} remote set-url "
                    f"origin {clean_url}` manually to fix."
                ),
            }

        return {
            "status": "cloned",
            "destination": destination,
            "branch": branch or p.default_branch,
            "project_path": p.path_with_namespace,
            "web_url": p.web_url,
            "remote_url": clean_url,
        }

    @mcp.tool()
    def gitlab_get_file(project_id: str, file_path: str, ref: str = "") -> dict:
        """Read a file from the repository at any branch or commit.

        Args:
            project_id: Project ID or path.
            file_path: Path to the file (e.g. 'src/main.py', '.gitlab-ci.yml').
            ref: Branch, tag, or commit SHA (defaults to default branch).
        """
        gl = _gl()
        p = _project(gl, project_id)
        ref = ref or p.default_branch
        f = p.files.get(file_path=file_path, ref=ref)
        content = base64.b64decode(f.content).decode("utf-8", errors="replace")
        return {
            "file_path": f.file_path,
            "ref": ref,
            "size": f.size,
            "last_commit_id": f.last_commit_id[:8],
            "content": content,
        }

    @mcp.tool()
    def gitlab_create_or_update_file(
        project_id: str,
        file_path: str,
        content: str,
        commit_message: str,
        branch: str = "",
        author_name: str = "",
        author_email: str = "",
    ) -> dict:
        """Create or update a single file and commit it.

        Args:
            project_id: Project ID or path.
            file_path: File path to create/update (e.g. '.gitlab-ci.yml').
            content: Full file content (as a string).
            commit_message: Commit message.
            branch: Target branch (defaults to default branch).
            author_name: Optional commit author name override.
            author_email: Optional commit author email override.
        """
        gl = _gl()
        p = _project(gl, project_id)
        branch = branch or p.default_branch
        try:
            f = p.files.get(file_path=file_path, ref=branch)
            f.content = content
            f.save(branch=branch, commit_message=commit_message)
            action = "updated"
        except gitlab.exceptions.GitlabGetError:
            data: dict = {
                "file_path": file_path,
                "branch": branch,
                "content": content,
                "commit_message": commit_message,
            }
            if author_name:
                data["author_name"] = author_name
            if author_email:
                data["author_email"] = author_email
            p.files.create(data)
            action = "created"
        return {"action": action, "file_path": file_path, "branch": branch}

    @mcp.tool()
    def gitlab_batch_commit(
        project_id: str,
        branch: str,
        commit_message: str,
        actions: list[dict],
    ) -> dict:
        """Commit multiple file changes atomically in a single commit.

        Args:
            project_id: Project ID or path.
            branch: Target branch.
            commit_message: Commit message.
            actions: List of action objects. Each must have:
                "action": "create" | "update" | "delete" | "move"
                "file_path": "path/to/file"
                "content": "file content" (for create/update)
                "previous_path": "old/path" (for move only)
        """
        gl = _gl()
        p = _project(gl, project_id)
        commit = p.commits.create({
            "branch": branch,
            "commit_message": commit_message,
            "actions": actions,
        })
        return {
            "id": commit.id[:8],
            "title": commit.title,
            "branch": branch,
            "web_url": commit.web_url,
        }

    @mcp.tool()
    def gitlab_list_mrs(
        project_id: str,
        state: str = "opened",
        target_branch: str = "",
    ) -> list[dict]:
        """List merge requests in a project.

        Args:
            project_id: Project ID or path.
            state: 'opened', 'closed', 'merged', or 'all'.
            target_branch: Filter by target branch name.
        """
        gl = _gl()
        p = _project(gl, project_id)
        kwargs: dict = {"state": state, "per_page": 50}
        if target_branch:
            kwargs["target_branch"] = target_branch
        mrs = p.mergerequests.list(**kwargs)
        return [
            {
                "iid": mr.iid,
                "title": mr.title,
                "state": mr.state,
                "source_branch": mr.source_branch,
                "target_branch": mr.target_branch,
                "author": mr.author["username"],
                "pipeline_status": (mr.head_pipeline or {}).get("status", "none"),
                "web_url": mr.web_url,
                "created_at": mr.created_at,
            }
            for mr in mrs
        ]

    @mcp.tool()
    def gitlab_create_mr(
        project_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        labels: list[str] = [],
        remove_source_branch: bool = True,
        squash: bool = False,
    ) -> dict:
        """Create a merge request.

        Args:
            project_id: Project ID or path.
            source_branch: Branch with your changes.
            target_branch: Branch to merge into (e.g. 'main', 'staging', 'develop').
            title: MR title.
            description: MR body — supports Markdown, include context and checklist.
            labels: Label names to apply.
            remove_source_branch: Delete source branch after merge.
            squash: Squash all commits into one on merge.
        """
        gl = _gl()
        p = _project(gl, project_id)
        mr = p.mergerequests.create({
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "labels": ",".join(labels),
            "remove_source_branch_after_merge": remove_source_branch,
            "squash": squash,
        })
        return {
            "iid": mr.iid,
            "title": mr.title,
            "state": mr.state,
            "web_url": mr.web_url,
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
        }

    @mcp.tool()
    def gitlab_get_mr_diff(project_id: str, mr_iid: int) -> list[dict]:
        """Show the file diff of a merge request.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID (iid, shown as !123 in GitLab UI).
        """
        gl = _gl()
        p = _project(gl, project_id)
        mr = p.mergerequests.get(mr_iid)
        diffs = mr.diffs.list()
        if not diffs:
            return []
        return [
            {
                "old_path": d["old_path"],
                "new_path": d["new_path"],
                "diff": d["diff"][:3000],
                "new_file": d["new_file"],
                "deleted_file": d["deleted_file"],
                "renamed_file": d["renamed_file"],
            }
            for d in diffs[0].diffs
        ]

    @mcp.tool()
    def gitlab_merge_mr(
        project_id: str,
        mr_iid: int,
        merge_when_pipeline_succeeds: bool = True,
        squash: bool = False,
        should_remove_source_branch: bool = True,
    ) -> dict:
        """Merge a merge request.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID (iid).
            merge_when_pipeline_succeeds: Hold merge until CI pipeline passes.
            squash: Squash commits on merge.
            should_remove_source_branch: Delete the source branch after merge.
        """
        gl = _gl()
        p = _project(gl, project_id)
        mr = p.mergerequests.get(mr_iid)
        if mr.state != "opened":
            raise ValueError(f"MR !{mr_iid} is '{mr.state}', cannot merge.")
        mr.merge(
            merge_when_pipeline_succeeds=merge_when_pipeline_succeeds,
            squash=squash,
            should_remove_source_branch=should_remove_source_branch,
        )
        mr = p.mergerequests.get(mr_iid)
        return {"iid": mr.iid, "state": mr.state, "web_url": mr.web_url}

    @mcp.tool()
    def gitlab_list_groups(search: str = "", owned: bool = True) -> list[dict]:
        """List GitLab groups and subgroups you have access to.

        Args:
            search: Optional name filter.
            owned: If True, only return groups you own.
        """
        gl = _gl()
        kwargs: dict = {"per_page": 50, "order_by": "name"}
        if owned:
            kwargs["owned"] = True
        if search:
            kwargs["search"] = search
        groups = gl.groups.list(**kwargs)
        return [
            {
                "id": g.id,
                "name": g.name,
                "full_path": g.full_path,
                "visibility": g.visibility,
                "parent_id": getattr(g, "parent_id", None),
                "web_url": g.web_url,
            }
            for g in groups
        ]

    @mcp.tool()
    def gitlab_create_group(
        name: str,
        parent_group: str = "",
        description: str = "",
        visibility: str = "private",
    ) -> dict:
        """Create a new GitLab group or subgroup.

        Args:
            name: Group name (e.g. 'Agentic-AI'). Path is auto-derived (lowercased, spaces → hyphens).
            parent_group: Parent group name or full path (e.g. 'test-pipeline-builder').
                          Leave empty to create a top-level group.
            description: Optional group description.
            visibility: 'private', 'internal', or 'public'.
        """
        gl = _gl()
        path = name.lower().replace(" ", "-")
        data: dict = {
            "name": name,
            "path": path,
            "description": description,
            "visibility": visibility,
        }
        if parent_group:
            groups = gl.groups.list(search=parent_group, get_all=True)
            parent = next(
                (g for g in groups if g.path.lower() == parent_group.lower().split("/")[-1]),
                None,
            )
            if not parent:
                raise ValueError(f"Parent group '{parent_group}' not found.")
            data["parent_id"] = parent.id

        group = gl.groups.create(data)
        return {
            "id": group.id,
            "name": group.name,
            "full_path": group.full_path,
            "visibility": group.visibility,
            "web_url": group.web_url,
        }
