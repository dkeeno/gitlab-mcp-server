import os
from pathlib import Path
from dotenv import load_dotenv

_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

GITLAB_URL = os.environ.get("GITLAB_URL", "https://gitlab.com")
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")


def validate():
    if not GITLAB_TOKEN:
        raise RuntimeError(
            "GITLAB_TOKEN is not set. "
            "Copy /Users/youruser/.mcp-servers/gitlab-mcp-server/.env.example "
            "to .env and set GITLAB_TOKEN=glpat-... "
            "or export GITLAB_TOKEN in your shell."
        )
