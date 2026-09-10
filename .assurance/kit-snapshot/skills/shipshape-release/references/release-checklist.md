# Release checklist

## Candidate evidence

The helper's `local` result is the completion criterion before proposing a
version. `ok: true` means all of these refer to one exact SHA:

- the working tree is clean and checked out on GitHub's actual default branch;
- local HEAD, origin's default-branch ref, and GitHub's default-branch head
  agree;
- the candidate's configuration identifies enabled security workflows;
- the exact release workflow is registered and active, and its default-branch
  `.sdlc/scripts/release-preflight.py` helper exists; neither needs a prior tag
  run;
- the exact-path CI workflow and enabled Secret scan and CodeQL workflows have
  an unambiguously latest applicable `push` attempt for that SHA and branch,
  ordered by the current attempt's UTC `run_started_at`;
- each applicable run completed successfully and every returned job succeeded.

An unrelated workflow, a same-named workflow at another path, an earlier green
attempt before a failed rerun, or a skipped job supplies no evidence. API,
authentication, pagination, missing/malformed/tied attempt chronology, and
incomplete-response errors all stop publication. A local-only repository is not
GitHub-release-ready: connect and push its intended GitHub origin first.

GitHub generally suppresses workflow runs for events created with the repository
`GITHUB_TOKEN` ([documented behavior](https://docs.github.com/en/actions/concepts/security/github_token)),
so a bot merge may have no post-merge `push` run. Neither pushing an unchanged ref
nor `workflow_dispatch` satisfies this push-only policy. The bounded recovery is a
separately approved release/version commit pushed with normal human or appropriately
authorized app credentials: restart preflight for that new SHA, whose evidence does
not transfer to the bot-merged SHA. A continuous bot-merge release path remains a
separate feature.

## Version and approval

Review every commit since the last tag. Patch means fixes only, minor includes
new features, and major breaks existing users. When torn between two, choose
the larger bump. First releases normally start at v0.1.0; v1.0.0 is a promise
of stability.

Show the exact SHA and proposed tag at the publication approval boundary. Once
approved, the helper proves the candidate again before tag creation and proves
the local tag plus remote absence immediately before push. Existing local and
remote tags remain immutable; a correction uses a new patch tag.

## Publication evidence

At run start, the rendered workflow asks GitHub for the repository's current
default branch and loads `.sdlc/scripts/release-preflight.py` from that branch.
It does not check out or execute the candidate's helper. This is current trusted
policy, not an immutable default-branch snapshot. The workflow creates a
release only after `publish` verifies the remote tag's exact commit,
default-branch ancestry, enabled workflow identities, latest attempts, and
jobs. Its JSON output records the SHA and supporting run URLs.

After the tag push, `verify-release` must identify the same immutable tag and
SHA, the exact `.github/workflows/release.yml` push run for that tag, and the
non-draft release URL. Wait for a pending run. An external transient or a bug
in the current default-branch helper can be fixed before rerunning the same tag;
the existing approval still covers that unchanged exact tag and SHA. A bug in
the tagged release workflow cannot be fixed by rerunning because its immutable
commit retains the old workflow code. Land that fix on the default branch,
prove a new candidate, choose a new version tag, and obtain approval for the
new exact tag and SHA. Never delete or move the old tag.

## Trust limit

This policy trusts administrators allowed to change the default-branch setting,
the helper fetched from that branch, or release workflow code. A trusted
administrator can therefore replace current or future release policy; this
design does not claim an immutable policy snapshot when the branch moves.
GitHub API availability and the token's read access to Actions evidence are
also required; neither condition is silently skipped.
