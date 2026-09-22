#!/bin/bash
# QueryForge verification entry point.
#
# One command that answers "is this checkout healthy?". It checks the environment,
# runs the full offline test suite, and reports repository state. It exits non-zero
# on any failure, so a red baseline is impossible to miss.
#
# Everything here is offline: no network, no model calls, no API spend.
#
# Usage:
#   ./init.sh              full: environment + test suite + repository state
#   ./init.sh --quick      environment + repository state only (skip the ~65s suite)
#   ./init.sh --eval       additionally run the offline tier-1 evaluation gate
#   ./init.sh --help

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

QUICK=0
RUN_EVAL=0
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    --eval)  RUN_EVAL=1 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

fail() { echo ""; echo "FAILED: $*" >&2; exit 1; }

echo "=== QueryForge verification ==="
echo "root: $ROOT"
echo

# ---------------------------------------------------------------- environment
echo "--- environment ---"
[ -f "$ROOT/AGENTS.md" ]        || fail "AGENTS.md missing"
[ -f "$ROOT/pyproject.toml" ]   || fail "pyproject.toml missing — wrong directory?"
[ -d "$ROOT/.venv" ]            || fail ".venv missing — create it with: python3.11 -m venv .venv && .venv/bin/pip install -e ."
[ -x "$ROOT/.venv/bin/python" ] || fail ".venv/bin/python not executable"

PYVER="$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
  3.11|3.12) echo "python: $PYVER (supported)" ;;
  *) echo "python: $PYVER — WARNING: project targets 3.11 or 3.12" ;;
esac

if [ -f "$ROOT/.env" ]; then
  echo "env:    .env present (real model calls possible)"
else
  echo "env:    .env absent — offline checks only; real-model runs need provider credentials"
  echo "        copy .env.example to .env and fill in one provider key"
fi

# --------------------------------------------------------- leaked-secret guard
# .env is gitignored; a tracked .env means someone force-added it. Fail loudly:
# this repository is public-facing and .env holds live provider credentials.
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  fail ".env is tracked by git — remove it from the index (it holds API keys)"
fi
echo "secret: .env not tracked (ok)"
echo

# ------------------------------------------------------------- repository state
echo "--- repository state ---"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "working tree: dirty ($(git status --porcelain | wc -l | tr -d ' ') changed paths)"
else
  echo "working tree: clean"
fi
if git rev-parse --verify origin/HEAD >/dev/null 2>&1; then
  echo "branch:       $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
fi
echo

# --------------------------------------------------------------- verification
if [ "$QUICK" -eq 1 ]; then
  echo "--- test suite SKIPPED (--quick) ---"
else
  echo "--- full test suite ---"
  LOG_LEVEL=CRITICAL .venv/bin/python -m unittest discover -s tests -q 2>&1 | tail -5 \
    || fail "test suite failed — repair the baseline before adding scope"
  echo
fi

if [ "$RUN_EVAL" -eq 1 ]; then
  echo "--- offline tier-1 evaluation gate ---"
  LOG_LEVEL=CRITICAL .venv/bin/python -B scripts/benchmark_agent.py --tier 1 --report /tmp/qf_tier1_init.json 2>&1 | tail -3 \
    || fail "tier-1 evaluation gate failed"
  echo
fi

echo "=== verification complete ==="
echo
echo "Static / build checks NOT run by this script — run them when you touch these areas:"
echo "  make check            repository hygiene + offline acceptance (Python)"
echo "  make web-check        Studio lint + typecheck + build + tests (TypeScript)"
echo "  make semantic-check   semantic-model drift vs the sample database"
echo
echo "Real-model evaluation spends money. Ask before running tier 3:"
echo "  .venv/bin/python -B scripts/benchmark_agent.py --tier 3 --model-provider deepseek --model deepseek-v4-flash"
