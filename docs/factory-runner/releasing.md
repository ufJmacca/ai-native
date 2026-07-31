# Factory runner release and independent verification

This runbook defines the automated AN-04 producer release and the independent
consumer verification path for `factory-runner-protocol/v1`.

## Release authority and merge policy

Release Please is the only semantic-version authority. AN-04 does not choose a
version in advance and no other workflow edits the version, changelog, or
release tag. Release Please opens or updates its release PR, repository rules
require zero approving human reviews, and the trusted workflow enables GitHub
auto-merge. Required status checks and branch protection remain mandatory.

When the protected release PR merges, Release Please creates the exact tag and
a draft release. The reusable release workflow checks out the tag commit by
its 40-character SHA and verifies the tag, version, draft-release URL, and
upload URL before building anything.

There is no reviewer-controlled environment or operator click in this path. A
scope, security, permission, conflict, or external-service exception stops the
workflow; fixing the exception does not waive a required check.

## Build-once certification sequence

The draft release stays unpublished while one workflow performs these gates in
order:

1. Build exactly one `ai_native_base-<version>-py3-none-any.whl` with the
   release commit and tag embedded in its strict build identity.
2. Build and push the OCI image from that exact wheel, using digest-pinned
   Python and uv bases and no source-tree copy.
3. Run author-success, author-no-change, and verify-success against the source
   checkout, an isolated wheel installation, and the image under a read-only
   root filesystem, fixed UID/GID, dropped capabilities, no network, and only
   declared writable mounts.
4. Require identical canonical RunResult, output-manifest, and complete
   output-tree digests across all three executable forms.
5. Generate an SPDX JSON SBOM and run a blocking Trivy policy for fixable
   `HIGH` and `CRITICAL` vulnerabilities.
6. Create GitHub/Sigstore provenance for the wheel and image, attest the image
   SBOM, and preserve the wheel provenance bundle as a release asset.
7. Build and deep-verify
   `factory-runner-release-receipt.json` from the actual artifact bytes and
   attestation URL.
8. Attest the receipt, upload the immutable draft assets without overwrite,
   download them into a clean directory, re-run the offline verifier, and
   verify wheel, image, SBOM, and receipt attestations.
9. Publish the verified draft release atomically.

Any failure leaves the release in draft state. An incorrect or incomplete
artifact is never replaced in place: correction requires a new semantic version,
a new tag, new digests, and a new receipt.

## Published artifacts

The release contains:

- the exact wheel named in the receipt;
- `compatibility-report.json`;
- `factory-runner.spdx.json`;
- `trivy-report.json`;
- `provenance.intoto.jsonl`; and
- `factory-runner-release-receipt.json`.

The image identity recorded by the receipt always has this immutable form:

```text
ghcr.io/ufjmacca/ai-native-factory-runner@sha256:<64 lowercase hex characters>
```

No consumer command requires a mutable image tag. AN-04 initially certifies
`linux/amd64`; another platform must pass the same image, security,
compatibility, attestation, and receipt gates before it can be added to the
receipt.

## Independent receipt-first verification

Start from an empty directory and an exact release tag. Verify the receipt and
wheel attestations before executing the downloaded wheel:

```bash
tag="ai-native-base-vX.Y.Z"
mkdir release-assets
gh release download "${tag}" \
  --repo ufJmacca/ai-native \
  --dir release-assets \
  --pattern 'ai_native_base-*.whl' \
  --pattern 'compatibility-report.json' \
  --pattern 'factory-runner.spdx.json' \
  --pattern 'trivy-report.json' \
  --pattern 'provenance.intoto.jsonl' \
  --pattern 'factory-runner-release-receipt.json'
gh attestation verify release-assets/factory-runner-release-receipt.json \
  --repo ufJmacca/ai-native \
  --signer-workflow \
  ufJmacca/ai-native/.github/workflows/factory-runner-release.yml
gh attestation verify release-assets/ai_native_base-X.Y.Z-py3-none-any.whl \
  --repo ufJmacca/ai-native \
  --signer-workflow \
  ufJmacca/ai-native/.github/workflows/factory-runner-release.yml
uv venv verifier
uv pip install \
  --python verifier/bin/python \
  release-assets/ai_native_base-X.Y.Z-py3-none-any.whl
verifier/bin/python -I \
  -m ai_native.factory_runner.release_verification \
  --receipt release-assets/factory-runner-release-receipt.json \
  --artifact-dir release-assets \
  --json
```

The offline verifier rejects unsafe names and links, missing or duplicate
assets, digest drift, malformed wheels, mismatched metadata, missing embedded
identity, schema drift, a failed or mismatched compatibility report, and any
receipt/artifact identity disagreement.

The repository wrapper
`scripts/verify_factory_runner_release_receipt.py` delegates to the same
packaged verifier for producer-side checks. Verify the image provenance and
SBOM attestation separately:

```bash
gh attestation verify \
  oci://ghcr.io/ufjmacca/ai-native-factory-runner@sha256:<digest> \
  --repo ufJmacca/ai-native \
  --signer-workflow \
  ufJmacca/ai-native/.github/workflows/factory-runner-release.yml
```

The private factory may begin FF-00 only after resolving this published
receipt, reproducing the receipt-resolved compatibility check, and locking the
wheel, OCI, source, schema, and report digests without source-tree coupling.
