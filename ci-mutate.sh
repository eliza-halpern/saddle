#!/usr/bin/env bash
# ci-mutate.sh: two-phase mutation gate for CI.
#
# Phase 1 (the guarantee): every mutant in modules touched by the diff,
# selected via `git diff BASE...HEAD` with tests/test_X.py mapped to
# src/saddle/X.py. Must complete; survivors fail the job.
# Phase 2 (the sampling): two more modules, rotated by commit hash, in the
# remaining budget. Survivors fail; running out of time does not.
#
# Usage: ./ci-mutate.sh BASE_SHA
set -u
cd "$(dirname "$0")" || exit 1

BASE="${1:-}"

# Resolve the diff base: caller-passed SHA, else HEAD~1, else full run.
FULL_RUN=0
if [ -z "$BASE" ] || [[ "$BASE" =~ ^0+$ ]] || ! git rev-parse --verify "$BASE" >/dev/null 2>&1; then
    if git rev-parse --verify HEAD~1 >/dev/null 2>&1; then
        BASE="HEAD~1"
    else
        FULL_RUN=1
    fi
fi

# Map one changed path to a saddle module name (empty when unmapped).
mod_for_file() {
    case "$1" in
        src/saddle/*.py)
            basename "$1" .py
            ;;
        tests/test_*.py)
            local name
            name=$(basename "$1" .py)
            name=${name#test_}
            while [ ! -f "src/saddle/$name.py" ]; do
                case "$name" in
                    *_*) name=${name%_*} ;;
                    *) name=""; break ;;
                esac
            done
            printf '%s' "$name"
            ;;
    esac
}

run_phase() {
    local budget="$1"
    shift
    timeout "$budget" uv run --frozen mutmut run "$@"
    local code=$?
    if [ "$code" -ne 0 ] && [ "$code" -ne 124 ]; then
        echo "mutation run failed (exit $code), not a timeout"
        exit 1
    fi
}

judge() {
    local out
    out=$(uv run --frozen mutmut results)
    echo "$out"
    if echo "$out" | grep -q ": survived$"; then
        echo "FAIL: surviving mutants found"
        exit 1
    fi
    echo "OK: no surviving mutants in scope"
}

if [ "$FULL_RUN" -eq 1 ]; then
    echo "no diff base; running full mutation with a time box"
    run_phase 780
    judge
    exit 0
fi

# JavaScript (StrykerJS, over the changed lines of the diff's non-test .js
# files): any survivor, or a tool that could not run, fails the job.
js_phase() {
    if ! git diff --name-only "$BASE...HEAD" -- '*.js' | grep -q .; then
        return 0
    fi
    if [ -f package-lock.json ] && [ ! -d node_modules/@stryker-mutator/core ]; then
        npm ci --ignore-scripts --no-audit --no-fund || return 1
    fi
    echo "javascript phase: StrykerJS over the diff's changed .js lines"
    uv run --frozen python -c 'import sys; from saddle.jsevidence import main; sys.exit(main(sys.argv[1:]))' "$BASE"
}
js_phase || exit 1

mapfile -t changed < <(git diff --name-only "$BASE...HEAD" -- src/saddle tests)
mods=()
for path in ${changed[@]+"${changed[@]}"}; do
    mod=$(mod_for_file "$path")
    if [ -n "$mod" ] && [ "$mod" != "__init__" ]; then
        mods+=("$mod")
    fi
done
if [ "${#mods[@]}" -gt 0 ]; then
    mapfile -t mods < <(printf '%s\n' "${mods[@]}" | sort -u)
fi

if [ "${#mods[@]}" -eq 0 ]; then
    echo "no Python modules changed; skipping mutation"
    exit 0
fi

echo "phase 1: diff modules: ${mods[*]}"
globs=()
for mod in "${mods[@]}"; do
    globs+=("saddle.$mod*")
done
run_phase 480 "${globs[@]}"

mapfile -t all_mods < <(
    for file in src/saddle/*.py; do
        name=${file##*/}
        name=${name%.py}
        [ "$name" = "__init__" ] || echo "$name"
    done | sort
)
rest=()
for mod in "${all_mods[@]}"; do
    skip=0
    for have in "${mods[@]}"; do
        if [ "$mod" = "$have" ]; then skip=1; break; fi
    done
    if [ "$skip" -eq 0 ]; then rest+=("$mod"); fi
done

if [ "${#rest[@]}" -gt 0 ]; then
    sha=$(git rev-parse HEAD)
    offset=$((16#${sha:0:7} % ${#rest[@]}))
    extra=("${rest[$offset]}")
    if [ "${#rest[@]}" -gt 1 ]; then
        extra+=("${rest[$(((offset + 1) % ${#rest[@]}))]}")
    fi
    echo "phase 2: rotation sample: ${extra[*]}"
    globs=()
    for mod in "${extra[@]}"; do
        globs+=("saddle.$mod*")
    done
    run_phase 300 "${globs[@]}"
else
    echo "phase 2: diff already covers all modules; skipping sample"
fi

judge
