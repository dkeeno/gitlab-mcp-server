# GitLab DevOps MCP Server

A Model Context Protocol (MCP) server that lets Claude Code (or any MCP client) operate GitLab end-to-end. ~50 typed tools covering the full DevOps lifecycle.

## What it does

| Category | Tools |
|---|---|
| **Repo & branch** | clone, batch commit, branch creation, file ops |
| **Pipelines** | trigger, monitor, retry, cancel |
| **CI generation** | generate `.gitlab-ci.yml` for common stacks (Node, Python, Go, generic) |
| **Security scans** | SAST, DAST, secret detection, dependency, container scanning |
| **Pipeline failure analysis** | log fetching, root-cause classification, suggested fix |
| **Runners** | register local runners, pause/unpause, retag |
| **Access tokens** | full lifecycle for personal / project / group tokens |
| **Merge requests** | create, list, merge, comment |

## Why this exists

GitLab automation is normally split across `glab`, the GitLab API, ad-hoc Python scripts, and CI YAML. This MCP server exposes everything as a single set of typed tools that an AI agent (or human via chat) can call.

## Install

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set GITLAB_TOKEN
```

Add to your MCP client config:

```json
{
  "mcpServers": {
    "gitlab-devops": {
      "command": "python3",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

## Usage examples

In any MCP client:

- "Generate a Terraform pipeline for project X"
- "Why did pipeline 12345 fail?" → reads logs, classifies, suggests fix
- "Run a SAST scan on the latest commit"
- "List all my open MRs across all projects"
- "Revoke the leaked access token in the my-org/my-repo group"

## Architecture

Modular — each capability area lives in its own file under `tools/`:

```
tools/
├── gitlab_repo.py        # repos, branches, files
├── gitlab_pipeline.py    # CI/CD lifecycle
├── ci_generator.py       # YAML scaffolding
├── gitlab_security.py    # SAST / DAST / scans
├── gitlab_runner.py      # runner registration
├── error_analyzer.py     # log → root cause → fix
└── gitlab_tokens.py      # access token lifecycle
```

## License

MIT — see [LICENSE](LICENSE).

## Support

See [SUPPORT.md](SUPPORT.md) for community + commercial support.
