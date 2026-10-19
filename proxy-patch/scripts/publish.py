#!/usr/bin/env python3
"""Verify an approved image and copy it unchanged to both configured registries."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pipeline", ROOT / "scripts/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)
PUBLICATION = json.loads((ROOT / "publication.json").read_bytes())


def approved(output, index, manifest):
    pipeline.verify_image(output)
    actual = json.loads((output / "image-digests.json").read_bytes())
    version = pipeline.LOCK["base_version"]
    revision = pipeline.LOCK["patch_revision"]
    pipeline.check(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version), "invalid upstream version")
    pipeline.check(re.fullmatch(r"proxyfix\.[1-9][0-9]*", revision), "invalid patch build number")
    tag = version + "-proxyfix-" + revision.split(".")[1]
    pipeline.check(PUBLICATION["tag"] == tag, "publication tag differs from source lock")
    repositories = PUBLICATION["repositories"]
    pipeline.check(isinstance(repositories, list) and bool(repositories) and all(isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9._/-]*", value) for value in repositories), "invalid approved publication repositories")
    pipeline.check(len(set(repositories)) == len(repositories), "duplicate approved publication repositories")
    pipeline.check(index == actual["registry_index_digest"] == PUBLICATION["registry_index_digest"], "approved registry index differs from qualified image")
    pipeline.check(manifest == actual["platform_manifest_digest"] == PUBLICATION["platform_manifest_digest"], "approved platform manifest differs from qualified image")
    pipeline.check(actual["binary_sha256"] == PUBLICATION["binary_sha256"], "qualified CLI checksum differs")
    return actual


def prepare(work, test=False):
    go = str(pipeline.bootstrap(work))
    env = pipeline.build_env(work)
    env["CGO_ENABLED"] = "1" if test else "0"
    directory = work / "tools/registry-publisher"
    directory.mkdir(parents=True, exist_ok=True)
    # Compile only reviewed files, including when retrying in an existing work directory.
    with tempfile.TemporaryDirectory(prefix="registry-publisher-source-", dir=directory.parent) as scratch:
        prepare_source(work, Path(scratch), directory, go, env, test)


def prepare_source(work, source, directory, go, env, test):
    files = ("go.mod", "go.sum", "main.go", "main_internal_test.go")
    for name in files:
        shutil.copyfile(ROOT / "publish" / name, source / name)
    selected = pipeline.run([go, "list", "-m", "-f", "{{.Version}}", PUBLICATION["registry_client_module"]], cwd=source, env=env, capture=True).decode().strip()
    pipeline.check(selected == PUBLICATION["registry_client_version"], "unexpected registry client version")
    pipeline.run([go, "mod", "verify"], cwd=source, env=env)
    files = ("main.go", "main_internal_test.go")
    formatted = pipeline.run([str(work / "toolchains/go/bin/gofmt"), "-l", *[str(source / name) for name in files]], capture=True)
    pipeline.check(not formatted, "registry publisher needs Go formatting")
    if test:
        command = [go, "test", "-race", "-count=1", "-json", "."]
        result = subprocess.run(command, cwd=source, env=env, stdin=subprocess.DEVNULL, capture_output=True)
        # Persist only test identifiers/status, never registry responses or headers.
        cases = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("Action") in ("pass", "fail", "skip") and event.get("Test"):
                cases.append({"test": event["Test"], "status": event["Action"]})
        evidence = work / "evidence/publisher-tests.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps({"status": "pass" if result.returncode == 0 else "fail", "cases": cases}, indent=2) + "\n")
        pipeline.check(result.returncode == 0, "registry publisher regression checks failed")
        print("Registry publisher race/regression checks passed:", len(cases))
    else:
        binary = directory / "publish"
        pipeline.run([go, "build", "-o", str(binary), "."], cwd=source, env=env)
        (directory / "publish.sha256").write_text(pipeline.digest(binary.read_bytes()) + "  publish\n")
        print("Registry publisher compiled from pinned module graph.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "test", "verify", "publish"))
    parser.add_argument("--work", type=Path, default=ROOT.parent / ".build")
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--approved-index")
    parser.add_argument("--approved-manifest")
    args = parser.parse_args()
    pipeline.check(all(Path(name).name == name and name not in ("", ".", "..") for name in (args.dist,)), "dist must be a directory name")
    work = args.work.resolve()
    pipeline.EVIDENCE_DIRECTORY = work / "evidence"
    if args.command in ("build", "test"):
        prepare(work, args.command == "test")
        return
    output = work / args.dist
    actual = approved(output, args.approved_index, args.approved_manifest)
    if args.command == "verify":
        print("Approved OCI index, platform manifest, CLI and build inputs match.")
        return
    binary = work / "tools/registry-publisher/publish"
    expected_tool = (binary.parent / "publish.sha256").read_text()
    pipeline.check(expected_tool == pipeline.digest(binary.read_bytes()) + "  publish\n", "registry publisher checksum mismatch")
    command = [str(binary), "--oci", str(output / "oci"), "--index", actual["registry_index_digest"], "--manifest", actual["platform_manifest_digest"], "--binary", actual["binary_sha256"], "--tag", PUBLICATION["tag"]]
    for repository in PUBLICATION["repositories"]:
        command += ["--repository", repository]
    try:
        result = subprocess.run(command, capture_output=True, timeout=16 * 60)
        code, captured = result.returncode, result.stdout
    except subprocess.TimeoutExpired as error:
        code, captured = -1, error.stdout or b""
    events = []
    allowed = {repository + ":" + PUBLICATION["tag"] for repository in PUBLICATION["repositories"]}
    for line in captured.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("status") in ("started", "verified", "preflight-failed") and event.get("reference") in allowed:
            events.append({"reference": event["reference"], "status": event["status"]})
            if event["status"] == "verified":
                print("Verified:", event["reference"])
        elif event.get("status") == "failed" and re.fullmatch(r"[a-z-]+", event.get("stage", "")):
            events.append({"status": "failed", "stage": event["stage"]})
    verified = {event["reference"] for event in events if event["status"] == "verified"}
    receipt = {"status": "pass" if code == 0 and verified == allowed else "fail", "registry_index_digest": actual["registry_index_digest"], "platform_manifest_digest": actual["platform_manifest_digest"], "binary_sha256": actual["binary_sha256"], "destinations": events}
    evidence = work / "evidence/publication.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(receipt, indent=2) + "\n")
    pipeline.check(receipt["status"] == "pass", "publication stopped; inspect safe per-registry receipt before retrying")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print("publication stopped:", str(error) if isinstance(error, pipeline.PipelineError) else type(error).__name__, file=sys.stderr)
        sys.exit(1)
