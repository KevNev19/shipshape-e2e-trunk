---
name: shipshape-release
description: Verify and publish one exact tested commit. Use when the user says "cut a release", "ship a version", "release this", or "tag v1.2".
---

# shipshape-release

Guide the user through a release. A release is a named, tagged version of
the project others can rely on. Follow
[../shipshape-init/references/voice.md](../shipshape-init/references/voice.md);
read [references/release-checklist.md](references/release-checklist.md) before
preflight and again for exact post-publication verification.

Use this entrypoint only when the user requests a release.

This skill sequences the release helper; the helper owns release-readiness
policy and prints one JSON result. Translate its blocker and `next_action`
without weakening either.

## Bundle path

Before running the helper, set `SHIPSHAPE_ROOT` to the actual absolute bundle
path resolved from this loaded skill: from the directory containing this
`SKILL.md`, go up two parent directories. This is the Shipshape bundle, not
the target repository or current working directory. Replace the example with
the discovered path and keep it quoted:

```bash
export SHIPSHAPE_ROOT="/absolute/path/to/the/loaded/shipshape/bundle"
```

## Steps

1. **Prove the candidate.** Run the deterministic local preflight:

   ```bash
   preflight_file="$(mktemp "${TMPDIR:-/tmp}/shipshape-release-preflight.XXXXXX")"
   python3 "${SHIPSHAPE_ROOT}/scripts/release_preflight.py" local . \
     >"$preflight_file"
   ```

   Read the JSON from `$preflight_file`, then remove only that created file
   with `rm -f -- "$preflight_file"` whether the result passes or fails.
   Continue only when `ok` is `true`. Record its exact `candidate_sha`,
   `repository`, default branch, and workflow run URLs. On failure, state the
   blocker and its one `next_action`, then stop. Missing GitHub evidence is a
   blocker, including for a local-only repository.

2. **What's in this release.** Find the last release tag
   (`git describe --tags --abbrev=0`, or "first release" if none) and list
   the commits since. Summarize them in plain language, grouped as: new
   things, fixes, everything else.

3. **Pick the version.** Semver in one sentence: fixes bump the last number,
   new features the middle, breaking changes the first. Propose the bump the
   commits justify (conventional-commit prefixes are the hint: `fix:` →
   patch, `feat:` → minor) and name its tag `v<version>`. Let the user override
   the version while keeping the `v` tag prefix required by the workflow.

4. **Publication approval.** Show the proposed tag and exact candidate SHA.
   Explain that pushing the tag asks the trusted release workflow to publish
   a GitHub release. Pause for an explicit yes; this remains the publication
   boundary.

5. **Recheck, tag, and recheck.** On approval, set `CANDIDATE_SHA`,
   `OWNER_REPO`, and `TAG` from the accepted JSON and version. Re-run the full
   preflight immediately before tagging, create the immutable tag at that
   exact SHA, then re-run with the tag immediately before its only push:

   ```bash
   python3 "${SHIPSHAPE_ROOT}/scripts/release_preflight.py" local . \
     --repo "$OWNER_REPO" --candidate "$CANDIDATE_SHA"
   git tag -a "$TAG" "$CANDIDATE_SHA" -m "$TAG"
   python3 "${SHIPSHAPE_ROOT}/scripts/release_preflight.py" local . \
     --repo "$OWNER_REPO" --candidate "$CANDIDATE_SHA" --tag "$TAG"
   git push origin "refs/tags/${TAG}:refs/tags/${TAG}"
   ```

   Each helper invocation must return `ok: true`; otherwise stop before the
   next command. The second invocation proves that the local tag still names
   the candidate and the remote tag is absent. Never replace an existing tag.

6. **Verify the exact publication.** Wait for the release workflow, then run:

   ```bash
   python3 "${SHIPSHAPE_ROOT}/scripts/release_preflight.py" verify-release \
     --repo "$OWNER_REPO" --tag "$TAG" --candidate "$CANDIDATE_SHA"
   ```

   A pending result means wait and retry this same exact command. Preserve the
   tag after a failure. When its exact tag and SHA stay unchanged, the existing
   publication approval remains valid: fix an external transient or the current
   default-branch helper, then rerun the same tagged workflow. When the tagged
   workflow definition is wrong, rerunning retains that old code. Fix it on the
   default branch, prove a new candidate, choose a new version, and return to
   the publication boundary for that new exact tag and SHA. On success, report
   the release URL, exact SHA, and every workflow run URL from the JSON.

## Guardrails

- Every release note traces to a commit.
- A dirty tree, missing evidence, pending/skipped/red job, or changed candidate
  stops the sequence at the helper result.
- Existing tags remain immutable; a correction gets a new patch version.
- The approved release tag is the only pushed ref.
