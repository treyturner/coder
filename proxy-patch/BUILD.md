# Reproduce this CLI build

Run from the archived `proxy-patch/` directory on Linux amd64. Use the Python and zlib versions in `sources.lock.json`, plus Git, a C compiler and Make. The pipeline downloads and checks the locked Go toolchain and uses task-local module and build caches. Existing proxy and CA settings remain in effect.

```sh
python3 scripts/pipeline.py all --work /absolute/task/build --assembly qualified
python3 scripts/pipeline.py verify --work /absolute/task/build
```

Use a fresh assembly directory for each independent reproduction. The source commits, patch hashes, resulting trees, dependency versions, runtime image and packaging inputs are pinned in the lock. A mismatch stops the build. Neither command publishes images, creates credentials, contacts a workspace or starts a remote workload.

When CI Python needs the locked zlib runtime, `scripts/ci_packager.py` builds the checksummed source in `ci/packager.json` under its task directory and writes the selected library path to `GITHUB_ENV` for a fresh Python process. That packaging setup is required only when creating compressed artifacts; verification reads the existing archives without recompressing them.
