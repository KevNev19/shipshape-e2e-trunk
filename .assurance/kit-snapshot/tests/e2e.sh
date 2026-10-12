#!/usr/bin/env bash
# End-to-end check of the shipshape pipeline against a scratch consumer repo.
# All candidates and reviewed plans stay outside the repository until apply.
set -euo pipefail

KIT="$(cd "$(dirname "$0")/.." && pwd)"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/shipshape-e2e.XXXXXX")"
consumer="$scratch/consumer"
session="$scratch/session"
detect="$session/detect.json"
candidate="$session/config.json"
approved_candidate="$session/config.approved.json"
plan="$session/plan.json"
stale_result="$session/stale.json"
trap 'rm -rf "$scratch"' EXIT

mkdir -p "$consumer/src" "$session"
cd "$consumer"
git init -qb main
printf '[project]\nname = "e2e-demo"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n' > pyproject.toml
mkdir tests
printf 'def add(left: int, right: int) -> int:\n    return left + right\n' > src/calc.py
printf 'from src.calc import add\n\ndef test_addition():\n    assert add(19, 23) == 42\n' > tests/test_calc.py
python3 -m pytest -q
git status --short --untracked-files=all > "$session/repository-before.txt"

echo "== detect =="
python3 "$KIT/scripts/detect.py" . --no-network > "$detect"
python3 -c 'import json, pathlib, sys; d=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert d["languages"][0]["name"] == "python", d' "$detect"

echo "== outside candidate + saved preview =="
python3 "$KIT/scripts/render.py" init-config . \
  --detect "$detect" --set features.branch_protection=false \
  --output "$candidate" > "$session/proposed.json"
git status --short --untracked-files=all > "$session/repository-after-proposal.txt"
cmp "$session/repository-before.txt" "$session/repository-after-proposal.txt"
test ! -e ".sdlc"

python3 "$KIT/scripts/render.py" plan . --config "$candidate" > "$plan"
plan_digest="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["plan_digest"])' "$plan")"
git status --short --untracked-files=all > "$session/repository-after-preview.txt"
cmp "$session/repository-before.txt" "$session/repository-after-preview.txt"
test ! -e ".sdlc"
cp "$candidate" "$approved_candidate"

echo "== stale candidate refusal =="
python3 - "$candidate" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
config = json.loads(path.read_text())
config["commands"]["test"] = "python3 -m pytest tests -q"
path.write_text(json.dumps(config, indent=2) + "\n")
PY
if python3 "$KIT/scripts/render.py" apply . \
  --config "$candidate" --expected-plan-digest "$plan_digest" > "$stale_result"; then
  echo "FAIL: changed candidate was applied with an earlier preview digest" >&2
  exit 1
fi
python3 -c 'import json, pathlib, sys; r=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert r["status"] == "stale-plan" and r["written"] == [] and not r["state_written"], r' "$stale_result"
git status --short --untracked-files=all > "$session/repository-after-stale.txt"
cmp "$session/repository-before.txt" "$session/repository-after-stale.txt"
test ! -e ".sdlc"
cp "$approved_candidate" "$candidate"

echo "== apply reviewed candidate =="
python3 "$KIT/scripts/render.py" apply . \
  --config "$candidate" --expected-plan-digest "$plan_digest" > "$session/apply.json"
python3 -c 'import json, pathlib, sys; r=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert r["ok"] and r["binding"]["status"] == "matched" and len(r["written"]) >= 10, r' "$session/apply.json"

echo "== hook install + guard blocks a planted key =="
bash ".sdlc/hooks/install.sh" > /dev/null
key="AKIA""AAAAAAAAAAAAAAAA"
printf 'ACCESS_KEY = "%s"\n' "$key" > "src/config.py"
git add "src/config.py"
if bash ".sdlc/hooks/secret-guard.sh" 2>/dev/null; then
  echo "FAIL: guard did not block a planted key" >&2
  exit 1
fi
git reset -q
rm "src/config.py"

echo "== deterministic idempotent re-apply =="
python3 "$KIT/scripts/render.py" plan . > "$session/idempotent-plan-1.json"
python3 "$KIT/scripts/render.py" plan . > "$session/idempotent-plan-2.json"
digest_one="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["plan_digest"])' "$session/idempotent-plan-1.json")"
digest_two="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["plan_digest"])' "$session/idempotent-plan-2.json")"
test "$digest_one" = "$digest_two"
cp ".sdlc/state.json" "$session/state-before-idempotent-apply.json"
python3 "$KIT/scripts/render.py" apply . \
  --expected-plan-digest "$digest_one" > "$session/apply-idempotent.json"
python3 -c 'import json, pathlib, sys; r=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert r["binding"]["status"] == "matched" and r["written"] == [] and r["removed"] == [], r' "$session/apply-idempotent.json"
cmp "$session/state-before-idempotent-apply.json" ".sdlc/state.json"

echo "== doctor =="
python3 "$KIT/scripts/doctor.py" . > "$session/doctor.json" \
  || { echo "FAIL: doctor reported failures" >&2; cat "$session/doctor.json" >&2; exit 1; }
python3 -c 'import json, pathlib, sys; d=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert d["ok"] and d["counts"]["FAIL"] == 0, d' "$session/doctor.json"

echo ""
echo "All mechanical checks passed."
echo ""
echo "Optional local Claude plugin-dir trial (no global plugin change):"
echo "  claude --plugin-dir \"$KIT\""
