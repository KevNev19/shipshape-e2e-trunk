# Finding contract

Every review report follows this order. The reviewer reads the full diff
internally but does not narrate it file by file. Findings-first changes what
the human reads, not what the reviewer inspects.

## 1. Intent

Say whether an intent artifact (a written record of what the change is meant
to do) is available. Look for intent artifacts under `docs/changes/`. Link the
artifact or originating issue (the task that led to the change) when one
exists. When neither exists, say: "No intent artifact was found. This review
judged the change on its own terms."

## 2. Verified findings

Report verified findings in severity order. Each finding has:

- evidence: a check result, or an exact file and line;
- the consequence in plain language; and
- exactly one next action.

If there are none, say "No verified findings." Do not invent a finding to
fill the section.

## 3. Intent conformance (match to the request)

Say whether the change does what was asked, and only that. Name any
unrequested functionality: behaviour beyond the stated intent.

## 4. Material uncertainties (important things that could not be verified)

State what could not be verified. "No verified findings" never means "no
risk." Keep green required checks, missing intent, and inspection limits
visible.

## 5. Verdict

End on exactly one verdict: "Looks safe to merge", "Needs attention first: <the one thing>",
or "Do not merge: <reason>".

Any secret in the diff is an automatic "Do not merge".
Red checks can never be called "looks safe" — ever.
