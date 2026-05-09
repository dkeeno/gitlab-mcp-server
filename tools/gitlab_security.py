"""Security scanning tools: SAST, DAST, secret detection, vulnerability management."""
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
    def gitlab_get_security_report(
        project_id: str,
        pipeline_id: int,
        report_type: str = "sast",
    ) -> dict:
        """Retrieve a security scan report from a completed pipeline.

        Args:
            project_id: Project ID or path.
            pipeline_id: Pipeline ID that ran the security scan.
            report_type: One of: 'sast', 'secret_detection', 'dependency_scanning',
                         'container_scanning', 'dast'.
        """
        gl = _gl()
        p = _project(gl, project_id)
        artifact_map = {
            "sast": "gl-sast-report.json",
            "secret_detection": "gl-secret-detection-report.json",
            "dependency_scanning": "gl-dependency-scanning-report.json",
            "container_scanning": "gl-container-scanning-report.json",
            "dast": "gl-dast-report.json",
        }
        artifact_path = artifact_map.get(report_type)
        if not artifact_path:
            raise ValueError(f"Unknown report_type '{report_type}'. Choose from: {list(artifact_map.keys())}")

        jobs = p.jobs.list(pipeline_id=pipeline_id, per_page=100)
        security_jobs = [j for j in jobs if j.status in ("success", "failed") and report_type.replace("_", "-") in j.name.lower().replace("_", "-")]

        if not security_jobs:
            return {"error": f"No {report_type} job found in pipeline {pipeline_id}", "pipeline_id": pipeline_id}

        try:
            import json
            content = security_jobs[0].artifact(artifact_path)
            report = json.loads(content)
            vulnerabilities = report.get("vulnerabilities", [])
            return {
                "report_type": report_type,
                "pipeline_id": pipeline_id,
                "scanner": report.get("scan", {}).get("scanner", {}).get("name", "unknown"),
                "vulnerability_count": len(vulnerabilities),
                "vulnerabilities": vulnerabilities[:50],
            }
        except Exception as e:
            return {"error": str(e), "report_type": report_type, "pipeline_id": pipeline_id}

    @mcp.tool()
    def gitlab_list_vulnerabilities(
        project_id: str,
        severity: str = "",
        state: str = "detected",
        limit: int = 50,
    ) -> list[dict]:
        """List security vulnerabilities for a project (requires GitLab Ultimate or paid plan for full access).

        Falls back to reading the latest pipeline security report if the vulnerability API is unavailable.

        Args:
            project_id: Project ID or path.
            severity: Filter by severity: 'critical', 'high', 'medium', 'low', 'info', 'unknown'.
            state: Vulnerability state: 'detected', 'confirmed', 'resolved', 'dismissed'.
            limit: Max results (default 50).
        """
        import requests

        gl = _gl()
        p = _project(gl, project_id)

        headers = {"PRIVATE-TOKEN": cfg.GITLAB_TOKEN}
        params: dict = {"per_page": limit, "state": state}
        if severity:
            params["severity"] = severity

        url = f"{cfg.GITLAB_URL}/api/v4/projects/{p.id}/vulnerabilities"
        resp = requests.get(url, headers=headers, params=params)

        if resp.status_code == 403:
            return [{"note": "Vulnerability API requires GitLab Ultimate. Use gitlab_get_security_report to read raw scan results from pipeline artifacts."}]

        resp.raise_for_status()
        vulns = resp.json()
        return [
            {
                "id": v.get("id"),
                "title": v.get("title"),
                "severity": v.get("severity"),
                "state": v.get("state"),
                "scanner": v.get("scanner", {}).get("name"),
                "location": v.get("location", {}).get("file"),
                "line": v.get("location", {}).get("start_line"),
                "cve": v.get("identifiers", [{}])[0].get("value") if v.get("identifiers") else None,
                "solution": v.get("solution"),
                "web_url": v.get("web_url"),
            }
            for v in vulns
        ]

    @mcp.tool()
    def gitlab_create_issue_from_vuln(
        project_id: str,
        title: str,
        severity: str,
        description: str,
        cve: str = "",
        affected_file: str = "",
        solution: str = "",
        labels: list[str] = [],
    ) -> dict:
        """Create a GitLab issue to track a security vulnerability finding.

        Args:
            project_id: Project ID or path.
            title: Issue title (e.g. 'CRITICAL: SQL injection in login endpoint').
            severity: Severity level for label: 'critical', 'high', 'medium', 'low'.
            description: Vulnerability description and reproduction steps.
            cve: CVE identifier if applicable (e.g. 'CVE-2024-1234').
            affected_file: File or component affected.
            solution: Recommended fix or mitigation.
            labels: Additional labels to apply.
        """
        gl = _gl()
        p = _project(gl, project_id)

        body = f"## Security Vulnerability\n\n**Severity:** {severity.upper()}\n\n"
        if cve:
            body += f"**CVE:** {cve}\n"
        if affected_file:
            body += f"**Affected file:** `{affected_file}`\n"
        body += f"\n### Description\n{description}\n"
        if solution:
            body += f"\n### Recommended Fix\n{solution}\n"

        all_labels = [f"security::{severity}", "security-finding"] + labels
        issue = p.issues.create({
            "title": title,
            "description": body,
            "labels": ",".join(all_labels),
        })
        return {
            "iid": issue.iid,
            "title": issue.title,
            "state": issue.state,
            "labels": issue.labels,
            "web_url": issue.web_url,
        }

    @mcp.tool()
    def gitlab_get_security_summary(project_id: str, pipeline_id: int) -> dict:
        """Get a unified security summary across all scan types from a pipeline.

        Args:
            project_id: Project ID or path.
            pipeline_id: Pipeline ID that ran security scans.
        """
        import json

        gl = _gl()
        p = _project(gl, project_id)
        jobs = p.jobs.list(pipeline_id=pipeline_id, per_page=100)

        report_map = {
            "sast": "gl-sast-report.json",
            "secret_detection": "gl-secret-detection-report.json",
            "dependency_scanning": "gl-dependency-scanning-report.json",
            "container_scanning": "gl-container-scanning-report.json",
            "dast": "gl-dast-report.json",
        }

        summary: dict = {"pipeline_id": pipeline_id, "scans": {}}
        severity_totals: dict = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}

        for scan_type, artifact_path in report_map.items():
            matching = [j for j in jobs if j.status == "success" and scan_type.replace("_", "-") in j.name.lower().replace("_", "-")]
            if not matching:
                summary["scans"][scan_type] = {"status": "not_run"}
                continue
            try:
                content = matching[0].artifact(artifact_path)
                report = json.loads(content)
                vulns = report.get("vulnerabilities", [])
                by_severity: dict = {}
                for v in vulns:
                    sev = v.get("severity", "unknown").lower()
                    by_severity[sev] = by_severity.get(sev, 0) + 1
                    if sev in severity_totals:
                        severity_totals[sev] += 1
                summary["scans"][scan_type] = {"count": len(vulns), "by_severity": by_severity}
            except Exception as e:
                summary["scans"][scan_type] = {"status": "error", "detail": str(e)}

        summary["totals"] = severity_totals
        summary["risk_level"] = (
            "critical" if severity_totals["critical"] > 0
            else "high" if severity_totals["high"] > 0
            else "medium" if severity_totals["medium"] > 0
            else "low" if severity_totals["low"] > 0
            else "clean"
        )
        return summary
