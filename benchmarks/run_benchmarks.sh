#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

PYTHON_BIN=".venv/bin/python"
if [[ ! -f "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

echo "=========================================================="
echo " Starting SentinelGate Automated Benchmark Suite..."
echo "=========================================================="

"${PYTHON_BIN}" "${ROOT_DIR}/benchmarks/load_test.py" --iterations 1000 --concurrency 50 --output "${ROOT_DIR}/benchmarks/benchmark_results.json"

echo "=========================================================="
echo " Benchmark suite completed successfully."
echo " Results written to benchmarks/benchmark_results.json"
echo "=========================================================="
