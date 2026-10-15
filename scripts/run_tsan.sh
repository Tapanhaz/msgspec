#!/usr/bin/env bash
# Build msgspec with ThreadSanitizer inside nascheme/cpython-tsan and run tests/unit.
set -euxo pipefail
cd "$(dirname "$0")/.."

export PYTHON_GIL=0 MSGSPEC_TSAN=1
export CC="${CC:-clang-21}" CXX="${CXX:-clang++-21}" 
export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+tsan
trap 'chown -R --reference=. . 2>/dev/null || true' EXIT

python -c "import sys; assert not sys._is_gil_enabled()"

SYMBOLIZER=$(command -v llvm-symbolizer-21 || command -v llvm-symbolizer || true)
if [ -z "${SYMBOLIZER}" ] || ! command -v "${CC}" > /dev/null; then
  echo "::error::CC='${CC}' or llvm-symbolizer not found"
  ls /usr/bin | grep -Ei 'clang|llvm' || true
  exit 1
fi
"${CC}" --version

python -m pip install -q -U pip
python -m pip install -q "setuptools>=80" "setuptools-scm>=8" pytest-run-parallel
python -m pip install -q --group test-unit
python -m pip install -q --no-build-isolation -e .

nm -D src/msgspec/_core*.so > /tmp/nm.txt
grep -q __tsan /tmp/nm.txt
python -c "import msgspec, sys; assert not sys._is_gil_enabled(), 'import re-enabled the GIL'"

cat /work/tsan_suppressions/*.txt > /tmp/supp.txt
cat >> /tmp/supp.txt <<'EOF'
race:dec_addstatus
race:dec_hash
EOF
mkdir -p tsan-logs
rm -f tsan-logs/*

run_phase() {
  local phase=$1; shift
  TSAN_OPTIONS="suppressions=/tmp/supp.txt:external_symbolizer_path=${SYMBOLIZER}:halt_on_error=0:exitcode=0:history_size=7:second_deadlock_stack=1:log_path=$PWD/tsan-logs/$phase" \
    pytest tests/unit --ignore=tests/unit/test_performance.py \
      -p no:randomly -q "$@" || true
}

run_phase baseline
run_phase parallel --parallel-threads=4 --iterations=5

{
  echo "## TSAN unique race sites"
  grep -h "SUMMARY: ThreadSanitizer" tsan-logs/* 2>/dev/null | sort | uniq -c | sort -rn
} | tee tsan-summary.txt | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"

! grep -q "SUMMARY: ThreadSanitizer" tsan-logs/* 2>/dev/null