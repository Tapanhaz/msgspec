#!/usr/bin/env bash
# Build msgspec with ThreadSanitizer inside nascheme/cpython-tsan and run tests/unit.
set -euxo pipefail
cd "$(dirname "$0")/.."

export PYTHON_GIL=0 MSGSPEC_TSAN=1
export CC="${CC:-clang-21}" CXX="${CXX:-clang++-21}"
export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+tsan
export MSGSPEC_STRESS=1
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
# CPython's own _decimal races, unrelated to msgspec
race:dec_addstatus
race:dec_hash
EOF
mkdir -p tsan-logs pytest-logs
rm -f tsan-logs/* pytest-logs/*

STRESS=tests/unit/test_free_threading.py
COMMON=(tests/unit --ignore=tests/unit/test_performance.py --ignore="${STRESS}")

tsan_opts() { 
  echo "suppressions=/tmp/supp.txt:external_symbolizer_path=${SYMBOLIZER}:halt_on_error=0:exitcode=0:history_size=7:second_deadlock_stack=1:handle_abort=1:log_path=$PWD/tsan-logs/$1"
}
  
LAST_RC=0
run_phase() {
local phase=$1; shift
LAST_RC=0
TSAN_OPTIONS="$(tsan_opts "${phase}")" \
  python -X faulthandler -m pytest "$@" -p no:randomly -q 2>&1 \
  | tee "pytest-logs/${phase}.txt" || LAST_RC=${PIPESTATUS[0]}
  echo "${phase}: pytest exit code ${LAST_RC}" >> pytest-logs/exit-codes.txt
}
  
isolate_stress() {
  local ids n=0 rc id
  mapfile -t ids < <(python -m pytest --collect-only -q -p no:randomly "${STRESS}" 2>/dev/null | grep '::' || true)
  : > pytest-logs/stress-isolated.txt
  for id in "${ids[@]}"; do
    n=$((n + 1)); rc=0
    TSAN_OPTIONS="$(tsan_opts "stress-${n}")" \
      timeout 900 python -X faulthandler -m pytest "${id}" -p no:randomly -q \
      > "pytest-logs/stress-${n}.txt" 2>&1 || rc=$?
    echo "${rc} ${id}" >> pytest-logs/stress-isolated.txt
  done
}

STRESS_CRASHED=0
run_phase stress "${STRESS}"

if [ "${LAST_RC}" -ge 124 ]; then
  STRESS_CRASHED=1
  isolate_stress
fi
run_phase baseline "${COMMON[@]}"

shopt -s nullglob
logs=(tsan-logs/*)
{
  echo "## pytest exit codes per phase (0=ok, 1=failures, 134=abort, 139=segfault, 124=timeout)"
  cat pytest-logs/exit-codes.txt
  if [ -s pytest-logs/stress-isolated.txt ]; then
    echo
    echo "## stress tests rerun one per process (exit code, test); non-zero means it failed or crashed"
    grep -v '^0 ' pytest-logs/stress-isolated.txt || echo "(all passed in isolation)"
  fi
  echo
  echo "## TSAN unique race sites"
  if [ ${#logs[@]} -eq 0 ]; then
    echo "(none: no TSAN reports)"
  else
    grep -h "SUMMARY: ThreadSanitizer" "${logs[@]}" | sort | uniq -c | sort -rn || true
    echo
    echo "## Reports by first msgspec frame (_core.c)"
    awk 'BEGIN{RS="==================\n"}
         /WARNING: ThreadSanitizer/ {
           if (match($0, /[^ ]*_core\.c:[0-9]+/)) print substr($0, RSTART, RLENGTH)
           else print "(no _core.c frame)"
         }' "${logs[@]}" | sort | uniq -c | sort -rn
  fi
  for f in "${logs[@]}"; do
    if grep -q "ERROR: ThreadSanitizer\|CHECK failed" "${f}"; then
      echo
      echo "## fatal signal / TSAN failure in ${f} (first 60 lines)"
      head -60 "${f}"
    fi
  done
} | tee tsan-summary.txt | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"

status=0
[ ${#logs[@]} -eq 0 ] || ! grep -q "SUMMARY: ThreadSanitizer" "${logs[@]}" || status=1
[ "${STRESS_CRASHED}" -eq 0 ] || status=1
exit "${status}"