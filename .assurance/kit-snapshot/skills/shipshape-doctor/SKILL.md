---
name: shipshape-doctor
description: Read-only health check of a repository's SDLC setup — security guardrails first, then process files, drift, and version staleness, ending with the one thing to do next. Use when the user says "shipshape doctor", "is my repo set up properly", "check this repo's setup", or "is everything still protected".
---

# shipshape-doctor

Report the health of the current repository's setup in plain language.
Strictly read-only: this skill never writes, fixes, or installs anything —
it recommends, and other skills act. Follow the voice rules in
[../shipshape-init/references/voice.md](../shipshape-init/references/voice.md).

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

1. **Run the check.**

   ```bash
   python3 "${SHIPSHAPE_ROOT}/scripts/doctor.py" .
   ```

   If the JSON says `set_up: false`, tell the user this repo has no
   shipshape setup yet and that `/shipshape-init` is the next step. Stop.

2. **Render the bounded scorecard.** Security leads. Give each check's status,
   name, and translated detail without widening the claim. Static hook wiring
   is not an observed scanner run; workflow/configuration presence is not a
   successful run or an alert query. For ordinary trunk setup, say CI is
   configured after push and provides no proved pre-landing enforcement.
   Treat `features.branch_protection` as local intent until the `remote
   protection` check verifies the configured trunk, ordinary PR, or tiered PR
   mode through the shared read-only inspector. Missing helper, CLI, API, or
   exact-typed configuration evidence is not proof and must remain a failure
   or an explicitly unknown result.
   A partial setup apply is incomplete; report its pending conflicts/errors,
   rather than folding them into the harmless user-edit case.
   The `remote protection` result also checks that the privileged auto-merge
   workflow is present only for a valid tiered PR configuration; helper
   presence by itself grants no authority.

3. **Close with the one next action** from the JSON's `next_action` field —
   one thing, not a list. If everything passes, say so plainly and stop.

## Don't

- Don't fix anything, however small — doctor diagnoses, init/customize treat.
- Don't bury a FAIL under a wall of PASSes; failures lead.
- Don't report ordinary drift after a completed setup as a problem — editing
  managed files is fine. A conflict recorded by a partial apply is incomplete,
  not ordinary drift.
- Don't dump the raw JSON; translate it.
- Don't invent checks the script didn't run.
