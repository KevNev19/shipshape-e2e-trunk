---
name: shipshape-init
description: Set up a proper software delivery process (SDLC) in the current git repository — scan the project, ask a few plain-language questions, preview exactly what will be created, then write it. Use when the user says "set up this repo", "shipshape init", "get this project shipshape", or "add CI / proper process to this repo".
---

# shipshape-init

Onboard the current repository onto a proper delivery process. Works for any
project and any person — the user may not be a software engineer, so follow
the voice rules in [references/voice.md](references/voice.md) for everything
you say.

Use this entrypoint only when the user requests repository setup or repair.

This skill sequences scripts; it contains no logic of its own. Every script
prints JSON — branch on the JSON, and translate it for the user.

## Bundle path

Before running a bundled script, set `SHIPSHAPE_ROOT` to the actual absolute
bundle path discovered from this loaded skill: from the directory containing
this loaded `SKILL.md`, go up two parent directories. This is the Shipshape
bundle, not the target repository or current working directory. Replace the
example value with the path you discovered and keep it quoted:

```bash
export SHIPSHAPE_ROOT="/absolute/path/to/the/loaded/shipshape/bundle"
```

## Steps

1. **Preflight.** Check the basics and explain any gap in plain words before
   going further:

   ```bash
   python3 --version && git -C . rev-parse --show-toplevel
   ```

   - No `python3`: stop and explain how to install it (macOS: `xcode-select --install`
     or Homebrew; Windows: python.org installer).
   - Not a git repository: offer to run `git init -b main` first and explain in
     one sentence what a git repository is.

2. **Create a private session directory, then scan the project.** Keep this
   directory until setup finishes so the reviewed files are the ones applied.

   ```bash
   SHIPSHAPE_SESSION_DIR="$(mktemp -d "${TMPDIR:-/tmp}/shipshape-init.XXXXXX")"
   SHIPSHAPE_DETECT="${SHIPSHAPE_SESSION_DIR}/detect.json"
   SHIPSHAPE_CONFIG="${SHIPSHAPE_SESSION_DIR}/config.json"
   SHIPSHAPE_PLAN="${SHIPSHAPE_SESSION_DIR}/plan.json"
   python3 "${SHIPSHAPE_ROOT}/scripts/detect.py" . > "${SHIPSHAPE_DETECT}"
   cat "${SHIPSHAPE_DETECT}"
   ```

   If `existing_sdlc.config_present` is `true`, this repo is already set up —
   say so, and switch to repair/upgrade mode. Keep the existing
   `.sdlc/config.json`, skip the interview, and preview with the existing
   config in step 5. Repair never changes `docs/sdlc/design.md`.

3. **Confirm what was found.** Summarize the scan in two or three sentences
   ("This looks like a Python project that uses pytest for its tests — is
   that right?"). Confidence below "high" means ask, don't assert.

4. **Interview, then propose the config.** Ask the questions in
   [references/interview.md](references/interview.md) — a handful, outcomes
   not tools. Build the candidate outside the repository:

   ```bash
   python3 "${SHIPSHAPE_ROOT}/scripts/render.py" init-config . \
     --detect "${SHIPSHAPE_DETECT}" --output "${SHIPSHAPE_CONFIG}" \
     --set workflow_style=<trunk|pr> --set profile=<solo|team> \
     --set commands.test="<confirmed test command>"
   ```

   Pass a `--set` for every answer that differs from the detected default.
   Completion criterion: the command reports `action: proposed`, and the
   repository is still unchanged.

5. **Preview — the one plan gate.**

   ```bash
   # New setup:
   python3 "${SHIPSHAPE_ROOT}/scripts/render.py" plan . \
     --config "${SHIPSHAPE_CONFIG}" > "${SHIPSHAPE_PLAN}"

   # Existing setup repair:
   python3 "${SHIPSHAPE_ROOT}/scripts/render.py" plan . \
     > "${SHIPSHAPE_PLAN}"

   SHIPSHAPE_PLAN_DIGEST="$(python3 -c \
     'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["plan_digest"])' \
     "${SHIPSHAPE_PLAN}")"
   ```

   Read the saved plan JSON. Present it in plain language: each file that will
   be created, one line each, what it is and why
   ("`.github/workflows/ci.yml` — runs your tests automatically on GitHub after
   every change"). List `conflicts` separately: those files exist already and
   will NOT be touched. Then pause once for a go-ahead. Ask for replacement
   approval per conflicted path in that same gate. This is the only routine
   pause; retain approvals the user already gave.

6. **Apply exactly what was reviewed.** Add one `--force "<path>"` for each
   conflicted file the user approved. No approval means that path stays
   untouched.

   ```bash
   # New setup:
   python3 "${SHIPSHAPE_ROOT}/scripts/render.py" apply . \
     --config "${SHIPSHAPE_CONFIG}" \
     --expected-plan-digest "${SHIPSHAPE_PLAN_DIGEST}"

   # Existing setup repair:
   python3 "${SHIPSHAPE_ROOT}/scripts/render.py" apply . \
     --expected-plan-digest "${SHIPSHAPE_PLAN_DIGEST}"
   ```

   Accept the reviewed binding only when `binding.status` is `matched`. Report
   `status`, every written or removed path, and every untouched conflict.
   `partial` means some managed files remain at an earlier version; do not
   describe that result as fully upgraded. If status is `stale-plan`, return to
   step 5 and show the changed scope. Retain approvals for unchanged paths and
   ask only about new or changed scope before applying its new digest. The
   earlier preview never authorizes a silently regenerated plan.

7. **Install and inspect the local secret guard when enabled.** The installer
   verifies generated direct-hook wiring or statically recognizes its supported
   pre-commit configuration before returning success:

   ```bash
   bash .sdlc/hooks/install.sh
   ```

   On zero, report the wiring as installed and inspected. Static recognition of
   an existing dispatcher does not prove that arbitrary dispatcher code ran;
   describe scan behavior as verified only when that behavior was actually
   observed. On failure, report the guard as configured but not verifiably wired
   and give the installer error as the next thing to fix.

8. **Preview remote protection when enabled.** Read the applied config and use
   its exact `default_branch`, `workflow_style`, `features.tiered_review`, and
   applicable CodeQL setting. Run one read-only plan with the matching route:

   ```bash
   # Direct-push trunk: deletion and non-fast-forward safeguards only.
   python3 "${SHIPSHAPE_ROOT}/scripts/protection.py" plan \
     --repo <owner/repo> --branch <default-branch> --workflow-style trunk

   # PR style: add --tiered-review only when enabled, --codeql-enabled only
   # when CodeQL is enabled for a supported primary language, and
   # --require-review only when tiered review is off and one approval is wanted.
   python3 "${SHIPSHAPE_ROOT}/scripts/protection.py" plan \
     --repo <owner/repo> --branch <default-branch> --workflow-style pr \
     [--tiered-review] [--codeql-enabled] [--require-review]
   ```

   Show the proposal and every preserved existing control. A config feature flag
   records intent; this read-only plan is still not proof that protection is
   active. If the plan refuses or GitHub identity/access is unavailable, leave
   remote activation pending. Otherwise show the identical `apply` command,
   replacing only `plan` with `apply`, and run it after explicit approval.
   Completion criterion: apply reports `created` or `unchanged`, or the pending
   reason is reported without claiming protection is active.

9. **Seed a newly created design doc.** Only when step 6 lists
   `docs/sdlc/design.md` in `written`, fill its "What this project is" and
   "Structure" sections from the scan in plain language. It then belongs to
   the user. For repair, leave every byte of an existing design doc alone.

10. **Close accurately, then remove the session directory.** Report the local
   guard as installed/inspected, plus any scan behavior actually observed.
   Generated CI, CodeQL, and Dependabot files are
   configured until GitHub runs or confirms them. Writing configuration does
   not proof that their GitHub runs succeeded. Report the protection plan/apply
   result from step 8; a config flag alone is not remote proof. In trunk mode,
   checks run after a direct push, so they are not pre-landing enforcement. Name
   `docs/sdlc/harness.md` as the plan of record,
   suggest `/shipshape-doctor` as the anytime health check, give one next step,
   then remove the exact directory created in step 2.

   ```bash
   rm -rf "${SHIPSHAPE_SESSION_DIR}"
   ```

## Don't

- Keep proposed config and scan data in the unique session directory until
  apply; repository writes begin only after the step-5 plan gate.
- Don't ever overwrite a conflicted file without a per-file confirmation.
- Don't assert a detection guess as fact — confidence below "high" gets a
  question, not a statement.
- Don't use jargon without a parenthetical explanation on first use.
- Don't dump raw JSON or command output at the user — always translate.
- Don't commit or push; suggest it as the next step and let the user decide.
- Don't skip the interview for an empty repo — those users need the process
  most; the kit works fine with zero code.
