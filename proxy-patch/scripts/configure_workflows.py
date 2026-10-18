#!/usr/bin/env python3
"""Keep only the two patch workflows enabled in the GitHub fork."""
import argparse
import json
import re
import subprocess
import sys

REPOSITORY = "treyturner/coder"
PREFIX = "repos/" + REPOSITORY + "/actions/workflows"
ENABLED = frozenset((
    ".github/workflows/proxy-patch-verify.yml",
    ".github/workflows/proxy-patch-publish.yml",
))


def request(path, method="GET"):
    command = ["gh", "api", "--hostname", "github.com"]
    if method != "GET":
        command += ["--method", method, "--silent"]
    result = subprocess.run(command + [path], stdin=subprocess.DEVNULL, capture_output=True)
    if result.returncode:
        statuses = re.findall(rb"HTTP\s+([1-5][0-9]{2})", result.stderr)
        status = " (HTTP " + statuses[-1].decode() + ")" if statuses else ""
        raise RuntimeError("GitHub workflow administration failed" + status + "; check existing CLI authentication and Actions access")
    return json.loads(result.stdout) if method == "GET" else None


def workflows():
    values = []
    for number in range(1, 101):
        page = request(PREFIX + "?per_page=100&page=" + str(number))
        for item in page["workflows"]:
            path, identifier, state = item["path"], item["id"], item["state"]
            if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+", path) or type(identifier) is not int or identifier < 1:
                raise RuntimeError("invalid workflow identity")
            if state not in ("active", "deleted", "disabled_fork", "disabled_inactivity", "disabled_manually"):
                raise RuntimeError("unrecognized workflow state")
            if state != "deleted":
                values.append({"path": path, "id": identifier, "state": state})
        if len(page["workflows"]) < 100:
            break
    else:
        raise RuntimeError("workflow listing exceeded the verified limit")
    if len({item["id"] for item in values}) != len(values):
        raise RuntimeError("duplicate workflow identities")
    return values


def configure(apply=False):
    values = workflows()
    changes = [(item, "enable" if item["path"] in ENABLED else "disable") for item in values
               if (item["path"] in ENABLED) != (item["state"] == "active")]
    if apply:
        for item, action in changes:
            request(PREFIX + "/" + str(item["id"]) + "/" + action, "PUT")
        values = workflows()
    incorrect = [item["path"] for item in values if (item["path"] in ENABLED) != (item["state"] == "active")]
    if incorrect:
        print("Workflow settings require configuration:", ", ".join(sorted(incorrect)))
        raise RuntimeError("run configure_workflows.py --apply with existing repository Actions administration access")
    if not any(item["path"] == ".github/workflows/proxy-patch-verify.yml" for item in values):
        raise RuntimeError("patch verification workflow is unavailable")
    print("Verified GitHub fork workflow policy; inherited workflows are disabled.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="disable inherited workflows and enable any registered patch workflows")
    args = parser.parse_args()
    configure(args.apply)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print("Workflow configuration stopped:", str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        sys.exit(1)
