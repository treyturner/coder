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
    pipeline.check(PUBLICATION["repositories"] == ["ghcr.io/treyturner/coder", "forgejo.treyturner.info/treyturner/coder"], "publication destinations differ from approved repositories")
    pipeline.check(index == actual["registry_index_digest"] == PUBLICATION["registry_index_digest"], "approved registry index differs from qualified image")
    pipeline.check(manifest == actual["platform_manifest_digest"] == PUBLICATION["platform_manifest_digest"], "approved platform manifest differs from qualified image")
    pipeline.check(actual["binary_sha256"] == PUBLICATION["binary_sha256"], "qualified CLI checksum differs")
    return actual


def prepare(work, parent, test=False):
    pipeline.validate_assembly(parent)
    go = str(pipeline.bootstrap(work))
    env = pipeline.build_env(work)
    env["CGO_ENABLED"] = "1" if test else "0"
    directory = work / "tools/registry-publisher"
    directory.mkdir(parents=True, exist_ok=True)
    # Registry authentication imports two already-selected modules whose zip
    # sums the CLI does not need. Use a private modfile and pinned extra sums;
    # the qualified Coder source and go.sum remain untouched.
    shutil.copyfile(parent / "coder/go.mod", directory / "publisher.mod")
    sums = (parent / "coder/go.sum").read_text().splitlines() + (ROOT / "publish/registry.sum").read_text().splitlines()
    (directory / "publisher.sum").write_text("\n".join(sorted(set(sums))) + "\n")
    modfile = "-modfile=" + str(directory / "publisher.mod")
    selected = pipeline.run([go, "list", modfile, "-m", "-f", "{{.Version}}", PUBLICATION["registry_client_module"]], cwd=parent / "coder", env=env, capture=True).decode().strip()
    pipeline.check(selected == PUBLICATION["registry_client_version"], "unexpected registry client version")
    files = ["main.go"] + (["main_internal_test.go"] if test else [])
    for name in files:
        shutil.copyfile(ROOT / "publish" / (name + ".txt"), directory / name)
    formatted = pipeline.run([str(work / "toolchains/go/bin/gofmt"), "-l", *[str(directory / name) for name in files]], capture=True)
    pipeline.check(not formatted, "registry publisher needs Go formatting")
    if test:
        command = [go, "test", modfile, "-race", "-count=1", "-json", *[str(directory / name) for name in files]]
        result = subprocess.run(command, cwd=parent / "coder", env=env, capture_output=True)
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
        pipeline.run([go, "build", modfile, "-o", str(binary), str(directory / "main.go")], cwd=parent / "coder", env=env)
        (directory / "publish.sha256").write_text(pipeline.digest(binary.read_bytes()) + "  publish\n")
        print("Registry publisher compiled from pinned module graph.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "test", "verify", "publish"))
    parser.add_argument("--work", type=Path, default=ROOT.parent / ".build")
    parser.add_argument("--assembly", default="publisher")
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--approved-index")
    parser.add_argument("--approved-manifest")
    args = parser.parse_args()
    pipeline.check(all(Path(name).name == name and name not in ("", ".", "..") for name in (args.assembly, args.dist)), "assembly and dist must be directory names")
    work = args.work.resolve()
    if args.command in ("build", "test"):
        prepare(work, work / args.assembly, args.command == "test")
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
        print("publication stopped:", str(error) if isinstance(error, RuntimeError) else type(error).__name__, file=sys.stderr)
        sys.exit(1)
