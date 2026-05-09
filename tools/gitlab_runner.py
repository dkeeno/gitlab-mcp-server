"""GitLab runner management — list, register local runner, monitor status."""
import subprocess
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
    def gitlab_list_runners(
        project_id: str = "",
        runner_type: str = "",
        status: str = "",
    ) -> list[dict]:
        """List GitLab runners.

        When to use local runners vs shared runners:
        - LOCAL runner: jobs needing private cluster access, sensitive credentials, private network
        - SHARED runner: standard builds, tests, linting, public repos
        - ArgoCD (not a runner): all production/environment deployments (GitOps)

        Args:
            project_id: Project ID or path to list project-specific runners. Empty = list all accessible.
            runner_type: Filter by 'instance_type', 'group_type', or 'project_type'.
            status: Filter by 'online', 'offline', 'stale', 'never_contacted'.
        """
        gl = _gl()
        if project_id:
            p = _project(gl, project_id)
            runners = p.runners.list(per_page=100)
        else:
            kwargs: dict = {"per_page": 100}
            if runner_type:
                kwargs["type"] = runner_type
            if status:
                kwargs["status"] = status
            runners = gl.runners.list(**kwargs)
        return [
            {
                "id": r.id,
                "name": getattr(r, "name", None) or getattr(r, "description", ""),
                "status": getattr(r, "status", "unknown"),
                "active": getattr(r, "active", True),
                "shared": getattr(r, "is_shared", None),
                "tags": getattr(r, "tag_list", []),
                "platform": getattr(r, "platform", None),
                "executor": getattr(r, "executor", None),
            }
            for r in runners
        ]

    @mcp.tool()
    def gitlab_register_local_runner(
        project_id: str,
        runner_name: str,
        tags: list[str] = ["local", "local-private-net"],
        executor: str = "shell",
        docker_image: str = "alpine:latest",
    ) -> dict:
        """Register the local gitlab-runner binary as a project runner.

        The local runner runs on THIS machine with outbound-only connectivity to gitlab.com.
        No inbound firewall rules are needed.

        Use cases:
        - Jobs that need kubectl/AWS/GCP access with credentials on this machine
        - Terraform jobs that access private VPC endpoints
        - Jobs that need access to internal services not reachable by shared runners

        Step 1: This tool creates the runner in GitLab and gets a registration token.
        Step 2: It runs `gitlab-runner register` with the shell executor.

        Args:
            project_id: Project ID or path to register runner for.
            runner_name: Display name for the runner.
            tags: CI job tags that will use this runner (e.g. ['local', 'local-k8s']).
            executor: Runner executor type: 'shell' or 'docker'.
            docker_image: Default Docker image (only used when executor='docker').
        """
        import requests

        gl = _gl()
        p = _project(gl, project_id)

        headers = {"PRIVATE-TOKEN": cfg.GITLAB_TOKEN, "Content-Type": "application/json"}
        create_url = f"{cfg.GITLAB_URL}/api/v4/user/runners"
        payload = {
            "runner_type": "project_type",
            "project_id": p.id,
            "description": runner_name,
            "tag_list": tags,
            "run_untagged": False,
            "locked": True,
        }
        resp = requests.post(create_url, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            return {"error": f"Failed to create runner: {resp.status_code} {resp.text}"}

        runner_data = resp.json()
        token = runner_data.get("token")
        runner_id = runner_data.get("id")

        if not token:
            return {"error": "GitLab did not return a runner token. Check permissions.", "response": runner_data}

        register_cmd = [
            "gitlab-runner", "register",
            "--non-interactive",
            f"--url={cfg.GITLAB_URL}",
            f"--token={token}",
            f"--name={runner_name}",
            f"--executor={executor}",
        ]
        if executor == "docker":
            register_cmd += [f"--docker-image={docker_image}"]

        result = subprocess.run(register_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return {
                "runner_id": runner_id,
                "token": token[:8] + "...",
                "register_error": result.stderr,
                "note": "Runner created in GitLab but local registration failed. Run the register command manually.",
                "manual_command": " ".join(register_cmd[:-1]) + " [token hidden]",
            }

        return {
            "runner_id": runner_id,
            "name": runner_name,
            "tags": tags,
            "executor": executor,
            "status": "registered",
            "note": "Runner is now listening. Tag CI jobs with one of the tags above to route them here.",
        }

    @mcp.tool()
    def gitlab_get_runner_status(runner_id: int) -> dict:
        """Check a runner's health, version, and last contact time.

        Args:
            runner_id: Numeric runner ID (from gitlab_list_runners).
        """
        gl = _gl()
        r = gl.runners.get(runner_id)
        return {
            "id": r.id,
            "name": getattr(r, "description", ""),
            "status": r.status,
            "active": r.active,
            "version": getattr(r, "version", None),
            "platform": getattr(r, "platform", None),
            "executor": getattr(r, "executor", None),
            "tags": r.tag_list,
            "contacted_at": getattr(r, "contacted_at", None),
        }

    @mcp.tool()
    def gitlab_update_runner_tags(runner_id: int, tags: list[str]) -> dict:
        """Update the tag list for a runner.

        Args:
            runner_id: Numeric runner ID.
            tags: New tag list (replaces existing tags).
        """
        gl = _gl()
        r = gl.runners.get(runner_id)
        r.tag_list = tags
        r.save()
        return {"runner_id": runner_id, "tags": tags}

    @mcp.tool()
    def gitlab_pause_runner(runner_id: int, pause: bool = True) -> dict:
        """Pause or unpause a runner.

        Args:
            runner_id: Numeric runner ID.
            pause: True to pause (disable), False to unpause (enable).
        """
        gl = _gl()
        r = gl.runners.get(runner_id)
        r.active = not pause
        r.save()
        return {"runner_id": runner_id, "active": r.active, "paused": pause}
