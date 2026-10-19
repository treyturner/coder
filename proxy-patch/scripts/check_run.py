#!/usr/bin/env python3
"""Require a successful verification run from this repository before publishing."""
import json
import os
import re
import sys
import urllib.error
import urllib.request


class RunCheckError(RuntimeError):
    """A fixed local verification-run diagnostic safe for workflow logs."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_api(request):
    try:
        return urllib.request.build_opener(NoRedirect()).open(request, timeout=30)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise RunCheckError("source workflow request failed (HTTP " + str(status) + ")") from None


def verified_run(run_id):
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise RunCheckError("verification run ID must be a positive integer")
    if os.environ.get("GITHUB_SERVER_URL") != "https://github.com":
        raise RunCheckError("publishing requires the GitHub Actions origin")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise RunCheckError("existing Actions read authentication is unavailable")
    request = urllib.request.Request(
        "https://api.github.com/repos/treyturner/coder/actions/runs/" + run_id,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"},
    )
    with open_api(request) as response:
        run = json.load(response)
    allowed = (
        run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("event") in ("push", "workflow_dispatch")
        and run.get("path", "").split("@", 1)[0] == ".github/workflows/proxy-patch-verify.yml"
        and run.get("repository", {}).get("full_name") == "treyturner/coder"
        and run.get("head_repository", {}).get("full_name") == "treyturner/coder"
        and (run.get("head_branch", "").startswith("proxy-patch/") or run.get("head_branch") == "main")
    )
    if not allowed:
        raise RunCheckError("run is not a successful trusted proxy-patch verification")
    return run


def main():
    if len(sys.argv) != 2:
        raise RunCheckError("provide one verification run ID")
    verified_run(sys.argv[1])
    print("Verified source workflow run:", sys.argv[1])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print remote response bodies, URLs or authentication material.
        print("verification-run check failed:", str(error) if isinstance(error, RunCheckError) else type(error).__name__, file=sys.stderr)
        sys.exit(1)
