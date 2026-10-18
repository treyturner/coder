#!/usr/bin/env python3
"""Select the frozen zlib runtime for the pinned CI Python without global installs."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pipeline", ROOT / "scripts/pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=ROOT.parent / ".build")
    parser.add_argument("--force", action="store_true", help="exercise the local source build even when the runtime already matches")
    args = parser.parse_args()
    lock = pipeline.LOCK["packager"]
    pipeline.check(platform.system() == "Linux" and platform.machine() in ("amd64", "x86_64"), "CI packager requires Linux amd64")
    pipeline.check(platform.python_version() == lock["python"], "select the locked Python version before configuring zlib")
    print("Packager Python:", platform.python_version(), "zlib runtime:", zlib.ZLIB_RUNTIME_VERSION, flush=True)
    if zlib.ZLIB_RUNTIME_VERSION == lock["zlib"] and not args.force:
        print("Packager already matches frozen inputs.")
        return
    config = json.loads((ROOT / "ci/packager.json").read_bytes())["zlib"]
    pipeline.check(config["version"] == lock["zlib"], "CI zlib source differs from frozen packaging version")
    work = args.work.resolve()
    tools = work / "packager"
    tools.mkdir(parents=True, exist_ok=True)
    archive = tools / ("zlib-" + config["version"] + ".tar.gz")
    if not archive.exists():
        archive.write_bytes(pipeline.download(config["url"], config["sha256"]))
    pipeline.check(pipeline.digest(archive.read_bytes()) == config["sha256"], "cached zlib source checksum differs")
    # Fresh extraction and prefix prevent reuse of unverified build outputs.
    prefix = Path(tempfile.mkdtemp(prefix="zlib-" + config["version"] + "-", dir=tools))
    env = pipeline.build_env(work)
    env["CFLAGS"] = "-O2 -fPIC"
    with tempfile.TemporaryDirectory(prefix="zlib-source-", dir=tools) as scratch:
        source_root = Path(scratch)
        with tarfile.open(archive) as tar:
            tar.extractall(source_root, filter="data")
        source = source_root / ("zlib-" + config["version"])
        pipeline.run(["./configure", "--prefix=" + str(prefix)], cwd=source, env=env)
        pipeline.run(["make", "-j4"], cwd=source, env=env)
        pipeline.run(["make", "install"], cwd=source, env=env)
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    selected = str(prefix / "lib") + (os.pathsep + existing if existing else "")
    pipeline.check(not any(char in selected for char in "\r\n\0"), "invalid runtime library path")
    runtime_env = os.environ.copy()
    runtime_env["LD_LIBRARY_PATH"] = selected
    probe = "import json,platform,ssl,zlib; context=ssl.create_default_context(); assert context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED; print(json.dumps({'python':platform.python_version(),'zlib':zlib.ZLIB_RUNTIME_VERSION}))"
    result = subprocess.run([sys.executable, "-c", probe], env=runtime_env, capture_output=True, timeout=30)
    pipeline.check(result.returncode == 0 and json.loads(result.stdout) == lock, "selected Python cannot use the frozen zlib runtime")
    env_file = os.environ.get("GITHUB_ENV")
    pipeline.check(bool(env_file), "provide a task-local GITHUB_ENV file to select the verified runtime")
    with open(env_file, "a", encoding="utf-8") as output:
        output.write("LD_LIBRARY_PATH=" + selected + "\n")
    evidence = work / "evidence/ci-packager.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({"status": "pass", "packager": lock, "zlib_source_sha256": config["sha256"], "global_install": False}, indent=2) + "\n")
    print("Selected the checksummed task-local zlib runtime; TLS verification is enabled.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print("CI packager stopped:", str(error) if isinstance(error, pipeline.PipelineError) else type(error).__name__, file=sys.stderr)
        sys.exit(1)
