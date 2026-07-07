#!/usr/bin/env bash
# Wrapper for the IPSS-M skill: run ipssm_pipeline.py from the repo root
# regardless of the current working directory.
#
# Usage: run_ipssm.sh <input.csv|xlsx> [pipeline flags...]
#   e.g. run_ipssm.sh cohort.xlsx
#        run_ipssm.sh cohort.csv --screen-only
set -euo pipefail

# This script lives at <repo>/.claude/skills/ipssm/scripts/ -> repo root is 4 levels up.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../../../.." && pwd)"

if [[ ! -f "${repo_root}/ipssm_pipeline.py" ]]; then
  echo "[run_ipssm] ERROR: cannot find ipssm_pipeline.py at repo root: ${repo_root}" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: run_ipssm.sh <input.csv|xlsx> [--screen-only|--translate-only|-v file|--rscript path]" >&2
  exit 2
fi

python_bin="$(command -v python3 || command -v python || true)"
if [[ -z "${python_bin}" ]]; then
  echo "[run_ipssm] ERROR: python3 not found on PATH" >&2
  exit 1
fi

# Absolutize the input file (first arg) so it still resolves after we cd to the
# repo root; leave flags/other args untouched. Outputs land next to the input.
input="$1"; shift
if [[ -e "${input}" ]]; then
  input="$(cd "$(dirname "${input}")" && pwd)/$(basename "${input}")"
fi

cd "${repo_root}"
exec "${python_bin}" ipssm_pipeline.py "${input}" "$@"
