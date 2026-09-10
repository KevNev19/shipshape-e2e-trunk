#!/usr/bin/env bash
# Thin wrappers around the GitHub CLI for shipshape skills. Every mutation is
# a single explicit command the skill shows the user before running. Failures
# print a plain-English fallback ("here's what to click instead") because
# branch protection APIs vary by GitHub plan and token scope.
#
# Usage:
#   gh_helpers.sh protection-plan <branch> <trunk|pr> [options]
#   gh_helpers.sh protect <branch> <trunk|pr> [options]
#   gh_helpers.sh protection-status <branch>            show current protection
#   gh_helpers.sh add-collaborator <user> <pull|push|admin>
#   gh_helpers.sh remove-collaborator <user>
#   gh_helpers.sh list-collaborators
set -euo pipefail

fallback() {
  echo "Could not change branch protection automatically (plan or permission limits)." >&2
  echo "Do it by hand instead: GitHub -> your repository -> Settings -> Rules -> Rulesets." >&2
  if [ "$2" = "trunk" ]; then
    echo "Target '$1'; block branch deletion and non-fast-forward pushes." >&2
    echo "Do not require post-push CI unless you first adopt a PR or staging path." >&2
  else
    echo "Target '$1'; require the exact source-bound checks shown in the plan." >&2
  fi
}

repo_slug() {
  gh repo view --json nameWithOwner --jq .nameWithOwner
}

protection_command() {
    operation="$1"
    usage_name="$2"
    branch="${3:-}"
    workflow_style="${4:-}"
    if [ -z "$branch" ] || { [ "$workflow_style" != "trunk" ] && [ "$workflow_style" != "pr" ]; }; then
      echo "usage: gh_helpers.sh $usage_name <branch> <trunk|pr> [--tiered-review] [--codeql-enabled] [--require-review]" >&2
      return 2
    fi
    slug="$(repo_slug)"
    script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
    protection_args=(
      "$operation"
      --repo "$slug"
      --branch "$branch"
      --workflow-style "$workflow_style"
    )
    shift 4
    for option in "$@"; do
      case "$option" in
        --tiered-review|--codeql-enabled|--require-review)
          protection_args+=("$option")
          ;;
        *)
          echo "usage: gh_helpers.sh $usage_name <branch> <trunk|pr> [--tiered-review] [--codeql-enabled] [--require-review]" >&2
          return 2
          ;;
      esac
    done
    if ! python3 "$script_dir/protection.py" "${protection_args[@]}"; then
      if [ "$operation" = "apply" ]; then
        fallback "$branch" "$workflow_style"
      fi
      return 1
    fi
}

cmd="${1:-}"
case "$cmd" in
  protection-plan)
    protection_command plan protection-plan "${@:2}"
    ;;
  protect)
    protection_command apply protect "${@:2}"
    ;;
  protection-status)
    branch="${2:?usage: gh_helpers.sh protection-status <branch>}"
    slug="$(repo_slug)"
    gh api "repos/$slug/branches/$branch/protection" 2>/dev/null \
      || echo '{"protected": false}'
    ;;
  add-collaborator)
    user="${2:?usage: gh_helpers.sh add-collaborator <user> <pull|push|admin>}"
    permission="${3:?permission required: pull (read), push (write), or admin}"
    slug="$(repo_slug)"
    if gh api --method PUT "repos/$slug/collaborators/$user" -f permission="$permission" >/dev/null; then
      echo "Invited '$user' with $permission access (they get an email invitation to accept)."
    else
      echo "Could not invite '$user' automatically. By hand: GitHub -> your repository ->" >&2
      echo "Settings -> Collaborators -> 'Add people'." >&2
      exit 1
    fi
    ;;
  remove-collaborator)
    user="${2:?usage: gh_helpers.sh remove-collaborator <user>}"
    slug="$(repo_slug)"
    gh api --method DELETE "repos/$slug/collaborators/$user" >/dev/null
    echo "Removed '$user' from the repository."
    ;;
  list-collaborators)
    slug="$(repo_slug)"
    gh api "repos/$slug/collaborators" \
      --jq '.[] | {login: .login, role: .role_name}'
    ;;
  *)
    echo "usage: gh_helpers.sh protection-plan|protect|protection-status|add-collaborator|remove-collaborator|list-collaborators" >&2
    exit 2
    ;;
esac
