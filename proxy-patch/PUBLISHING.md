# Publishing the patched CLI

This fork stores the repeatable patch queue under `proxy-patch/`. The branch is based on the fork's main branch so its review contains the build integration without rolling upstream source back to v2.37.4. The pipeline assembles the exact v2.37.4 Coder and Tailscale commits recorded in `sources.lock.json`; the ordinary repository root is not the patched CLI source.

Run commands from `proxy-patch/`, following [README.md](README.md) and the [reviewed plan](docs/PLAN.md). The frozen README, research plan and original workflow are retained as build recipe inputs. The active fork workflows are `.github/workflows/proxy-patch-verify.yml` and `.github/workflows/proxy-patch-publish.yml` at the repository root. The nested `.github/workflows/verify.yml` is recipe data. The preserved report records local qualification before fork integration; subsequent publication status comes from actual run receipts.

The GitHub repository is `treyturner/coder`. Its Forgejo pull mirror carries source and workflow files. Both active workflows check the GitHub server origin and repository; publication also requires `main`. The source-run checker rejects mirror origins before using a token. Registry images require separate copies to both destinations.

Inherited workflow files match the upstream integration base. This fork enables only `proxy-patch-verify.yml` and `proxy-patch-publish.yml` through GitHub's repository workflow settings. Before merging, run `python3 proxy-patch/scripts/configure_workflows.py --apply` from an existing authenticated GitHub CLI session with repository Actions administration access. The command disables inherited workflows without deleting or editing their source and enables registered patch workflows. Run it again after upstream syncs to cover newly introduced or renamed workflows. Verification and every publication job check this policy with read-only Actions access and stop if it drifts. The helper supports GitHub CLI 2.46 without newer pagination flags. The publication workflow becomes registered after it reaches `main`.

The Forgejo source mirror does not copy GitHub workflow settings. Keep Actions disabled for the mirror unless a separate workflow policy is deliberately configured there. These GitHub jobs qualify the pinned CLI; this fork does not run the ordinary current-root server CI.

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

Publication never starts from a push or pull request. The job checks the source workflow identity and success, verifies the downloaded artifact against the current frozen recipe, source lock and both supplied digests, and qualifies a registry publisher from the pinned Go/module graph. A separate companion job uses the existing GitHub token with `contents: write`, without registry credentials. It requires verification of the current `main` commit and creates a new release tag at that commit. Matching retries preserve the original tag target and record the current verification run, current integration commit and original tag commit in the receipt. The registry job has `packages: write` and waits for both prior jobs to pass; it repeats artifact verification before binding registry credentials. Its temporary authentication files are removed and never uploaded.

The build records use `proxy-patch-2.37.4-proxyfix-1`, outside upstream release tags and excluded from GitHub's latest-release selection. The companion job creates or resumes a matching draft, refuses invalid or moving tag targets, conflicting payload metadata, asset names or bytes, uploads missing assets without replacement, and checks their downloaded bytes. It then exposes the complete release and checks that the same files are publicly downloadable through the fixed repository web download URLs before permitting registry publication. Public readback makes no anonymous REST API asset requests. Every redirect is revalidated, with a bounded hop count. GitHub authentication never follows asset redirects to storage hosts. `sources.tar.gz` contains the exact patched tracked source and license notices; `recipe.tar.gz` contains the exact frozen patch/build recipe. GitHub's generated source archive contains the fork integration. One canonical GitHub release serves both registries; a Forgejo Git mirror does not copy release assets.

Keep `main` fixed during each publication attempt so its successful source run names the current commit. After a publication-tool fix or verification artifact expiry, verify the current main commit again and retry with the new successful run. The release identity binds the complete payload, independently of the run ID or current integration commit. Identical payloads reuse the existing tag and assets; changed recipe, source or image bytes require a new build tag. Interrupted matching draft uploads and existing matching publications are resumable. Unexpected, incomplete or conflicting asset bytes fail closed; the workflow never deletes, clobbers, retags or substitutes a payload. Retain completed build records even if a subsequent registry copy fails. Release text describes the qualified build; registry success requires its separate readback receipt. This workflow does not enable GitHub release immutability settings or create credentials.

The publisher uses go-containerregistry v0.20.7, with the same module versions selected by the qualified Coder graph. Registry authentication needs two additional already-selected module zip checksums, retained in `publish/registry.sum`. A private modfile adds them without editing the qualified source or its `go.sum`.

GitHub's prebuilt Python can use a different zlib runtime from the qualified host. Each CI job checks the frozen Python/zlib combination first. When required, `scripts/ci_packager.py` builds the checksummed official zlib 1.3.1 source under the job's temporary directory, probes a fresh Python process with that library, and selects it through the job environment. It performs no global install and preserves TLS verification. This integration changes neither the frozen recipe nor the qualified image.

The pinned `gci` formatter treats piped stdin as Go source even when file paths are supplied. Pipeline subprocesses receive `subprocess.DEVNULL`, a character device on the locked Linux platform, so formatting behaves consistently in CI and local piped execution. Output remains captured for safe diagnostics. Command failures report fixed categories and validated source file/line locations; raw stderr, remote bodies and proxy URLs remain suppressed. Verification always uploads sanitized JSON evidence, including failure cases. Source-run and release failures expose locally generated error messages, while unexpected exceptions expose only their type. Registry login failures identify the failing registry without printing authentication output.

Both registry tags are checked before any registry writes. An existing matching index is accepted for an idempotent retry; a conflicting index, authorization error, TLS error or network failure stops the operation. The publisher copies the stored registry index, rather than the OCI layout catalog, without reformatting manifests or recompressing layers. It then reads back the index, platform manifest, config, every compressed layer and embedded CLI checksum from each registry. Safe per-registry results are uploaded as `proxy-publication-receipt`.

Workflow concurrency serializes this repository's publication jobs. Registry tag preflight and writes are not an atomic operation against other clients; use one publisher for these versioned tags. Two registries cannot be committed as one transaction. If the second fails, retain the verified first publication and retry the same approved digest after addressing the failure through existing approved configuration. A hard job cancellation can prevent receipt upload; use both registries' tag preflight and digest readback to establish the state before retrying. Do not delete a successful publication, broaden access, disable TLS, change compression, or substitute another image to make the second pass.

## Future revisions

Follow the source-update and regression procedure in README.md. After qualifying a new build, update `publication.json` with its derived `<upstream_version>-proxyfix-<build_number>` tag and actual reviewed index, platform and CLI identities. The registry suffix derives from the positive numeric part of `patch_revision` (`proxyfix.1` becomes `proxyfix-1`). Review and merge those changes, run verification again, and approve the new digests before manual publication. A new tag never authorizes skipping source, compatibility, regression, artifact or rollback checks.

The local registry tests cover full OCI copies, verified TLS, synthetic Basic authentication, both-destination preflight, HTTP 401/403/500, conflicting tags, cancellation, CLI checksum mismatch, partial publication and idempotent recovery. Local execution is recorded separately from hosted CI and actual registry receipt evidence.

Companion fixtures cover interrupted and paginated draft lookup, duplicate releases, conflicting tags/metadata/asset bytes, failed readback, public delivery, idempotent retries and authentication removal on storage redirects. They also exercise the actual twelve-file payload without contacting GitHub or a registry. Offline fixture success does not claim a release exists.
