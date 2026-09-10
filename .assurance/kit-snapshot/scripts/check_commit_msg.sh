#!/usr/bin/env bash
# Enforces the shipshape commit-message convention (docs/agents/harness.md,
# "Commit messages"): imperative subject, then a body carrying Why: and
# What: lines, plus Co-authored-by trailers for any AI agent whose work is
# in the commit. Runs at commit-msg stage via .pre-commit-config.yaml.
# Merges, reverts, and fixups are exempt. Bypass a false alarm with
# `git commit --no-verify`.
set -u

msg_file="${1:?usage: check_commit_msg.sh <commit-msg-file>}"
subject="$(head -1 "$msg_file")"

case "$subject" in
  Merge*|Revert*|fixup!*|squash!*) exit 0 ;;
esac

fail=0
if [ "${#subject}" -gt 72 ]; then
  echo "Subject is ${#subject} characters; keep it under 72 (aim for 50)." >&2
  fail=1
fi
if [ -n "$(sed -n 2p "$msg_file")" ]; then
  echo "Line 2 must be blank (subject, blank line, then body)." >&2
  fail=1
fi
body="$(sed -n '3,$p' "$msg_file" | grep -v '^#' || true)"
if ! printf '%s\n' "$body" | grep -qiE '^Why:'; then
  echo "Body needs a 'Why:' line — the reason this change exists." >&2
  fail=1
fi
if ! printf '%s\n' "$body" | grep -qiE '^What:'; then
  echo "Body needs a 'What:' line — what actually changed." >&2
  fail=1
fi

if [ "$fail" -eq 1 ]; then
  cat >&2 <<'EOF'

Expected shape:

  type: short imperative subject

  Why: the problem or reason, one or two sentences.
  What: the change itself, prose or bullets.

  Co-authored-by: Claude <noreply@anthropic.com>            (if Claude contributed)
  Co-authored-by: Codex (gpt-5.6-sol) <noreply@openai.com>  (if a Codex agent contributed)
  Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>  (if Copilot contributed)
EOF
  exit 1
fi
exit 0
