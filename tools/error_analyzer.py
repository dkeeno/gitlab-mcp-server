"""Error analysis, root-cause tracing, and remediation for GitLab pipelines and deployments."""
import re
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


_PATTERNS = [
    (r"ERROR: Job failed: exit code (\d+)", "job_exit_code"),
    (r"no such file or directory", "missing_file"),
    (r"command not found", "missing_command"),
    (r"Permission denied", "permission_denied"),
    (r"CERTIFICATE_VERIFY_FAILED|SSL certificate problem", "ssl_error"),
    (r"Could not resolve host", "dns_error"),
    (r"Connection refused|dial tcp.*connection refused", "connection_refused"),
    (r"image pull (access denied|not found)|manifest unknown|not found.*Pulling from", "image_pull_error"),
    (r"no space left on device", "disk_full"),
    (r"OOMKilled|signal: killed|memory limit exceeded", "oom_killed"),
    (r"CrashLoopBackOff", "crash_loop"),
    (r"Forbidden|RBAC.*denied|User.*cannot.*get|does not have.*permission", "rbac_error"),
    (r"lock file.*locked|state lock", "terraform_lock"),
    (r"Error: No value for required variable", "missing_tf_variable"),
    (r"Error acquiring the state lock", "terraform_state_lock"),
    (r"ARGOCD.*Sync.*failed|ComparisonError|SyncFailed", "argocd_sync_failed"),
    (r"ImagePullBackOff", "image_pull_backoff"),
    (r"Pending.*Unschedulable|No nodes are available", "k8s_scheduling"),
    (r"secret.*not found|unable to get.*secret", "missing_secret"),
    (r"GITLAB_TOKEN|CI_.*undefined|variable.*not set|getenv.*empty", "missing_ci_variable"),
    (r"pip install.*error|npm.*err!|go: .*cannot find", "dependency_error"),
    (r"tests? failed|FAILED.*tests?|AssertionError|assert.*failed", "test_failure"),
]

_REMEDIATION = {
    "missing_ci_variable": {
        "title": "Missing CI/CD variable",
        "steps": [
            "Go to GitLab project → Settings → CI/CD → Variables",
            "Add the missing variable with 'Masked' and 'Protected' enabled",
            "Use gitlab_create_ci_variable tool to add it programmatically",
            "Retry the pipeline after adding the variable",
        ],
    },
    "image_pull_error": {
        "title": "Docker image pull failure",
        "steps": [
            "Check the image name and tag are correct",
            "Ensure CI_REGISTRY_USER / CI_REGISTRY_PASSWORD are set for private registries",
            "Add `docker login` to the job's before_script",
            "For GitLab registry: use $CI_REGISTRY_USER, $CI_REGISTRY_PASSWORD, $CI_REGISTRY",
        ],
    },
    "image_pull_backoff": {
        "title": "Kubernetes ImagePullBackOff",
        "steps": [
            "Check the image tag exists in the registry",
            "Verify the K8s imagePullSecret is configured and valid",
            "Run: kubectl get secret <name> -n <namespace> to inspect",
            "Recreate the secret: kubectl create secret docker-registry ...",
        ],
    },
    "rbac_error": {
        "title": "RBAC / permission denied",
        "steps": [
            "Check the ServiceAccount bound to the pod has the required ClusterRole/Role",
            "Run: kubectl auth can-i <verb> <resource> --as=system:serviceaccount:<ns>:<sa>",
            "Apply a RoleBinding or ClusterRoleBinding granting the missing permission",
            "For IRSA on EKS: verify the IAM role trust policy and annotation on ServiceAccount",
        ],
    },
    "terraform_state_lock": {
        "title": "Terraform state lock",
        "steps": [
            "Another Terraform job may be running — check active pipeline jobs",
            "If the lock is stale, force-unlock: terraform force-unlock <lock-id>",
            "For S3 backend: check DynamoDB lock table for stale entries",
            "Never force-unlock while a legitimate apply is in progress",
        ],
    },
    "missing_tf_variable": {
        "title": "Missing Terraform variable",
        "steps": [
            "Add TF_VAR_<name>=value as a GitLab CI/CD masked variable",
            "Or add it to a terraform.tfvars file (do NOT commit secrets there)",
            "Use gitlab_create_ci_variable with key=TF_VAR_<name>",
        ],
    },
    "argocd_sync_failed": {
        "title": "ArgoCD sync failure",
        "steps": [
            "Run: kubectl -n argocd get app <name> -o yaml | grep -A10 conditions",
            "Check for resource conflicts: kubectl get events -n <app-namespace>",
            "Verify the GitLab repo is accessible from ArgoCD (check repo connection)",
            "Force sync with prune: argocd app sync <name> --prune --force",
            "If hook timeout: check PostSync hooks and increase timeout in the manifest",
        ],
    },
    "oom_killed": {
        "title": "Container killed due to out-of-memory",
        "steps": [
            "Increase memory limits in the Helm values or Deployment manifest",
            "Check memory usage with: kubectl top pods -n <namespace>",
            "Add a Vertical Pod Autoscaler (VPA) to auto-tune limits",
            "Profile the application for memory leaks if usage keeps growing",
        ],
    },
    "crash_loop": {
        "title": "CrashLoopBackOff — pod keeps restarting",
        "steps": [
            "Fetch crash logs: kubectl logs <pod> --previous -n <namespace>",
            "Check liveness probe configuration — it may be too aggressive",
            "Verify environment variables and config maps are correct",
            "Use 'kubectl describe pod <pod>' to see the exit code and last message",
        ],
    },
    "missing_secret": {
        "title": "Kubernetes secret not found",
        "steps": [
            "Create the secret: kubectl create secret generic <name> --from-literal=key=val",
            "Or use Sealed Secrets / External Secrets Operator for GitOps-safe secrets",
            "Verify namespace: the secret must be in the same namespace as the pod",
        ],
    },
    "k8s_scheduling": {
        "title": "Pod cannot be scheduled — no available nodes",
        "steps": [
            "Check node capacity: kubectl describe nodes | grep -A5 Allocatable",
            "Scale up the node group (via Terraform or EKS node group scaling)",
            "Check for taints/tolerations mismatches on the pod spec",
            "Enable cluster autoscaler if not already running",
        ],
    },
    "connection_refused": {
        "title": "Connection refused",
        "steps": [
            "Verify the target service/pod is running and healthy",
            "Check the port number is correct",
            "Inspect network policies that may block ingress/egress",
            "Verify service DNS resolves: kubectl exec -it <pod> -- nslookup <service>",
        ],
    },
    "ssl_error": {
        "title": "SSL/TLS certificate error",
        "steps": [
            "Verify the server's certificate is valid and not expired",
            "If using a private CA, add the CA certificate to the container's trust store",
            "Do NOT disable SSL verification in production — fix the cert instead",
        ],
    },
    "test_failure": {
        "title": "Test failures",
        "steps": [
            "Check the test report artifacts for failing test names and stack traces",
            "Run the failing tests locally to reproduce",
            "Use gitlab_get_job_logs to see the full test output",
        ],
    },
    "dependency_error": {
        "title": "Dependency installation failure",
        "steps": [
            "Check if the package exists and the version constraint is valid",
            "For npm: delete package-lock.json and run npm install",
            "For pip: ensure requirements.txt pins compatible versions",
            "Check for network access to package registries from the runner",
        ],
    },
    "job_exit_code": {
        "title": "Job exited with non-zero code",
        "steps": [
            "Check the full job log for the error message above the exit line",
            "The exit code indicates the specific failure mode",
            "Look for the last failing command in the script section",
        ],
    },
}


def _classify_log(log: str) -> list[str]:
    found = []
    for pattern, error_type in _PATTERNS:
        if re.search(pattern, log, re.IGNORECASE):
            found.append(error_type)
    return found or ["unknown"]


def register(mcp: FastMCP):

    @mcp.tool()
    def analyze_pipeline_failure(project_id: str, pipeline_id: int) -> dict:
        """Analyze a failed pipeline: fetch all failed job logs, classify errors, return root-cause report.

        Args:
            project_id: Project ID or path.
            pipeline_id: Failed pipeline ID.
        """
        gl = _gl()
        p = _project(gl, project_id)
        pipeline = p.pipelines.get(pipeline_id)
        jobs = p.jobs.list(pipeline_id=pipeline_id, per_page=100)
        failed_jobs = [j for j in jobs if j.status == "failed"]

        if not failed_jobs:
            return {
                "pipeline_id": pipeline_id,
                "status": pipeline.status,
                "message": "No failed jobs found. Pipeline may have been canceled.",
            }

        analyses = []
        for job in failed_jobs:
            try:
                log = job.trace().decode("utf-8", errors="replace")
            except Exception:
                log = ""
            lines = log.splitlines()
            tail = "\n".join(lines[-100:]) if len(lines) > 100 else log
            error_types = _classify_log(log)
            remediations = [_REMEDIATION.get(et, {"title": et, "steps": ["Review the job log for details."]}) for et in error_types]
            analyses.append({
                "job_id": job.id,
                "job_name": job.name,
                "stage": job.stage,
                "error_types": error_types,
                "log_tail": tail,
                "remediations": remediations,
            })

        primary_types = analyses[0]["error_types"] if analyses else ["unknown"]
        primary_remediation = analyses[0]["remediations"][0] if analyses else {}

        return {
            "pipeline_id": pipeline_id,
            "ref": pipeline.ref,
            "web_url": pipeline.web_url,
            "failed_job_count": len(failed_jobs),
            "primary_error": primary_types[0],
            "primary_remediation": primary_remediation,
            "job_analyses": analyses,
        }

    @mcp.tool()
    def suggest_fix(error_type: str) -> dict:
        """Get a detailed remediation guide for a known error type.

        Args:
            error_type: Error type string from analyze_pipeline_failure output.
                        Known types: missing_ci_variable, image_pull_error, rbac_error,
                        terraform_state_lock, argocd_sync_failed, oom_killed, crash_loop,
                        missing_secret, k8s_scheduling, connection_refused, ssl_error,
                        test_failure, dependency_error, missing_tf_variable, image_pull_backoff.
        """
        remedy = _REMEDIATION.get(error_type)
        if not remedy:
            return {
                "error_type": error_type,
                "known": False,
                "message": "Unknown error type. Use analyze_pipeline_failure to classify errors from logs.",
            }
        return {"error_type": error_type, "known": True, **remedy}

    @mcp.tool()
    def apply_fix_to_ci(
        project_id: str,
        fix_description: str,
        changes: list[dict],
        branch: str = "",
        commit_message: str = "",
    ) -> dict:
        """Apply a fix to one or more files and commit it to the repository.

        Use this after analyze_pipeline_failure and suggest_fix to commit the remediation.

        Args:
            project_id: Project ID or path.
            fix_description: Human-readable description of the fix being applied.
            changes: List of file changes. Each item:
                {
                  "file_path": ".gitlab-ci.yml",
                  "action": "update",
                  "content": "... new file content ..."
                }
            branch: Branch to commit fix to (defaults to default branch).
            commit_message: Commit message (auto-generated if empty).
        """
        gl = _gl()
        p = _project(gl, project_id)
        branch = branch or p.default_branch
        msg = commit_message or f"fix: {fix_description}"

        commit = p.commits.create({
            "branch": branch,
            "commit_message": msg,
            "actions": [
                {
                    "action": c.get("action", "update"),
                    "file_path": c["file_path"],
                    "content": c["content"],
                }
                for c in changes
            ],
        })
        return {
            "commit_id": commit.id[:8],
            "message": commit.title,
            "branch": branch,
            "web_url": commit.web_url,
            "files_changed": [c["file_path"] for c in changes],
        }

    @mcp.tool()
    def trace_deploy_failure(
        project_id: str,
        pipeline_id: int,
    ) -> dict:
        """Full deployment failure trace: correlates CI job logs + pipeline context into one report.

        Use this when a deployment pipeline fails. It identifies whether the failure is in:
        - The build/test stage (code issue)
        - The Terraform/provision stage (infra issue)
        - The deploy stage (ArgoCD update / GitOps issue)
        - The DAST / post-deploy stage

        Args:
            project_id: Project ID or path.
            pipeline_id: Pipeline ID of the failed deployment.
        """
        gl = _gl()
        p = _project(gl, project_id)
        pipeline = p.pipelines.get(pipeline_id)
        jobs = p.jobs.list(pipeline_id=pipeline_id, per_page=100)

        stage_results: dict = {}
        for job in jobs:
            stage = job.stage
            if stage not in stage_results:
                stage_results[stage] = {"status": "success", "jobs": []}
            if job.status == "failed":
                stage_results[stage]["status"] = "failed"
            try:
                log_raw = job.trace().decode("utf-8", errors="replace") if job.status in ("failed", "success") else ""
            except Exception:
                log_raw = ""
            error_types = _classify_log(log_raw) if job.status == "failed" else []
            stage_results[stage]["jobs"].append({
                "id": job.id,
                "name": job.name,
                "status": job.status,
                "error_types": error_types,
                "log_tail": "\n".join(log_raw.splitlines()[-50:]) if log_raw and job.status == "failed" else "",
            })

        first_failed_stage = next(
            (s for s in ["validate", "test", "security", "build", "provision", "deploy", "dast", "promote"]
             if stage_results.get(s, {}).get("status") == "failed"),
            None,
        )

        failed_jobs_in_stage = []
        remediation_steps: list[dict] = []
        if first_failed_stage:
            failed_jobs_in_stage = [j for j in stage_results[first_failed_stage]["jobs"] if j["status"] == "failed"]
            for j in failed_jobs_in_stage:
                for et in j["error_types"]:
                    r = _REMEDIATION.get(et)
                    if r:
                        remediation_steps.append({"error_type": et, **r})

        return {
            "pipeline_id": pipeline_id,
            "ref": pipeline.ref,
            "overall_status": pipeline.status,
            "web_url": pipeline.web_url,
            "first_failing_stage": first_failed_stage,
            "stage_summary": {
                s: {"status": v["status"], "job_count": len(v["jobs"])}
                for s, v in stage_results.items()
            },
            "failing_jobs": failed_jobs_in_stage,
            "remediation": remediation_steps,
        }
