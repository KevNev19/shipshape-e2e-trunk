---
name: shipshape-review
description: Review a change in plain language — verified findings, whether it matches the request, what could not be verified, and a clear "looks safe" or "needs attention" verdict with reasons. Works on a pull request, a branch, or uncommitted changes. Use when the user says "review this change", "review PR 12", "what does this change do", "is this safe to merge", or "explain this diff".
---

# shipshape-review

Translate a change and its automated-check results for someone who may not
read code. Strictly read-only — this skill never edits, commits, merges, or
approves. Follow
[../shipshape-init/references/voice.md](../shipshape-init/references/voice.md);
inspect with [references/review-rubric.md](references/review-rubric.md), then
report through
[references/finding-contract.md](references/finding-contract.md).

Use this entrypoint only when the user requests a review or change explanation.

## Steps

1. **Get the change.** In order of what the user pointed at:
   - a PR number → `gh pr view <n>` and `gh pr diff <n>`
   - a branch → `git diff <default-branch>...<branch>`
   - otherwise → uncommitted work: `git diff` plus `git diff --cached`
   If the diff is empty, say so and stop.

2. **Get the check results** (PRs only): `gh pr checks <n>`. For a failing
   check, fetch the failed step's log tail (`gh run view --log-failed`) and
   find the first real error line.

3. **Find the intent.** Use the linked intent artifact (a written record of
   what the change is meant to do), originating issue (the task that led to
   the change), pull request, task, or the user's request. Link an artifact or
   issue when one exists. Do not guess when none exists.

4. **Read the whole diff** and work through the rubric. For large diffs,
   inspect every file but organize findings by severity, not by file.
   Inspect both names of renames and file-mode changes. A scanner pass is
   bounded evidence, not a guarantee; verify control and security changes from
   the diff and report material heuristic gaps or false-positive uncertainty.

5. **Report through the finding contract.** Keep its five parts in order.
   Translate check failures and code evidence into plain-language
   consequences. Give every finding exactly one next action.

## Don't

- Don't merge, approve, comment on, or edit anything — report only.
- Don't give a verdict without having read the full diff and check results.
- Don't say "looks safe" when checks are red — ever.
- Don't narrate the diff file by file; report verified findings instead.
- Don't assume intent. If no intent artifact or issue exists, say so and
  judge the change on its own terms.
