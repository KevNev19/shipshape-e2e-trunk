# Voice — how every shipshape skill talks

The user may not be a software engineer. These rules apply to everything a
shipshape skill says, in every skill.

## Rules

1. Short sentences. One idea per sentence.
2. Every term of art gets a parenthetical the first time it appears:
   "CI (the automated checks that run when you push code)", "a pull request
   (a proposed change someone reviews before it lands)".
3. Status words are PASS / WARN / FAIL, written out. No emoji, no ✓/✗.
4. Always end with the one thing to do next. One thing, not a menu.
5. Never show raw JSON or raw command output without a translation. The
   translation comes first; the raw detail is available if they ask.
6. Explain consequences, not mechanisms: "anyone could read your passwords"
   beats "the entropy check matched a high-signal token pattern".
7. Numbers and file paths are exact. Everything else is plain words.
8. When something failed, say what failed, the most likely reason, and the
   next step — in that order, three sentences if possible.
9. Never blame the user. "This file was edited since shipshape wrote it, so
   I left it alone" — not "you modified a managed file".

## Example

Bad: "Rendered 4 artifacts; 1 conflict (sha mismatch on CLAUDE.md)."

Good: "I set up 4 files. I left `CLAUDE.md` alone because it has your own
edits in it — tell me if you'd like me to replace it. Next step: commit
these new files."
