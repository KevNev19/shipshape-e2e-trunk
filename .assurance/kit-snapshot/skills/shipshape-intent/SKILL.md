---
name: shipshape-intent
description: Capture a short, durable change intent for non-trivial work through four questions and an explicit confirmation.
---

# shipshape-intent

Help the user record a shared target for one non-trivial change. Follow the
[voice rules](../shipshape-init/references/voice.md). The helper owns validation
and file generation; this skill sequences the conversation and translates its
JSON response.

Use this entrypoint only when the user requests a durable change intent.

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

1. **Lead with the escape hatch.** Ask whether this is a trivial change, such
   as correcting a typo. If yes, recommend no artifact and stop: "No intent
   file is needed. Next step: make the small change."

2. **Ask four questions, one at a time.** Wait for each answer before asking
   the next:
   - What problem needs to be solved, and why does it matter?
   - What constraints (limits the change must respect) apply?
   - What are the non-goals (things this change deliberately will not do)?
   - What observable checks will show the change is complete?

3. **Preview and confirm.** Propose a short lowercase kebab-case slug. Show the
   exact `docs/changes/YYYY-MM-DD-<slug>.md` destination with today's date and
   the complete file content. Explain that the file belongs to the author and
   may change freely until implementation begins. After implementation begins,
   changes are appended as dated amendments; the existing intent stays intact.
   Ask whether to create the shown file. Continue only after an explicit yes.

4. **Create through the helper.** Write the four answers to a temporary JSON
   file with the keys `problem`, `constraints`, `non_goals`, and
   `acceptance_checks`; the last value is a list of strings. Run:

   ```bash
   python3 "${SHIPSHAPE_ROOT}/scripts/intent.py" create . \
     --slug "<slug>" --input "<temp-json>"
   ```

   Remove the temporary file. If the JSON says `ok: false`, say what failed
   and the most likely reason, then translate its `next_action` in plain
   language and stop.

5. **Report completion.** Say which path was created and give the JSON's one
   next action. Do not add more next steps.
