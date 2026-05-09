#!/usr/bin/env python3
"""GitLab DevOps MCP Server — global Claude Code integration for GitLab."""
import sys
import os

# Ensure the server directory is on sys.path so tool modules resolve `import config`
sys.path.insert(0, os.path.dirname(__file__))

from mcp.server.fastmcp import FastMCP
import config as cfg

mcp = FastMCP(
    "gitlab-devops",
    instructions=(
        "GitLab DevOps MCP server. Capabilities:\n"
        "- Repository and branch management (gitlab_list_projects, gitlab_create_branch, gitlab_batch_commit, gitlab_clone_repo, etc.)\n"
        "- CI/CD pipeline generation, triggering, and monitoring (generate_ci_config, gitlab_trigger_pipeline, gitlab_get_pipeline_status)\n"
        "- Security scanning: SAST, DAST, secret detection, dependency scan (add_security_stages, gitlab_get_security_report)\n"
        "- Pipeline error analysis and remediation (analyze_pipeline_failure, trace_deploy_failure, apply_fix_to_ci)\n"
        "- Local runner registration for private-network jobs (gitlab_register_local_runner)\n"
        "- Merge request management (gitlab_create_mr, gitlab_merge_mr)\n"
        "- Access token management — group, project, and personal (gitlab_list/create/revoke_*_access_token)\n"
        "\n"
        "Ownership model:\n"
        "- GitLab pipelines execute everything (builds, tests, Terraform, deployments)\n"
        "- ArgoCD (deployed via Terraform in GitLab CI) manages all environment deployments\n"
        "- This MCP server ONLY authors configs, monitors pipelines, and advises on fixes\n"
        "\n"
        "Runner selection guidance:\n"
        "- SHARED runners: standard builds, tests, linting\n"
        "- LOCAL runner (tagged 'local'): jobs needing private cluster access or internal network\n"
        "- ArgoCD: all production and environment deployments (GitOps, no runner needed)\n"
    ),
)


def _load_tools():
    from tools import gitlab_repo, gitlab_pipeline, ci_generator, gitlab_security, gitlab_runner, error_analyzer, gitlab_tokens
    gitlab_repo.register(mcp)
    gitlab_pipeline.register(mcp)
    ci_generator.register(mcp)
    gitlab_security.register(mcp)
    gitlab_runner.register(mcp)
    error_analyzer.register(mcp)
    gitlab_tokens.register(mcp)


if __name__ == "__main__":
    try:
        cfg.validate()
    except RuntimeError as e:
        print(f"[gitlab-devops] Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    _load_tools()
    mcp.run()
