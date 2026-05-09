"""Generate and modify .gitlab-ci.yml configurations."""
import base64
import yaml
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


_BASE_STAGES = ["validate", "test", "security", "build", "provision", "deploy", "dast", "promote"]

_SAST_INCLUDE = {"template": "Security/SAST.gitlab-ci.yml"}
_SECRET_DETECT_INCLUDE = {"template": "Security/Secret-Detection.gitlab-ci.yml"}
_DEP_SCAN_INCLUDE = {"template": "Security/Dependency-Scanning.gitlab-ci.yml"}
_CONTAINER_SCAN_INCLUDE = {"template": "Security/Container-Scanning.gitlab-ci.yml"}


def _base_config(app_type: str, default_image: str) -> dict:
    return {
        "stages": _BASE_STAGES,
        "default": {"image": default_image},
        "variables": {
            "DOCKER_DRIVER": "overlay2",
            "DOCKER_TLS_CERTDIR": "/certs",
        },
        "include": [_SAST_INCLUDE, _SECRET_DETECT_INCLUDE],
    }


_APP_CONFIGS = {
    "nodejs": {
        "image": "node:20-alpine",
        "validate": {
            "stage": "validate",
            "script": ["npm ci", "npm run lint"],
        },
        "test": {
            "stage": "test",
            "script": ["npm ci", "npm test"],
            "coverage": '/^Statements\\s*:\\s*(\\d+\\.?\\d*)%/',
            "artifacts": {
                "reports": {"coverage_report": {"coverage_format": "cobertura", "path": "coverage/cobertura-coverage.xml"}},
                "when": "always",
            },
        },
    },
    "python": {
        "image": "python:3.11-slim",
        "validate": {
            "stage": "validate",
            "script": [
                "pip install -r requirements.txt",
                "pip install flake8 mypy",
                "flake8 .",
            ],
        },
        "test": {
            "stage": "test",
            "script": ["pip install -r requirements.txt", "pytest --cov=. --cov-report=xml"],
            "coverage": '/^TOTAL.+?\\s+(\\d+%)$/',
            "artifacts": {
                "reports": {"coverage_report": {"coverage_format": "cobertura", "path": "coverage.xml"}},
                "when": "always",
            },
        },
    },
    "go": {
        "image": "golang:1.22-alpine",
        "validate": {
            "stage": "validate",
            "script": ["go vet ./...", "go build ./..."],
        },
        "test": {
            "stage": "test",
            "script": ["go test -v -coverprofile=coverage.out ./..."],
        },
    },
    "generic": {
        "image": "alpine:latest",
        "validate": {"stage": "validate", "script": ["echo 'Add your validate commands'"]},
        "test": {"stage": "test", "script": ["echo 'Add your test commands'"]},
    },
}


def _docker_build_job(image_name: str) -> dict:
    return {
        "stage": "build",
        "image": "docker:24",
        "services": ["docker:24-dind"],
        "variables": {
            "IMAGE_TAG": "$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA",
            "IMAGE_LATEST": "$CI_REGISTRY_IMAGE:latest",
        },
        "before_script": ["docker login -u $CI_REGISTRY_USER -p $CI_REGISTRY_PASSWORD $CI_REGISTRY"],
        "script": [
            "docker build -t $IMAGE_TAG -t $IMAGE_LATEST .",
            "docker push $IMAGE_TAG",
            "docker push $IMAGE_LATEST",
        ],
        "only": ["main", "staging", "develop"],
    }


def _argocd_deploy_job(environment: str, gitops_repo: str) -> dict:
    return {
        "stage": "deploy",
        "image": "alpine/git:latest",
        "script": [
            "git config --global user.email 'ci@gitlab.com'",
            "git config --global user.name 'GitLab CI'",
            f"git clone https://oauth2:$GITOPS_TOKEN@{gitops_repo.replace('https://', '')} gitops",
            "cd gitops",
            f"sed -i 's|tag:.*|tag: $CI_COMMIT_SHORT_SHA|' environments/{environment}/values.yaml",
            "git add -A",
            f"git commit -m 'ci: update {environment} image to $CI_COMMIT_SHORT_SHA [skip ci]'",
            "git push",
        ],
        "environment": {"name": environment},
        "only": ["main" if environment == "production" else environment],
    }


def register(mcp: FastMCP):

    @mcp.tool()
    def generate_ci_config(
        project_id: str,
        app_type: str = "nodejs",
        environments: list[str] = ["staging", "production"],
        gitops_repo: str = "",
        include_docker_build: bool = True,
        include_security: bool = True,
        target_branch: str = "main",
    ) -> dict:
        """Generate a complete .gitlab-ci.yml and commit it to the project.

        The generated pipeline includes: validate → test → security → build → provision → deploy → dast → promote.

        Args:
            project_id: Project ID or path.
            app_type: Application type: 'nodejs', 'python', 'go', or 'generic'.
            environments: List of deployment environments (e.g. ['staging', 'production']).
            gitops_repo: GitLab repo URL for ArgoCD GitOps manifests (e.g. 'https://gitlab.com/org/gitops').
            include_docker_build: Add Docker image build + push stage.
            include_security: Add SAST, secret detection, dependency scanning.
            target_branch: Branch to commit the CI config to (default 'main').
        """
        app_type = app_type if app_type in _APP_CONFIGS else "generic"
        app = _APP_CONFIGS[app_type]
        config = _base_config(app_type, app["image"])

        if not include_security:
            config["include"] = []

        config["validate-job"] = {**app["validate"], "stage": "validate"}
        config["test-job"] = {**app["test"], "stage": "test"}

        if include_docker_build:
            config["docker-build"] = _docker_build_job(f"$CI_REGISTRY_IMAGE")

        if gitops_repo:
            for env in environments:
                config[f"deploy-{env}"] = _argocd_deploy_job(env, gitops_repo)
            config["promote-to-production"] = {
                "stage": "promote",
                "script": ["echo 'Manual promotion gate passed'"],
                "when": "manual",
                "only": ["staging"],
                "needs": ["deploy-staging"],
            }

        raw = yaml.dump(config, default_flow_style=False, sort_keys=False)

        gl = _gl()
        p = _project(gl, project_id)
        try:
            f = p.files.get(file_path=".gitlab-ci.yml", ref=target_branch)
            f.content = raw
            f.save(branch=target_branch, commit_message="ci: generate .gitlab-ci.yml pipeline")
            action = "updated"
        except gitlab.exceptions.GitlabGetError:
            p.files.create({
                "file_path": ".gitlab-ci.yml",
                "branch": target_branch,
                "content": raw,
                "commit_message": "ci: add .gitlab-ci.yml pipeline",
            })
            action = "created"

        return {
            "action": action,
            "file_path": ".gitlab-ci.yml",
            "branch": target_branch,
            "app_type": app_type,
            "stages": _BASE_STAGES,
            "content_preview": raw[:500] + "..." if len(raw) > 500 else raw,
        }

    @mcp.tool()
    def validate_ci_config(project_id: str, content: str = "") -> dict:
        """Lint a .gitlab-ci.yml using GitLab's API validator.

        Args:
            project_id: Project ID or path (used to resolve project-level includes).
            content: YAML content to validate. If empty, fetches the current .gitlab-ci.yml.
        """
        gl = _gl()
        p = _project(gl, project_id)

        if not content:
            try:
                f = p.files.get(file_path=".gitlab-ci.yml", ref=p.default_branch)
                content = base64.b64decode(f.content).decode("utf-8")
            except gitlab.exceptions.GitlabGetError:
                return {"valid": False, "errors": [".gitlab-ci.yml not found in repository"]}

        result = gl.ci_lint.create({"content": content})
        return {
            "valid": result.valid,
            "errors": result.errors,
            "warnings": result.warnings,
        }

    @mcp.tool()
    def add_security_stages(
        project_id: str,
        enable_sast: bool = True,
        enable_secret_detection: bool = True,
        enable_dependency_scan: bool = True,
        enable_container_scan: bool = False,
        enable_dast: bool = False,
        dast_target_url: str = "",
        branch: str = "",
    ) -> dict:
        """Inject GitLab security scanning templates into the project's .gitlab-ci.yml.

        Args:
            project_id: Project ID or path.
            enable_sast: Enable Static Application Security Testing.
            enable_secret_detection: Enable secret and credential detection.
            enable_dependency_scan: Enable dependency vulnerability scanning.
            enable_container_scan: Enable container image scanning.
            enable_dast: Enable Dynamic Application Security Testing (requires running app URL).
            dast_target_url: URL of the deployed app for DAST scan.
            branch: Branch to update (defaults to default branch).
        """
        gl = _gl()
        p = _project(gl, project_id)
        branch = branch or p.default_branch

        try:
            f = p.files.get(file_path=".gitlab-ci.yml", ref=branch)
            config = yaml.safe_load(base64.b64decode(f.content).decode("utf-8")) or {}
        except gitlab.exceptions.GitlabGetError:
            config = {"stages": list(_BASE_STAGES)}

        includes = config.get("include", [])
        if not isinstance(includes, list):
            includes = [includes]

        def _add(template: dict):
            if template not in includes:
                includes.append(template)

        if enable_sast:
            _add(_SAST_INCLUDE)
        if enable_secret_detection:
            _add(_SECRET_DETECT_INCLUDE)
        if enable_dependency_scan:
            _add(_DEP_SCAN_INCLUDE)
        if enable_container_scan:
            _add(_CONTAINER_SCAN_INCLUDE)

        config["include"] = includes

        if enable_dast and dast_target_url:
            config["dast"] = {
                "stage": "dast",
                "image": {"name": "registry.gitlab.com/security-products/dast:4", "entrypoint": [""]},
                "variables": {"DAST_WEBSITE": dast_target_url, "DAST_FULL_SCAN_ENABLED": "true"},
                "artifacts": {
                    "paths": ["gl-dast-report.json"],
                    "reports": {"dast": "gl-dast-report.json"},
                },
            }

        raw = yaml.dump(config, default_flow_style=False, sort_keys=False)
        f.content = raw
        f.save(branch=branch, commit_message="ci: add security scanning stages")
        return {
            "action": "updated",
            "sast": enable_sast,
            "secret_detection": enable_secret_detection,
            "dependency_scan": enable_dependency_scan,
            "container_scan": enable_container_scan,
            "dast": enable_dast,
        }

    @mcp.tool()
    def add_terraform_stages(
        project_id: str,
        terraform_dir: str = "infrastructure",
        state_backend: str = "s3",
        branch: str = "",
        manual_apply: bool = True,
    ) -> dict:
        """Add Terraform plan and apply stages to .gitlab-ci.yml.

        Terraform runs entirely inside GitLab CI — not locally.
        Credentials are injected via masked CI/CD variables (TF_VAR_* or AWS_* / GOOGLE_* etc.).

        Args:
            project_id: Project ID or path.
            terraform_dir: Path to Terraform code in the repo (default 'infrastructure').
            state_backend: Remote state backend: 's3' (EKS/AWS) or 'gcs' (GCP).
            branch: Branch to update (defaults to default branch).
            manual_apply: Require manual approval before apply (recommended for prod).
        """
        gl = _gl()
        p = _project(gl, project_id)
        branch = branch or p.default_branch

        try:
            f = p.files.get(file_path=".gitlab-ci.yml", ref=branch)
            config = yaml.safe_load(base64.b64decode(f.content).decode("utf-8")) or {}
        except gitlab.exceptions.GitlabGetError:
            config = {"stages": list(_BASE_STAGES)}

        tf_image = "hashicorp/terraform:1.8"

        config["terraform:plan"] = {
            "stage": "provision",
            "image": tf_image,
            "script": [
                f"cd {terraform_dir}",
                "terraform init",
                "terraform validate",
                "terraform plan -out=tfplan -input=false",
            ],
            "artifacts": {
                "paths": [f"{terraform_dir}/tfplan"],
                "expire_in": "1 week",
                "reports": {"terraform": f"{terraform_dir}/plan.json"},
            },
        }

        apply_job: dict = {
            "stage": "provision",
            "image": tf_image,
            "script": [
                f"cd {terraform_dir}",
                "terraform init",
                "terraform apply -input=false tfplan",
            ],
            "dependencies": ["terraform:plan"],
            "needs": ["terraform:plan"],
            "only": ["main"],
        }
        if manual_apply:
            apply_job["when"] = "manual"

        config["terraform:apply"] = apply_job

        raw = yaml.dump(config, default_flow_style=False, sort_keys=False)
        try:
            f.content = raw
            f.save(branch=branch, commit_message="ci: add Terraform plan/apply stages")
        except Exception:
            p.files.create({
                "file_path": ".gitlab-ci.yml",
                "branch": branch,
                "content": raw,
                "commit_message": "ci: add Terraform plan/apply stages",
            })
        return {
            "action": "updated",
            "terraform_dir": terraform_dir,
            "state_backend": state_backend,
            "manual_apply": manual_apply,
        }

    @mcp.tool()
    def add_environment_promotion(
        project_id: str,
        from_env: str = "staging",
        to_env: str = "production",
        branch: str = "",
    ) -> dict:
        """Add a manual promotion gate between environments in .gitlab-ci.yml.

        Args:
            project_id: Project ID or path.
            from_env: Source environment (e.g. 'staging').
            to_env: Target environment (e.g. 'production').
            branch: Branch to update (defaults to default branch).
        """
        gl = _gl()
        p = _project(gl, project_id)
        branch = branch or p.default_branch

        try:
            f = p.files.get(file_path=".gitlab-ci.yml", ref=branch)
            config = yaml.safe_load(base64.b64decode(f.content).decode("utf-8")) or {}
        except gitlab.exceptions.GitlabGetError:
            return {"error": ".gitlab-ci.yml not found. Generate it first with generate_ci_config."}

        config[f"promote-{from_env}-to-{to_env}"] = {
            "stage": "promote",
            "script": [f"echo 'Promoting {from_env} → {to_env}'"],
            "when": "manual",
            "environment": {"name": to_env},
            "needs": [f"deploy-{from_env}"],
        }

        raw = yaml.dump(config, default_flow_style=False, sort_keys=False)
        f.content = raw
        f.save(branch=branch, commit_message=f"ci: add {from_env}→{to_env} promotion gate")
        return {"action": "updated", "gate": f"{from_env}→{to_env}"}
