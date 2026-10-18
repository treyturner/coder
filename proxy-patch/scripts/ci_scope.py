#!/usr/bin/env python3
"""Select patch-only CI for changes confined to this fork's packaging queue."""
from fnmatch import fnmatchcase
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT_FILES = frozenset((
    ".gitignore",
    ".github/workflows/contrib.yaml",
    ".github/workflows/doc-check.yaml",
    ".github/workflows/deploy-docs.yaml",
    ".github/workflows/publish-mcp-registry.yaml",
    ".github/workflows/pr-cleanup.yaml",
    ".github/workflows/pr-deploy.yaml",
    ".github/workflows/linear-release.yaml",
    ".github/workflows/scorecard.yml",
    ".github/workflows/proxy-patch-verify.yml",
    ".github/workflows/proxy-patch-publish.yml",
))
PUSH_BRANCHES = ("main",)


def git(root, *args, optional=False):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=90)
    if result.returncode:
        if optional:
            return None
        raise RuntimeError("cannot establish changed paths")
    return result.stdout


def wrapper_only(root, server, repository, event, base, ref=""):
    if server != "https://github.com" or repository != "treyturner/coder" or event not in ("push", "pull_request"):
        return False
    if event == "push" and (not ref.startswith("refs/heads/") or not any(fnmatchcase(ref.removeprefix("refs/heads/"), pattern) for pattern in PUSH_BRANCHES)):
        return False
    if event == "push" and base == "0" * 40:
        git(root, "fetch", "--no-tags", "--depth=1", "origin", "main")
        base = "FETCH_HEAD"
    else:
        if not re.fullmatch(r"[0-9a-f]{40}", base):
            raise RuntimeError("missing or invalid comparison revision")
        if git(root, "cat-file", "-e", base + "^{commit}", optional=True) is None:
            git(root, "fetch", "--no-tags", "--depth=1", "origin", base)
    changed = git(root, "diff", "--name-only", "-z", "--no-renames", base, "HEAD", "--")
    paths = [os.fsdecode(path) for path in changed.split(b"\0") if path]
    return bool(paths) and all(path.startswith("proxy-patch/") or path in ROOT_FILES for path in paths)


def main():
    selected = wrapper_only(
        Path.cwd(), os.environ.get("GITHUB_SERVER_URL", ""),
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_EVENT_NAME", ""),
        os.environ.get("PATCH_BASE_SHA", ""),
        os.environ.get("GITHUB_REF", ""),
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("wrapper-only=" + str(selected).lower() + "\n")
    print("CI scope:", "pinned proxy patch" if selected else "ordinary repository checks")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, KeyError, subprocess.TimeoutExpired):
        print("CI scope stopped: comparison could not be verified", file=sys.stderr)
        sys.exit(1)
