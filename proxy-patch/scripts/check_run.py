#!/usr/bin/env python3
"""Require a successful verification run from this repository before publishing."""
import json
import os
import re
import sys
import urllib.request


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[1-9][0-9]*", sys.argv[1]):
        raise RuntimeError("verification run ID must be a positive integer")
    if os.environ.get("GITHUB_SERVER_URL") != "https://github.com":
        raise RuntimeError("publishing requires the GitHub Actions origin")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("existing Actions read authentication is unavailable")
    request = urllib.request.Request(
        "https://api.github.com/repos/treyturner/coder/actions/runs/" + sys.argv[1],
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
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
        raise RuntimeError("run is not a successful trusted proxy-patch verification")
    print("Verified source workflow run:", sys.argv[1])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never print remote response bodies, URLs or authentication material.
        print("verification-run check failed", file=sys.stderr)
        sys.exit(1)
