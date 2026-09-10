# gh recipes — access management

## Permission mapping (plain English → GitHub)

| User says                          | Permission | What it allows |
|------------------------------------|------------|----------------|
| "can look", "read only", "viewer"  | `pull`     | See the code, open issues |
| "can contribute", "works on this"  | `push`     | Everything above + push changes |
| "runs the project", "full control" | `admin`    | Everything above + settings, access, deletion |

Between two levels, pick the lower and say why.

## Commands (all via gh_helpers.sh)

Use the `SHIPSHAPE_ROOT` absolute bundle path resolved and exported by the
parent skill's Bundle path step.

```bash
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" list-collaborators
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" add-collaborator <user> <pull|push|admin>
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" remove-collaborator <user>
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" protection-plan <branch> trunk
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" protect <branch> trunk
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" protection-plan <branch> pr --codeql-enabled --require-review
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" protect <branch> pr --codeql-enabled --require-review
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" protection-plan <branch> pr --tiered-review --codeql-enabled
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" protect <branch> pr --tiered-review --codeql-enabled
bash "${SHIPSHAPE_ROOT}/scripts/gh_helpers.sh" protection-status <branch>
```

Always run and show `protection-plan` first. It is read-only. After the user
approves that exact proposal, repeat the same arguments with `protect`.

- `trunk` adds deletion and non-fast-forward safeguards only. CI runs after a
  direct push and is not a pre-landing gate.
- `pr` provisions strict source-bound status gates. Use `--tiered-review` only
  for the configured opt-in; omit `--require-review` in that lane so qualifying
  documentation can use its narrow automatic authority.
- Existing classic protection, reviews, checks, restrictions, bypasses, and
  unrelated rulesets are preserved. An unfamiliar same-name rule is a refusal,
  not permission to replace it.

## Known failure modes

- Private repo on a free plan: ruleset APIs may refuse → the script
  prints the Settings-page fallback; relay it.
- Missing `admin` permission on the repo: collaborator changes fail → the
  user needs the repository owner to do it or to grant them admin.
- Organization repos: team-based access is out of scope in v1 — point the
  user at Settings → Collaborators and teams.
