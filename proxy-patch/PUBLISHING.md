# Publishing the patched CLI

This fork stores the repeatable patch queue under `proxy-patch/`. The branch is based on the fork's main branch so its review contains the build integration without rolling upstream source back to v2.37.4. The pipeline assembles the exact v2.37.4 Coder and Tailscale commits recorded in `sources.lock.json`; the ordinary repository root is not the patched CLI source.

Run commands from `proxy-patch/`, following [README.md](README.md) and the [reviewed plan](docs/PLAN.md). The frozen README, research plan and original workflow are retained as build recipe inputs. The active fork workflows are `.github/workflows/proxy-patch-verify.yml` and `.github/workflows/proxy-patch-publish.yml` at the repository root. The nested `.github/workflows/verify.yml` is recipe data. The preserved report records local qualification before fork integration; subsequent publication status comes from actual run receipts.

The GitHub repository is `treyturner/coder`. Its Forgejo pull mirror carries source and workflow files. Both active workflows check the GitHub server origin and repository; publication also requires `main`. The source-run checker rejects mirror origins before using a token. Registry images require separate copies to both destinations.

The inherited Coder workflow classifies changed paths before selecting checks. On this GitHub fork, a nonempty diff confined to `proxy-patch/`, its two workflows, eight named fork CI files (`contrib.yaml`, `doc-check.yaml`, `deploy-docs.yaml`, `publish-mcp-registry.yaml`, `pr-cleanup.yaml`, `pr-deploy.yaml`, `linear-release.yaml`, `scorecard.yml`), and `.gitignore` uses the pinned patch qualification workflow. This selection applies to pull requests and pushes on `main`, matching verification's branch and path filters. Changes to `ci.yaml` itself and any production-source path keep ordinary repository CI; an unverifiable comparison fails the scope gate. A branch name alone never suppresses checks. Coder's organization membership, CLA store, preview publishing, deployment and cleanup, documentation-review workloads, Linear sync, MCP and Scorecard publication, Pixel snapshots and Slack notification jobs are restricted to `github.com/coder/coder`. Generic PR title and release-label checks remain active. A passing patch run qualifies the pinned CLI, not the unrelated current repository-root server source.

## Image references and identities

The requested external naming convention is `treyturner/coder:<upstream_version>-proxyfix-<build_number>`. The qualified build uses:

- `ghcr.io/treyturner/coder:2.37.4-proxyfix-1`
- `forgejo.treyturner.info/treyturner/coder:2.37.4-proxyfix-1`

The binary version remains `2.37.4+proxyfix.1.1d24629`. The OCI layout's internal reference remains `2.37.4-proxyfix.1`. Only the external registry tag changes punctuation; no image bytes are changed.

`publication.json` records both repositories, the derived tag, the qualified CLI checksum, and the independently reviewed identities:

```text
Registry index: sha256:dc6e6ed42ec8e5ff0a4bf72da5a43a87a66528cd60a1c554173d2ef7225f2759
Linux amd64:   sha256:51be698a535461b8d1d5b19c88c5fb20c99691862e46b68e0fa6283bed1cd107
```

## Verify before publishing

Open or update a pull request to run **Verify patched proxy CLI**. Merged changes run verification again on `main`. Superseded PR runs are cancelled; main and manually dispatched runs have unique concurrency groups and remain eligible to complete. It exercises the original defect, runs the full patch qualification pipeline and local registry regressions, rebuilds from separate source, and uploads `coder-proxy-linux-amd64`. That artifact contains the image, source, complete frozen build recipe, licenses, inventory, provenance and checksums. Hosted verification artifacts expire after 14 days; approved publication retains their exact 12 top-level payload files as durable GitHub Release assets before registry writes.

The main branch must contain the workflow before **Publish approved proxy CLI** is available for manual dispatch. Review and merge the fork integration separately; this task does not merge it automatically. Hosted execution and registry publication remain unverified until their actual runs complete.

Set the existing Forgejo registry username as the repository Actions variable `FORGEJO_REGISTRY_USERNAME` and bind the existing scoped token as the repository Actions secret `FORGEJO_REGISTRY_TOKEN`. The workflow maps the username variable to `FORGEJO_USER` for registry login. If the username is also configured as an Actions secret, remove that secret to avoid masking the username in logs and job outputs. Do not place the token in source, issue comments, command arguments, build inputs, or artifacts. The GitHub job uses its existing `GITHUB_TOKEN` with `packages: write`; the validation job has only read permissions. This workflow creates no account, token, signing identity, registry setting or network-policy change. Existing proxy and CA trust must permit normal verified HTTPS to both registries.

After the image and destinations are approved, manually dispatch **Publish approved proxy CLI** from `main` with:

1. The successful verification workflow run ID for the current `main` commit, produced after merge.
2. The independently reviewed registry index digest above.
3. The independently reviewed platform manifest digest above.

Publication never starts from a push or pull request. The job checks the source workflow identity and success, verifies the downloaded artifact against the current frozen recipe, source lock and both supplied digests, and qualifies a registry publisher from the pinned Go/module graph. A separate companion job uses the existing GitHub token with `contents: write`, without registry credentials. It requires verification of the current `main` commit and binds the release tag to that commit. The registry job has `packages: write` and waits for both prior jobs to pass; it repeats artifact verification before binding registry credentials. Its temporary authentication files are removed and never uploaded.

The build records use `proxy-patch-2.37.4-proxyfix-1`, outside upstream release tags and excluded from GitHub's latest-release selection. The companion job creates or resumes a matching draft, refuses different tag targets, metadata, asset names or bytes, uploads missing assets without replacement, and checks their downloaded bytes. It then exposes the complete release and checks that the same files are publicly downloadable before permitting registry publication. GitHub authentication never follows asset redirects to storage hosts. `sources.tar.gz` contains the exact patched tracked source and license notices; `recipe.tar.gz` contains the exact frozen patch/build recipe. GitHub's generated source archive contains the fork integration. One canonical GitHub release serves both registries; a Forgejo Git mirror does not copy release assets.

Keep `main` fixed during publication and retries so the successful source run still names its current commit. Interrupted matching draft uploads and existing matching publications are resumable. Unexpected, incomplete or conflicting asset bytes fail closed; the workflow never deletes, clobbers, retags or substitutes a payload. Retain completed build records even if a subsequent registry copy fails. Release text describes the qualified build; registry success requires its separate readback receipt. This workflow does not enable GitHub release immutability settings or create credentials.

The publisher uses go-containerregistry v0.20.7, with the same module versions selected by the qualified Coder graph. Registry authentication needs two additional already-selected module zip checksums, retained in `publish/registry.sum`. A private modfile adds them without editing the qualified source or its `go.sum`.

GitHub's prebuilt Python can use a different zlib runtime from the qualified host. Each CI job checks the frozen Python/zlib combination first. When required, `scripts/ci_packager.py` builds the checksummed official zlib 1.3.1 source under the job's temporary directory, probes a fresh Python process with that library, and selects it through the job environment. It performs no global install and preserves TLS verification. This integration changes neither the frozen recipe nor the qualified image.

The pinned `gci` formatter treats piped stdin as Go source even when file paths are supplied. Pipeline subprocesses receive `subprocess.DEVNULL`, a character device on the locked Linux platform, so formatting behaves consistently in CI and local piped execution. Output remains captured for safe diagnostics.

Both registry tags are checked before any registry writes. An existing matching index is accepted for an idempotent retry; a conflicting index, authorization error, TLS error or network failure stops the operation. The publisher copies the stored registry index, rather than the OCI layout catalog, without reformatting manifests or recompressing layers. It then reads back the index, platform manifest, config, every compressed layer and embedded CLI checksum from each registry. Safe per-registry results are uploaded as `proxy-publication-receipt`.

Workflow concurrency serializes this repository's publication jobs. Registry tag preflight and writes are not an atomic operation against other clients; use one publisher for these versioned tags. Two registries cannot be committed as one transaction. If the second fails, retain the verified first publication and retry the same approved digest after addressing the failure through existing approved configuration. A hard job cancellation can prevent receipt upload; use both registries' tag preflight and digest readback to establish the state before retrying. Do not delete a successful publication, broaden access, disable TLS, change compression, or substitute another image to make the second pass.

## Future revisions

Follow the source-update and regression procedure in README.md. After qualifying a new build, update `publication.json` with its derived `<upstream_version>-proxyfix-<build_number>` tag and actual reviewed index, platform and CLI identities. The registry suffix derives from the positive numeric part of `patch_revision` (`proxyfix.1` becomes `proxyfix-1`). Review and merge those changes, run verification again, and approve the new digests before manual publication. A new tag never authorizes skipping source, compatibility, regression, artifact or rollback checks.

The local registry tests cover full OCI copies, verified TLS, synthetic Basic authentication, both-destination preflight, HTTP 401/403/500, conflicting tags, cancellation, CLI checksum mismatch, partial publication and idempotent recovery. Local execution is recorded separately from hosted CI and actual registry receipt evidence.

Companion fixtures cover interrupted and paginated draft lookup, duplicate releases, conflicting tags/metadata/asset bytes, failed readback, public delivery, idempotent retries and authentication removal on storage redirects. They also exercise the actual twelve-file payload without contacting GitHub or a registry. Offline fixture success does not claim a release exists.
