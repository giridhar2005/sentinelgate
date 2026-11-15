#!/usr/bin/env bash
# ==============================================================================
# SentinelGate Production Audit & Release Verification Gate
# Runs all checks locally: linting, formatting, typechecking, tests with 85%
# coverage gate, dependency audit, live HTTP benchmark, link validation,
# and wheel packaging verification in an isolated temporary venv.
# ==============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

# ANSI Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${BOLD}${BLUE}======================================================================${NC}"
echo -e "${BOLD}${BLUE}          SentinelGate Comprehensive Release Audit Suite              ${NC}"
echo -e "${BOLD}${BLUE}======================================================================${NC}"

run_gate() {
    local gate_name="$1"
    shift
    echo -e "\n${BOLD}[*] Executing Gate: ${gate_name}...${NC}"
    if "$@"; then
        echo -e "${GREEN}[✔] PASS: ${gate_name}${NC}"
    else
        echo -e "${RED}[✘] FAIL: ${gate_name}${NC}"
        echo -e "${RED}[!] Release audit failed at gate: ${gate_name}${NC}"
        exit 1
    fi
}

# Gate 1: Code Linting
run_gate "Ruff Linter Checks" uv run ruff check sentinelgate tests scripts

# Gate 2: Code Formatting
run_gate "Ruff Formatting Checks" uv run ruff format --check sentinelgate tests scripts

# Gate 3: Type Checking
run_gate "Mypy Type Checking" uv run mypy sentinelgate

# Gate 4: Supply Chain Vulnerability Scan
run_gate "Pip-Audit Dependency Vulnerability Scan" uv run pip-audit

# Gate 5: Pytest Suite with Coverage (>=85% Gate)
run_gate "Pytest Suite (85% Coverage Gate)" uv run pytest --cov=sentinelgate --cov-fail-under=85

# Gate 6: Live HTTP Gateway Benchmark (<30s)
run_gate "Live HTTP Quick Benchmark" uv run python benchmarks/bench.py --quick

# Gate 7: Speculative Hedging Simulation Benchmark
run_gate "Speculative Latency Hedging Benchmark" uv run python benchmarks/bench_hedging.py

# Gate 8: Benchmark Documentation Sync
run_gate "BENCHMARKS.md Documentation Generator" uv run python scripts/generate_benchmarks_doc.py

# Gate 9: Markdown Link & Prose Quality Verification
run_gate "Markdown Link & Prose Validation" python3 -c "
import re, os, sys

docs = ['README.md', 'BENCHMARKS.md', 'docs/SECURITY.md', 'docs/INTEGRATIONS.md', 'docs/CONFIGURATION.md']
banned = ['revolutionary', 'cutting-edge', 'game-changer', 'seamless', 'robust', 'empower', 'delve', 'next-gen']

for doc in docs:
    if not os.path.exists(doc):
        continue
    with open(doc) as f:
        text = f.read()
    # Check banned words
    for w in banned:
        if re.search(r'\b' + re.escape(w) + r'\b', text, re.IGNORECASE):
            print(f'ERROR: Banned word \"{w}\" found in {doc}')
            sys.exit(1)
    # Check links
    links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', text)
    for label, target in links:
        if target.startswith('http') or target.startswith('#') or target.startswith('mailto:'):
            continue
        clean = target.split('#')[0]
        if not clean:
            continue
        if not os.path.exists(clean) and not os.path.exists(os.path.join(os.path.dirname(doc), clean)):
            print(f'ERROR: Broken link \"{target}\" found in {doc}')
            sys.exit(1)
print('All markdown docs validated successfully.')
"

# Gate 10: Clean Packaging Build & Isolated Venv Installation
run_gate "Packaging Build & Clean Venv Installation" bash -c '
    set -euo pipefail
    TMP_VENV=$(mktemp -d /tmp/sg_audit_venv_XXXXXX)
    trap "rm -rf ${TMP_VENV}" EXIT
    rm -rf dist/ build/ *.egg-info
    uv build --wheel
    uv venv "${TMP_VENV}" --python 3.12
    uv pip install --python "${TMP_VENV}/bin/python" dist/*.whl
    "${TMP_VENV}/bin/sentinelgate" --help > /dev/null
    "${TMP_VENV}/bin/python" -m sentinelgate --help > /dev/null
'

echo -e "\n${BOLD}${GREEN}======================================================================${NC}"
echo -e "${BOLD}${GREEN}  [✔] ALL 10 GATES PASSED! SENTINELGATE RELEASE AUDIT SUCCEEDED.      ${NC}"
echo -e "${BOLD}${GREEN}======================================================================${NC}"
