---
name: ipssm
description: Batch-calculate IPSS-M (Molecular International Prognostic Scoring System) risk scores for MDS patients from a local CSV or Excel file. Use when the user wants to score, risk-stratify, or run IPSS-M / IPSS-M risk-score / confidence levels on a cohort spreadsheet locally (no web upload). Handles cohort-format auto-detection, karyotype parsing, data validation, and the official R `ipssm` scenario analysis for missing cytogenetics.
---

# IPSS-M Local Batch Scoring

Run the local `ipssm_pipeline.py` to turn a patient CSV/Excel into IPSS-M risk
scores + confidence levels. This replaces the Streamlit web-upload flow — everything
runs on the user's machine via the official R `ipssm` package.

## When to use

Trigger when the user points at a spreadsheet of MDS patients and asks to compute
IPSS-M (a.k.a. "risk score", "分數", "風險分層", "confidence level"). Typical inputs:
a `.csv` or `.xlsx` with columns like `ID, HB, PLT, BM_BLAST, ...` and/or a raw
`karyotype` column, possibly from a hospital cohort (FJUH / HSCT layouts are
auto-detected).

## Step 0 — Locate the pipeline

The pipeline lives at the **repo root** as `ipssm_pipeline.py`. Either `cd` to the
repo root before running, or use the bundled wrapper which finds the root itself:

```bash
bash .claude/skills/ipssm/scripts/run_ipssm.sh <input.csv|xlsx> [flags]
```

## Step 1 — Check dependencies (do this before the first run)

**Python** (screener + Excel writing):
```bash
python3 -c "import pandas, openpyxl" 2>/dev/null || pip install -r requirements.txt
```

**R engine** (the default, offline, scenario-analysis-capable engine):
```bash
# Is Rscript on PATH?  (override with IPSSM_RSCRIPT=/path/to/Rscript if needed)
command -v Rscript || echo "MISSING_R"
# Is the ipssm package installed?
Rscript -e 'if(!requireNamespace("ipssm", quietly=TRUE)) quit(status=1)' && echo "ipssm OK" || echo "MISSING_IPSSM"
```

Act on the result:
- **`MISSING_R`** → tell the user to install R ≥ 4.3 (macOS: `brew install r`;
  Debian/Ubuntu: `sudo apt install r-base`; Windows: CRAN installer). If Rscript is
  installed but not on PATH, they can set `IPSSM_RSCRIPT=/full/path/to/Rscript`.
- **`MISSING_IPSSM`** → install the package once:
  ```bash
  Rscript install.R          # uses remotes::install_github("papaemmelab/ipssm")
  # or directly:
  Rscript -e 'remotes::install_github("papaemmelab/ipssm", upgrade="never")'
  ```
  (First install can take several minutes while it compiles dependencies.)

Do **not** silently fall back to any web API — this skill is the local R engine.
If the user explicitly wants the network REST API instead, that path still lives in
`streamlit_app.py`. See **Engine consistency disclaimer** below before mixing results
from the two.

## Step 1.5 — Scan & confirm column mapping (messy / unfamiliar files)

Real-world source files rarely use the exact `STANDARD_COLUMNS` names. The pipeline
auto-maps common aliases and unit-annotated headers (e.g. `"Hemoglobin (g/dL)"`,
`"Platelet_count"`) via `COLUMN_ALIASES`, and now attempts this mapping on **every**
file — not only ones that look like a known cohort format. Still, always preview
before computing on a file you haven't already confirmed is clean:

```bash
python3 ipssm_pipeline.py <input.csv|xlsx> --inspect
```

This is a **read-only preview** — it writes no files and runs no R. It reports:
- **確定匹配 (matched)** — columns confidently mapped to a standard field.
- **必填欄位缺失 (missing required)** — `HB`/`PLT`/`BM_BLAST` not matched → those
  patients would be **skipped entirely** if you proceed as-is.
- **選填欄位缺失 (missing optional)** — other standard fields not found → safely
  defaulted to `NA` (fine, most cohorts don't have all 42 fields).
- **未對應到任何標準欄位 (unmapped input columns)** — source columns nothing matched;
  could be irrelevant metadata, or a real field under an alias not yet in
  `COLUMN_ALIASES`.
- A **unit sanity warning** if the column mapped to `HB` looks like it's in g/L
  rather than the required g/dL (median > 25) — this is a heuristic hint, not an
  automatic conversion.

Decide what to do next:
- If there are **no missing-required fields** and nothing suspicious in the unmapped
  list, proceed straight to Step 2.
- If there **are** missing-required fields or an unmapped column that plausibly is
  one of them (e.g. an orphaned `"Hgb"` column), use `AskUserQuestion` to show the
  user the mapping table and ask them to confirm the correct column, or to point out
  which source column represents each missing required field, before running the
  full pipeline. Once confirmed, either rename that column in a working copy of the
  file before feeding it to the pipeline, or — if this hospital format will recur —
  offer to add the new alias to `COLUMN_ALIASES` in `ipssm_pipeline.py` so future
  runs pick it up automatically.
- Never silently proceed past a missing required field without telling the user —
  those patients get dropped from the results.

## Step 2 — Run

Full flow (validate → R risk calc → Excel):
```bash
python3 ipssm_pipeline.py <input.csv|xlsx>
```
Useful flags:
- `--screen-only` — just validate + clean, no R (fast sanity check of the input).
- `--translate-only` — input is an already-cleaned CSV; only run R.
- `-v validation.xlsx` — compare results against a manual validation sheet (adds an
  `Analysis` sheet).
- `--rscript /path/to/Rscript` — force a specific R.

## Step 3 — Report results

Outputs are written next to the input file:
- `<stem>_cleaned.csv` — the validated 42-column input that went into R.
- `<stem>_screening_log.txt` — validation report (auto-fixes, skipped patients, errors).
- `<stem>_cleaned_results.xlsx` — **the deliverable**, with sheets:
  - `Summary` — `ID` + `Confidence_Level` (UNCERTAIN rows highlighted red).
  - `R_Full_Output` — full R scores/categories per patient.
  - `Analysis` — only when `-v` was supplied.

Then:
1. Read the screening log and the console summary; tell the user how many patients
   **passed**, how many were **SKIPPED** (and why — e.g. missing HB/PLT/BM_BLAST), and
   the **CONFIDENT vs UNCERTAIN** counts.
2. Explain confidence: `Range = IPSSMscore_worst − IPSSMscore_best`; `Range < 1` →
   **CONFIDENT**, `≥ 1` → **UNCERTAIN** (missing key data spans multiple risk
   categories — interpret with care).
3. Hand the results Excel to the user with `SendUserFile` (the `<stem>_cleaned_results.xlsx`).

## Engine consistency disclaimer

This skill exclusively uses the **local R engine** (`ipssm_pipeline.py` →
official R `ipssm` package). The Streamlit web app additionally offers an
**official REST API engine** (`api.mds-risk-model.com`). These are two
**independently maintained implementations** of IPSS-M:
- They may not stay version-synced, and are not guaranteed to produce
  **numerically identical** scores even when both are given complete data.
- The REST API additionally **requires** `CYTO_IPSSR` and errors on missing
  cytogenetics, whereas the R engine runs scenario analysis (best/mean/worst)
  for missing values — so results can differ for structural reasons too.
- **Do not mix results from both engines** within the same report or cohort
  analysis; always label which engine produced a given set of scores. If asked
  to reconcile numbers between the CLI/skill output and the web app, treat
  this as the expected difference, not a bug — check `README.md`'s "計算引擎
  差異聲明" section for the full statement.

## Data & compliance (state briefly to the user)

Per the MSKCC IPSS-M terms mirrored in `README.md`: data must be **de-identified**
(no PHI), results are **for academic research only** — not for clinical diagnosis,
treatment, or medical reports. The pipeline never transmits the `ID` column anywhere;
the R engine runs fully locally.

## Reference

Field specs, NA handling, karyotype parsing rules, and cohort auto-detection are
documented in `SCREENER_REFERENCE.md` at the repo root — consult it when a patient is
skipped or a value looks wrong.
