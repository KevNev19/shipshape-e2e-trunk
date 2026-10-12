---
name: shipshape-customize
description: Change how this repository's SDLC setup behaves — switch working style, turn features on or off, update the test command or reviewers — through a short Q&A, then regenerate only what's affected. Use when the user says "customize the setup", "turn off CodeQL", "switch to pull requests", "change the test command", or "update my shipshape config".
---

# shipshape-customize

Edit `.sdlc/config.json` conversationally and re-render the affected files.
Never hand-edit managed files to customize — change the config, regenerate.
Follow [../shipshape-init/references/voice.md](../shipshape-init/references/voice.md).

Use this entrypoint only when the user requests a Shipshape setup change.

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

1. **Read the current setup.** `cat .sdlc/config.json` (if missing, point to
   `/shipshape-init` and stop). Play back the settings relevant to the
   user's request in plain language ("changes currently go straight to main,
   protected by the automated tests").

2. **Map the request to config keys.** Common ones: `workflow_style`
   (trunk/pr), `profile` (solo/team), `commands.test`, `owners.default`,
   `toolchain.*`, `features.*`. If the user asks to disable a
   consequence-gated feature (`features.codeql`, `features.secret_guard`,
   `features.dependabot`, `features.branch_protection`,
   `features.scheduled_health`): state the one-sentence consequence and
   require an explicit yes before proceeding. For security controls, name
   the lost protection (for example, "commits will no longer be checked for
   passwords or keys"). For `scheduled_health`, use: "nobody will be told
   when a protection quietly disappears."

   Enabling `features.tiered_review` has the inverse gate: before turning it
   on, state: "allowlisted ordinary documentation can merge without a person
   after its exact checks and protection are verified" and require an explicit
   yes. Disabling it needs no consequence gate.

3. **Propose the config outside the repository.** Use a unique session
   directory and keep it through apply:

   ```bash
   SHIPSHAPE_SESSION_DIR="$(mktemp -d "${TMPDIR:-/tmp}/shipshape-customize.XXXXXX")"
   SHIPSHAPE_CONFIG="${SHIPSHAPE_SESSION_DIR}/config.json"
   SHIPSHAPE_PLAN="${SHIPSHAPE_SESSION_DIR}/plan.json"
   python3 "${SHIPSHAPE_ROOT}/scripts/render.py" set-config . \
     --set key=value --output "${SHIPSHAPE_CONFIG}"
   ```

   The proposal preserves unknown keys and settings not named in `--set`.
   Completion criterion: the command reports `action: proposed`, and the
   repository is still unchanged.

4. **Preview, approve, and apply the exact proposal.**

   ```bash
   python3 "${SHIPSHAPE_ROOT}/scripts/render.py" plan . \
     --config "${SHIPSHAPE_CONFIG}" > "${SHIPSHAPE_PLAN}"

   SHIPSHAPE_PLAN_DIGEST="$(python3 -c \
     'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["plan_digest"])' \
     "${SHIPSHAPE_PLAN}")"
   ```

   Read the saved plan JSON. Summarize every change and conflict. The expected
   config conflict means the current config will be replaced by the exact
   proposal; the go-ahead is the per-file approval for `.sdlc/config.json`.
   Ask once for each other conflict and retain approvals already given. Then
   apply with the config force plus only the other approved paths and the saved
   digest:

   ```bash
   python3 "${SHIPSHAPE_ROOT}/scripts/render.py" apply . \
     --config "${SHIPSHAPE_CONFIG}" --force ".sdlc/config.json" \
     --expected-plan-digest "${SHIPSHAPE_PLAN_DIGEST}"
   ```

   Accept the reviewed binding only when `binding.status` is `matched`. If
   status is `stale-plan`, return to the preview, write its new JSON to
   `${SHIPSHAPE_PLAN}`, and show the changed scope. Retain approvals for
   unchanged paths and ask only about new or changed scope before applying its
   new digest. The earlier preview never authorizes a silently regenerated
   plan.

   Turning a feature off stops managing its files but does not delete them —
   offer to remove each now-unmanaged file, and delete only on a per-file yes.
   Tiered review is the safety exception: disabling it plans removal of the
   managed auto-merge workflow. A drifted or unowned copy remains a conflict
   and still needs per-file approval before removal.

5. **Inspect local wiring and preview remote protection.** If enabled
   secret-guard files changed, run `bash .sdlc/hooks/install.sh`. A zero exit
   proves generated wiring or supported static wiring inspection, not arbitrary
   dispatcher execution. Describe scan behavior as verified only when observed.

   When branch protection is enabled, run the read-only protection plan using
   the applied config's exact style and branch. Trunk uses only compatible
   deletion/non-fast-forward safeguards; PR style may add exact source-bound
   checks. Pass `--tiered-review` only for the explicit opt-in and
   `--codeql-enabled` only when CodeQL is enabled for a supported primary
   language:

   ```bash
   python3 "${SHIPSHAPE_ROOT}/scripts/protection.py" plan \
     --repo <owner/repo> --branch <default-branch> \
     --workflow-style <trunk|pr> [--tiered-review] [--codeql-enabled]
   ```

   Show the proposal and preserved controls. Run the identical `apply` command
   only after explicit approval. A refusal or unavailable identity/access leaves
   activation pending. Disabling the config flag never removes existing remote
   protection; report that preserved remote state for manual review.

6. **Report.** Report generated CI, CodeQL, and Dependabot as configured until
   GitHub checks them live. A branch-protection config flag is intent; report
   only the observed plan/apply result as remote evidence. For trunk
   repositories, checks run after direct pushes and do not block a change before
   it lands. Report `partial` and every untouched conflict when present, give
   one next step, then remove the exact temporary directory:

   ```bash
   rm -rf "${SHIPSHAPE_SESSION_DIR}"
   ```

## Don't

- Don't disable any security feature without stating the consequence and
  getting an explicit yes.
- Don't enable tiered review without stating its consequence and getting an
  explicit yes; don't gate turning it off.
- Don't edit managed files directly to satisfy a customization — config
  first, then regenerate.
- Don't delete files without a per-file confirmation.
- Don't touch settings the user didn't ask about.
- Keep the proposal outside the repository until the user approves the plan.
