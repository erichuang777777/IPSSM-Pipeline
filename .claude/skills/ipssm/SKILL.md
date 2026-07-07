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
`streamlit_app.py`.

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

## Data & compliance (state briefly to the user)

Per the MSKCC IPSS-M terms mirrored in `README.md`: data must be **de-identified**
(no PHI), results are **for academic research only** — not for clinical diagnosis,
treatment, or medical reports. The pipeline never transmits the `ID` column anywhere;
the R engine runs fully locally.

## Reference

Field specs, NA handling, karyotype parsing rules, and cohort auto-detection are
documented in `SCREENER_REFERENCE.md` at the repo root — consult it when a patient is
skipped or a value looks wrong.
