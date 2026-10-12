# Interview — the questions shipshape-init asks

Ask about outcomes, never tools. Skip any question the scan already answered
with high confidence — confirm instead of asking cold. Five questions
maximum; fewer is better.

## The questions

1. **Who works on this?**
   "Is it just you pushing changes, or do other people work on this project
   too?"
   → maps to `profile=solo` or `profile=team`.

2. **How should changes land?** (only if profile=team, or the user seems
   unsure)
   "When someone makes a change, should it go straight in, or should another
   person look at it first?"
   → maps to `workflow_style=trunk` (straight in, protected by automated
   checks) or `workflow_style=pr` (reviewed first).
   For `profile=solo`, default to trunk and just say so in one line.

3. **How do you check it works?**
   Confirm the detected test command ("It looks like `pytest` runs your
   tests — right?"). If nothing was detected, ask whether they have any way
   of checking the project works today. If they have none, say the setup
   will include a placeholder and that adding a first real test is a good
   early goal.
   → maps to `commands.test=...`.

4. **Anything that must never leak?**
   "Does this project handle anything sensitive — passwords, API keys,
   customer data, personal information?"
   The answer doesn't change the file set (security guardrails are always
   on) — it changes how strongly you talk about the secret guard at close,
   and it belongs in the design doc's description.

5. **Public or private?**
   "Is this project public on GitHub (or will it be), or private?"
   Affects what to say about CodeQL availability later (free on public
   repos). Record it; don't act on it in v1.

## Defaults if the user just says "use the defaults"

solo, trunk, detected test command (or placeholder), guardrails all on.
Say the four choices back in one sentence and move on.
