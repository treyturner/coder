#!/usr/bin/env python3
"""Derive a draft source lock from an official Coder tag for review and rebasing."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pipeline", ROOT / "scripts/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class BumpError(RuntimeError):
    """A fixed local failure while deriving public upstream metadata."""


def require(condition, message):
    if not condition:
        raise BumpError(message)


def checked_go(version, releases):
    require(re.fullmatch(r"go[0-9]+\.[0-9]+(?:\.[0-9]+)?", version) is not None, "unsupported upstream Go version")
    candidates = [item for item in releases if item.get("stable") and (item["version"] == version or item["version"].startswith(version + "."))]
    require(bool(candidates), "official Go toolchain metadata is unavailable")
    selected = max(candidates, key=lambda item: tuple(int(value) for value in item["version"][2:].split(".")))
    files = [item for item in selected["files"] if item.get("os") == "linux" and item.get("arch") == "amd64" and item.get("kind") == "archive"]
    require(len(files) == 1, "official Linux amd64 toolchain is ambiguous")
    item = files[0]
    require(re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is not None and item["filename"] == selected["version"] + ".linux-amd64.tar.gz", "invalid official toolchain checksum identity")
    return {"version": selected["version"], "filename": item["filename"], "url": "https://go.dev/dl/" + item["filename"], "sha256": item["sha256"]}


def replacement(mod):
    matches = [item["New"] for item in mod.get("Replace", []) if item["Old"]["Path"] == "tailscale.com"]
    require(len(matches) == 1 and matches[0]["Path"] == "github.com/coder/tailscale", "upstream Tailscale replacement requires manual review")
    value = matches[0]["Version"]
    require(re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-0)?\.[0-9]{14}-[a-f0-9]{12}", value) is not None, "unsupported upstream Tailscale pseudo-version")
    return value, value.rsplit("-", 1)[1]


def rollback(version, manifest):
    filename = "coder_" + version + "_linux_amd64.tar.gz"
    checksums = "coder_" + version + "_checksums.txt"
    matches = [checksum for checksum, name in re.findall(r"(?m)^([a-f0-9]{64})\s+\*?([^\s]+)$", manifest.decode()) if name == filename]
    require(len(matches) == 1, "official rollback checksum is unavailable or ambiguous")
    prefix = "https://github.com/coder/coder/releases/download/v" + version + "/"
    return {"filename": filename, "url": prefix + filename, "sha256": matches[0], "checksums_url": prefix + checksums, "checksums_sha256": pipeline.digest(manifest)}


def fetch(work, name, url, revision):
    repository = work / "bump-sources" / name
    if not repository.exists():
        repository.parent.mkdir(parents=True, exist_ok=True)
        pipeline.run(["git", "clone", "--depth=1", "--filter=blob:none", "--no-checkout", url, str(repository)])
    pipeline.run(["git", "fetch", "--depth=1", url, revision], cwd=repository)
    return repository


def git(repository, *args):
    return pipeline.run(["git", *args], cwd=repository, capture=True).decode().strip()


def effective_modules(data, expected):
    decoder = json.JSONDecoder()
    remaining = data.decode().lstrip()
    modules = {}
    while remaining:
        item, offset = decoder.raw_decode(remaining)
        path, version = item.get("Path"), item.get("Version")
        require(path in expected and path not in modules and bool(version) and not item.get("Replace"), "effective dependency replacements or pins require manual review")
        modules[path] = version
        remaining = remaining[offset:].lstrip()
    require(set(modules) == set(expected), "effective dependency pins require manual review")
    return modules


def write_draft(output, lock):
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x") as target:
            target.write(json.dumps(lock, indent=2) + "\n")
    except FileExistsError:
        raise BumpError("draft lock already exists; choose a fresh output path") from None


def draft(coder_tag, work, output):
    require(re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", coder_tag or "") is not None, "provide a complete official Coder tag")
    output = output.resolve()
    require(output != ROOT / "sources.lock.json" and not output.exists(), "choose a fresh draft-lock path; the reviewed source lock is never overwritten")
    original = (ROOT / "sources.lock.json").read_bytes()
    lock = copy.deepcopy(pipeline.LOCK)
    coder_url = "https://github.com/coder/coder.git"
    tailscale_url = "https://github.com/coder/tailscale.git"
    repository = fetch(work, "coder", coder_url, "refs/tags/" + coder_tag)
    coder_commit = git(repository, "rev-parse", "FETCH_HEAD^{commit}")
    require(re.fullmatch(r"[a-f0-9]{40}", coder_commit) is not None, "invalid upstream Coder commit")
    coder_tree = git(repository, "rev-parse", coder_commit + "^{tree}")
    epoch = int(git(repository, "show", "-s", "--format=%ct", coder_commit))
    current_go = str(pipeline.bootstrap(work))
    with tempfile.TemporaryDirectory(prefix="coder-bump-", dir=work) as scratch:
        directory = Path(scratch)
        (directory / "go.mod").write_bytes(pipeline.run(["git", "show", coder_commit + ":go.mod"], cwd=repository, capture=True))
        (directory / "go.sum").write_bytes(pipeline.run(["git", "show", coder_commit + ":go.sum"], cwd=repository, capture=True))
        mod = json.loads(pipeline.run([current_go, "mod", "edit", "-json"], cwd=directory, env=pipeline.build_env(work), capture=True))
        pseudo, prefix = replacement(mod)
        commit_metadata = json.loads(pipeline.download("https://api.github.com/repos/coder/tailscale/commits/" + prefix, accept="application/vnd.github+json"))
        tailscale_commit = commit_metadata.get("sha", "")
        require(re.fullmatch(r"[a-f0-9]{40}", tailscale_commit) is not None and tailscale_commit.startswith(prefix), "upstream Tailscale commit resolution differs")
        tailscale = fetch(work, "tailscale", tailscale_url, tailscale_commit)
        require(git(tailscale, "rev-parse", "FETCH_HEAD^{commit}") == tailscale_commit, "fetched Tailscale source differs")
        tailscale_tree = git(tailscale, "rev-parse", tailscale_commit + "^{tree}")
        releases = json.loads(pipeline.download("https://go.dev/dl/?mode=json&include=all", accept="application/json"))
        wanted_go = mod.get("Toolchain") or "go" + mod["Go"]
        lock["go"] = checked_go(wanted_go, releases)
        pipeline.LOCK = copy.deepcopy(lock)
        selected_go = str(pipeline.bootstrap(work / "bump-toolchains" / lock["go"]["version"]))
        selected = pipeline.run([selected_go, "list", "-m", "-json", *lock["modules"]], cwd=directory, env=pipeline.build_env(work), capture=True)
        modules = effective_modules(selected, lock["modules"])
    version = coder_tag.removeprefix("v")
    checksums_url = "https://github.com/coder/coder/releases/download/" + coder_tag + "/coder_" + version + "_checksums.txt"
    lock["rollback"] = rollback(version, pipeline.download(checksums_url))
    pipeline.download(lock["rollback"]["url"], lock["rollback"]["sha256"])
    lock.update({"base_version": version, "version": version + "+" + lock["patch_revision"] + "." + coder_commit[:7], "source_date_epoch": epoch, "modules": modules})
    for name, url, commit, tree in (("coder", coder_url, coder_commit, coder_tree), ("tailscale", tailscale_url, tailscale_commit, tailscale_tree)):
        lock["sources"][name].update({"url": url, "commit": commit, "base_tree": tree, "patch_sha256": None, "patched_tree": None})
    lock["sources"]["tailscale"]["original_replacement"] = "github.com/coder/tailscale " + pseudo
    require((ROOT / "sources.lock.json").read_bytes() == original, "reviewed source lock changed while deriving metadata")
    write_draft(output, lock)
    print("Draft lock written:", output)
    print("Review upstream fixes, rebase patches, choose the build number and refresh source hashes before qualification. Publication approval is unchanged.")
    return lock
