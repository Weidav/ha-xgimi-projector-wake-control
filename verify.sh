#!/bin/sh
# Runs local validation checks.
# Every step runs even if a previous one fails; the script exits non-zero if any step failed.

failed=""

run() {
    echo "==> $*"
    if ! "$@"; then
        failed="${failed}   - $*\n"
    fi
}

run uv run ruff check custom_components/xgimi tests/
run uv run ruff format --check custom_components/xgimi tests/

run uv run pyright custom_components/xgimi

run uv run deptry custom_components/xgimi

run uv run pytest --cov=custom_components/xgimi --cov-report=term

if [ -n "$failed" ]; then
    echo
    echo "Failed steps:"
    printf "%b" "$failed"
    exit 1
fi

echo
echo "All checks passed."
