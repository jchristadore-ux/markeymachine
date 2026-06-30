#!/usr/bin/env python3
"""Fetch raw Railway logs for a service so Claude (or you) can read them in a
session — for the dashboard service's own process logs, the separate live-bot
service, or build/deploy logs (anything not in a per-account worker.log).

Setup (no secrets are committed):
  Add these as environment secrets in the Claude Code web environment for this
  repo so they persist across sessions:
    RAILWAY_API_TOKEN       — a Railway account/team or project token
    RAILWAY_PROJECT_ID      — your project id
    RAILWAY_ENVIRONMENT_ID  — the environment id (e.g. production)
    RAILWAY_SERVICE_ID      — default service id (override with --service-id)

Usage:
    python tools/railway_logs.py --lines 100
    python tools/railway_logs.py --service-id <id> --lines 200
    python tools/railway_logs.py --deployment <deployment_id>

Notes:
  * Outbound traffic goes through the agent proxy; if backboard.railway.app is
    blocked by the environment's network policy this will fail clearly — fall
    back to the dashboard's /health and /admin/api/logs (which already cover
    every customer bot).
  * Railway's GraphQL schema can change; the queries are isolated constants
    below and print the server's error verbatim so they are easy to adjust.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable, List, Optional

API_URL = "https://backboard.railway.app/graphql/v2"

# Latest deployment for a project/environment/service.
DEPLOYMENTS_QUERY = """
query deployments($projectId: String!, $environmentId: String!, $serviceId: String!) {
  deployments(first: 1, input: {
    projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId
  }) {
    edges { node { id status createdAt } }
  }
}
""".strip()

# Logs for a deployment.
LOGS_QUERY = """
query deploymentLogs($deploymentId: String!, $limit: Int!) {
  deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
    timestamp
    message
    severity
  }
}
""".strip()


class RailwayError(RuntimeError):
    pass


class RailwayClient:
    """Thin GraphQL client. `post_fn(url, headers, json)` is injectable for tests."""

    def __init__(self, token: str, post_fn: Optional[Callable] = None) -> None:
        if not token:
            raise RailwayError("RAILWAY_API_TOKEN is not set.")
        self._token = token
        if post_fn is None:
            import requests
            post_fn = lambda url, headers, json_body: requests.post(  # noqa: E731
                url, headers=headers, json=json_body, timeout=15)
        self._post = post_fn

    def _gql(self, query: str, variables: dict) -> dict:
        resp = self._post(
            API_URL,
            {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            {"query": query, "variables": variables},
        )
        if getattr(resp, "status_code", 200) != 200:
            raise RailwayError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if data.get("errors"):
            raise RailwayError("GraphQL error: " + json.dumps(data["errors"])[:400])
        return data.get("data", {})

    def latest_deployment_id(self, project_id: str, environment_id: str, service_id: str) -> str:
        data = self._gql(DEPLOYMENTS_QUERY, {
            "projectId": project_id, "environmentId": environment_id, "serviceId": service_id,
        })
        edges = (data.get("deployments") or {}).get("edges") or []
        if not edges:
            raise RailwayError("No deployments found for that service/environment.")
        return edges[0]["node"]["id"]

    def deployment_logs(self, deployment_id: str, limit: int) -> List[dict]:
        data = self._gql(LOGS_QUERY, {"deploymentId": deployment_id, "limit": limit})
        return data.get("deploymentLogs") or []


def format_logs(logs: List[dict]) -> str:
    out = []
    for entry in logs:
        ts = entry.get("timestamp", "")
        sev = entry.get("severity", "")
        msg = entry.get("message", "")
        out.append(f"{ts} {sev:>5} {msg}".rstrip())
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fetch Railway logs for a service.")
    ap.add_argument("--service-id", default=os.environ.get("RAILWAY_SERVICE_ID", ""))
    ap.add_argument("--project-id", default=os.environ.get("RAILWAY_PROJECT_ID", ""))
    ap.add_argument("--environment-id", default=os.environ.get("RAILWAY_ENVIRONMENT_ID", ""))
    ap.add_argument("--deployment", default="", help="deployment id (skips lookup)")
    ap.add_argument("--lines", type=int, default=100)
    args = ap.parse_args(argv)

    try:
        client = RailwayClient(os.environ.get("RAILWAY_API_TOKEN", ""))
        deployment_id = args.deployment
        if not deployment_id:
            for name, val in (("RAILWAY_PROJECT_ID", args.project_id),
                              ("RAILWAY_ENVIRONMENT_ID", args.environment_id),
                              ("RAILWAY_SERVICE_ID", args.service_id)):
                if not val:
                    raise RailwayError(f"Missing {name} (pass --deployment to skip lookup).")
            deployment_id = client.latest_deployment_id(
                args.project_id, args.environment_id, args.service_id)
        logs = client.deployment_logs(deployment_id, max(1, args.lines))
        print(format_logs(logs))
        return 0
    except RailwayError as e:
        print(f"railway_logs: {e}", file=sys.stderr)
        print("Fallback: read the dashboard's /health and /admin/api/logs instead.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
