import time
import gitlab
from mcp.server.fastmcp import FastMCP
import config as cfg


def _gl():
    return gitlab.Gitlab(cfg.GITLAB_URL, private_token=cfg.GITLAB_TOKEN)


def _project(gl, project_id):
    try:
        return gl.projects.get(project_id)
    except gitlab.exceptions.GitlabGetError as e:
        raise ValueError(f"Project '{project_id}' not found: {e}")


def register(mcp: FastMCP):

    @mcp.tool()
    def gitlab_trigger_pipeline(
        project_id: str,
        ref: str = "",
        variables: dict = {},
    ) -> dict:
        """Trigger a GitLab CI/CD pipeline on a branch.

        Args:
            project_id: Project ID or path.
            ref: Branch or tag to run pipeline on (defaults to default branch).
            variables: Optional key-value pairs passed as pipeline variables.
        """
        gl = _gl()
        p = _project(gl, project_id)
        ref = ref or p.default_branch
        var_list = [{"key": k, "value": v} for k, v in variables.items()]
        pipeline = p.pipelines.create({"ref": ref, "variables": var_list})
        return {
            "id": pipeline.id,
            "ref": pipeline.ref,
            "status": pipeline.status,
            "web_url": pipeline.web_url,
            "created_at": pipeline.created_at,
        }

    @mcp.tool()
    def gitlab_list_pipelines(
        project_id: str,
        ref: str = "",
        status: str = "",
        limit: int = 10,
    ) -> list[dict]:
        """List recent pipelines in a project.

        Args:
            project_id: Project ID or path.
            ref: Filter by branch or tag.
            status: Filter by status: 'running', 'pending', 'success', 'failed', 'canceled'.
            limit: Maximum number of pipelines to return (default 10).
        """
        gl = _gl()
        p = _project(gl, project_id)
        kwargs: dict = {"per_page": limit, "order_by": "id", "sort": "desc"}
        if ref:
            kwargs["ref"] = ref
        if status:
            kwargs["status"] = status
        pipelines = p.pipelines.list(**kwargs)
        # NOTE: pipelines.list() returns "lite" objects that do NOT include `duration`
        # (and some other fields) — accessing them raises AttributeError. Use getattr
        # with default to stay safe; full details are available via gitlab_get_pipeline_status.
        return [
            {
                "id": pl.id,
                "status": pl.status,
                "ref": pl.ref,
                "sha": (pl.sha[:8] if getattr(pl, "sha", None) else None),
                "web_url": getattr(pl, "web_url", None),
                "created_at": getattr(pl, "created_at", None),
                "source": getattr(pl, "source", None),
            }
            for pl in pipelines
        ]

    @mcp.tool()
    def gitlab_get_pipeline_status(
        project_id: str,
        pipeline_id: int,
        wait: bool = False,
        poll_interval: int = 15,
        timeout: int = 1800,
    ) -> dict:
        """Get pipeline status and all job statuses. Optionally poll until completion.

        Args:
            project_id: Project ID or path.
            pipeline_id: Pipeline ID (integer).
            wait: If True, poll until pipeline finishes or timeout is reached.
            poll_interval: Seconds between polls (default 15).
            timeout: Max seconds to wait (default 1800 = 30 min).
        """
        gl = _gl()
        p = _project(gl, project_id)
        elapsed = 0
        terminal = {"success", "failed", "canceled", "skipped"}

        while True:
            pipeline = p.pipelines.get(pipeline_id)
            jobs = p.jobs.list(pipeline_id=pipeline_id, per_page=100)
            job_list = [
                {
                    "id": j.id,
                    "name": j.name,
                    "stage": j.stage,
                    "status": j.status,
                    "duration": j.duration,
                    "web_url": j.web_url,
                }
                for j in jobs
            ]
            result = {
                "id": pipeline.id,
                "status": pipeline.status,
                "ref": pipeline.ref,
                "sha": pipeline.sha[:8],
                "web_url": pipeline.web_url,
                "duration": pipeline.duration,
                "jobs": job_list,
            }
            if not wait or pipeline.status in terminal or elapsed >= timeout:
                return result
            time.sleep(poll_interval)
            elapsed += poll_interval

    @mcp.tool()
    def gitlab_get_pipeline_jobs(project_id: str, pipeline_id: int) -> list[dict]:
        """List all jobs in a pipeline with their status and stage.

        Args:
            project_id: Project ID or path.
            pipeline_id: Pipeline ID.
        """
        gl = _gl()
        p = _project(gl, project_id)
        jobs = p.jobs.list(pipeline_id=pipeline_id, per_page=100)
        return [
            {
                "id": j.id,
                "name": j.name,
                "stage": j.stage,
                "status": j.status,
                "runner": (j.runner or {}).get("description", "shared"),
                "duration": j.duration,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
                "web_url": j.web_url,
            }
            for j in jobs
        ]

    @mcp.tool()
    def gitlab_get_job_logs(project_id: str, job_id: int, tail_lines: int = 200) -> dict:
        """Fetch logs from a CI/CD job. Returns the last N lines.

        Args:
            project_id: Project ID or path.
            job_id: Job ID (integer).
            tail_lines: Number of lines from the end to return (default 200, 0 = all).
        """
        gl = _gl()
        p = _project(gl, project_id)
        job = p.jobs.get(job_id)
        log = job.trace().decode("utf-8", errors="replace")
        lines = log.splitlines()
        if tail_lines and len(lines) > tail_lines:
            lines = lines[-tail_lines:]
        return {
            "job_id": job_id,
            "job_name": job.name,
            "status": job.status,
            "stage": job.stage,
            "log": "\n".join(lines),
            "total_lines": len(log.splitlines()),
        }

    @mcp.tool()
    def gitlab_cancel_pipeline(project_id: str, pipeline_id: int) -> dict:
        """Cancel a running pipeline.

        Args:
            project_id: Project ID or path.
            pipeline_id: Pipeline ID to cancel.
        """
        gl = _gl()
        p = _project(gl, project_id)
        pipeline = p.pipelines.get(pipeline_id)
        pipeline.cancel()
        return {"pipeline_id": pipeline_id, "action": "canceled"}

    @mcp.tool()
    def gitlab_retry_pipeline(project_id: str, pipeline_id: int) -> dict:
        """Retry failed jobs in a pipeline.

        Args:
            project_id: Project ID or path.
            pipeline_id: Pipeline ID to retry.
        """
        gl = _gl()
        p = _project(gl, project_id)
        pipeline = p.pipelines.get(pipeline_id)
        pipeline.retry()
        return {"pipeline_id": pipeline_id, "action": "retried"}

    @mcp.tool()
    def gitlab_create_ci_variable(
        project_id: str,
        key: str,
        value: str,
        masked: bool = True,
        protected: bool = True,
        environment_scope: str = "*",
        variable_type: str = "env_var",
    ) -> dict:
        """Create or update a CI/CD variable (secret) in a project.

        Args:
            project_id: Project ID or path.
            key: Variable name (e.g. 'AWS_ACCESS_KEY_ID').
            value: Variable value.
            masked: Mask value in job logs (recommended for secrets).
            protected: Only expose to protected branches/tags.
            environment_scope: Scope ('*' = all environments, 'production', 'staging', etc.).
            variable_type: 'env_var' or 'file'.
        """
        gl = _gl()
        p = _project(gl, project_id)
        try:
            var = p.variables.get(key)
            var.value = value
            var.masked = masked
            var.protected = protected
            var.environment_scope = environment_scope
            var.save()
            action = "updated"
        except gitlab.exceptions.GitlabGetError:
            p.variables.create({
                "key": key,
                "value": value,
                "masked": masked,
                "protected": protected,
                "environment_scope": environment_scope,
                "variable_type": variable_type,
            })
            action = "created"
        return {"action": action, "key": key, "masked": masked, "protected": protected}

    @mcp.tool()
    def gitlab_list_ci_variables(project_id: str) -> list[dict]:
        """List all CI/CD variables for a project. Values are NOT returned for masked variables.

        Args:
            project_id: Project ID or path.
        """
        gl = _gl()
        p = _project(gl, project_id)
        variables = p.variables.list(per_page=100)
        return [
            {
                "key": v.key,
                "masked": v.masked,
                "protected": v.protected,
                "environment_scope": v.environment_scope,
                "variable_type": v.variable_type,
                "value": "***" if v.masked else v.value,
            }
            for v in variables
        ]

    @mcp.tool()
    def gitlab_delete_ci_variable(project_id: str, key: str) -> dict:
        """Delete a CI/CD variable from a project.

        Args:
            project_id: Project ID or path.
            key: Variable name to delete.
        """
        gl = _gl()
        p = _project(gl, project_id)
        p.variables.delete(key)
        return {"deleted": key, "project": project_id}

    @mcp.tool()
    def gitlab_get_pipeline_artifacts(
        project_id: str,
        job_id: int,
        artifact_path: str = "",
    ) -> dict:
        """Download artifacts from a completed job.

        Args:
            project_id: Project ID or path.
            job_id: Job ID to download artifacts from.
            artifact_path: Specific file path within the artifact zip (empty = list available).
        """
        gl = _gl()
        p = _project(gl, project_id)
        job = p.jobs.get(job_id)
        if not artifact_path:
            return {
                "job_id": job_id,
                "job_name": job.name,
                "status": job.status,
                "artifacts": job.artifacts or [],
                "artifacts_file": getattr(job, "artifacts_file", None),
            }
        content = job.artifact(artifact_path)
        return {
            "job_id": job_id,
            "artifact_path": artifact_path,
            "content": content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content),
        }

    @mcp.tool()
    def gitlab_list_group_variables(group_id: str) -> list[dict]:
        """List all CI/CD variables set at the group level.

        Group variables are inherited by every project in the group.
        Masked variable values are not returned.

        Args:
            group_id: Group ID (integer) or full group path (e.g. 'my-org/my-group').
        """
        gl = _gl()
        try:
            group = gl.groups.get(group_id)
        except gitlab.exceptions.GitlabGetError as e:
            raise ValueError(f"Group '{group_id}' not found: {e}")
        variables = group.variables.list(per_page=100)
        return [
            {
                "key": v.key,
                "masked": v.masked,
                "protected": v.protected,
                "environment_scope": v.environment_scope,
                "variable_type": v.variable_type,
                "value": "***" if v.masked else v.value,
            }
            for v in variables
        ]

    @mcp.tool()
    def gitlab_create_group_variable(
        group_id: str,
        key: str,
        value: str,
        masked: bool = True,
        protected: bool = True,
        environment_scope: str = "*",
        variable_type: str = "env_var",
    ) -> dict:
        """Create or update a CI/CD variable at the group level.

        Group variables are automatically available to all projects in the group,
        making them ideal for shared credentials (registry auth, cloud keys, etc.).

        Args:
            group_id: Group ID or full path (e.g. 'my-org/my-group').
            key: Variable name (e.g. 'AWS_ACCESS_KEY_ID').
            value: Variable value.
            masked: Mask value in job logs — recommended for all secrets.
            protected: Only expose to jobs on protected branches/tags.
            environment_scope: '*' = all environments, or 'production', 'staging', etc.
            variable_type: 'env_var' or 'file'.
        """
        gl = _gl()
        try:
            group = gl.groups.get(group_id)
        except gitlab.exceptions.GitlabGetError as e:
            raise ValueError(f"Group '{group_id}' not found: {e}")
        try:
            var = group.variables.get(key)
            var.value = value
            var.masked = masked
            var.protected = protected
            var.environment_scope = environment_scope
            var.save()
            action = "updated"
        except gitlab.exceptions.GitlabGetError:
            group.variables.create({
                "key": key,
                "value": value,
                "masked": masked,
                "protected": protected,
                "environment_scope": environment_scope,
                "variable_type": variable_type,
            })
            action = "created"
        return {
            "action": action,
            "key": key,
            "group": group_id,
            "masked": masked,
            "protected": protected,
            "environment_scope": environment_scope,
        }

    @mcp.tool()
    def gitlab_delete_group_variable(group_id: str, key: str) -> dict:
        """Delete a CI/CD variable from a group.

        Args:
            group_id: Group ID or full path.
            key: Variable name to delete.
        """
        gl = _gl()
        try:
            group = gl.groups.get(group_id)
        except gitlab.exceptions.GitlabGetError as e:
            raise ValueError(f"Group '{group_id}' not found: {e}")
        group.variables.delete(key)
        return {"deleted": key, "group": group_id}
