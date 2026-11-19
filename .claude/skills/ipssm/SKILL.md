---
name: ipssm
description: Batch-calculate IPSS-M (Molecular International Prognostic Scoring System) risk scores for MDS patients from a local CSV or Excel file. Use when the user wants to score, risk-stratify, or run IPSS-M / IPSS-M risk-score / confidence levels on a cohort spreadsheet locally (no web upload). Four stages with a user confirmation gate - field-level normalization (optional local/cloud LLM help for unparseable cells), trial calculation of risk uncertainty from missing values (UNCERTAIN list), a standardized dataset for the user to review, then the official R `ipssm` calculation with Best/Mean/Worst scenarios.
---

# IPSS-M Local Batch Scoring

Run the local `ipssm_pipeline.py` to turn a patient CSV/Excel into IPSS-M risk
scores + confidence levels. Everything
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
bash .claude/skills/ipssm/scripts/run_ipssm.sh <input.csv|xlsx> [flags]   # same flags as ipssm_pipeline.py
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

This skill is local-only: never fall back to any web API or upload patient data.

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

### Multi-sheet Excel (raw hospital exports)

`--inspect` also prints every sheet with its score (how many standard columns it maps),
detected header row, row count, and which sheet would be used. Raw files often have a cover
sheet, title rows above the header, hidden backup sheets, or data split across sheets.

- If the run stops with **`ERROR (來源檔案)`** (exit code 2), the sheet choice is ambiguous —
  e.g. a clinical sheet + an NGS/gene sheet, or two batches with the same layout. **Do not
  guess.** Show the user the sheet list from the message and ask (`AskUserQuestion`):
  merge by ID (`--sheet 臨床,NGS`), stack batches (`--sheet all` / `--sheet A,B`
  with `--sheet-join stack`), or use a single sheet (`--sheet NAME`).
- After a run, mention any `[SOURCE WARNING]` lines: unused sheets that look like data,
  header not on row 1, a whole required column absent (e.g. no HB column), duplicate IDs,
  patients missing from one of the merged sheets, merge conflicts.
- `Row` in the field audit is `sheet!excel_row`, so the user can find the cell in the
  original file. `.xls` must be re-saved as `.xlsx`.

## Step 1.6 — Ask how to handle missing values (always ask)

Real cohorts have gaps. Before running, ask the user (use `AskUserQuestion`) which
policy to apply, unless they already stated it:

- **`auto` (recommended)** — trial-calculate first. Patients whose missing values move
  the risk by at most `--max-range` (default 1.0, the model's own threshold) are scored
  with the **Mean scenario (population-mean fill)**; patients above it go to an
  **UNCERTAIN list and are excluded** (their Best/Mean/Worst interval is kept for reference).
- **`impute`** — keep everyone; the official R package runs Best / Mean / Worst and
  `Reported_Score` is the Mean scenario. Wide ranges are only flagged `UNCERTAIN`.
- **`skip`** — skip every patient with any missing *model input* field; only complete
  patients are scored.

Semantics to state when relevant:
- Blank, `NA`, `N/A`, `None`, `.` … are all **unknown**.
- **`ND` = Not Detected = tested, no mutation → treated as `0`** (for gene / `del5q` /
  `TP53mut` etc.). `ND` in a numeric field or `CYTO_IPSSR` has no value → missing.
- Unknown is **not** `0`. Do not let users fill unknown gene cells with `0`, and do not
  pre-fill means by hand — both hide uncertainty (a hand-filled mean looks complete and
  gets marked CONFIDENT).
- Which fields hurt most (single missing field alone pushes Range > 1 → UNCERTAIN):
  `HB` (3.95), `BM_BLAST` (2.03), `TP53` (1.70), `CYTO_IPSSR` (1.66), `FLT3`/`MLL_PTD`
  (1.15). `PLT` (0.80) and individual genes (0.29–0.62) are individually below 1 but add up.

## Step 2 — Run stages 1–3 (normalize → trial → standardized data)

```bash
python3 ipssm_pipeline.py <input.csv|xlsx> --missing auto        # or impute / skip
```

This **stops before the formal calculation** and writes next to the input:
- `<stem>_standardized.csv` — the 42 standard columns for the patients that will be
  calculated (editable).
- `<stem>_review.xlsx` — `Summary`, `Source`, `Standardized`, `Field_Audit`, `Column_Summary`,
  `Missing_Check`, `Consistency`, `Uncertain`, `Excluded` (+ `LLM_Review` if LLM was used).
- `<stem>_field_audit.csv` — every normalized cell: original → new value, rule, source
  (`rule` / `llm`), status (`auto_applied` / `needs_review` / `unresolved`), confidence.
- `<stem>_screening_log.txt`.

Flags: `--sheet NAME|N|A,B|all` and `--sheet-join stack|merge` (Excel sheets), `--max-range N` (auto threshold), `--screen-only` (stage 1 only), `--rscript PATH`,
`-v validation.xlsx`.

### Optional: LLM help for cells the rules cannot parse

If the log shows `unresolved` cells (text like "weakly positive" the keyword rules can't
map) offer the user an LLM-assisted pass — **only if they want it**, and ask where it runs:

- **Local** (data stays on the machine): any OpenAI-compatible endpoint, e.g.
  `--llm --llm-endpoint http://localhost:11434/v1 --llm-model <model>`.
- **Cloud**: needs `--llm-allow-remote`. Cell text leaves the machine, so first confirm the
  data is de-identified and permitted by the IPSS-M terms; the API key must be in an
  environment variable (`--llm-api-key-env NAME`), never on the command line.

The LLM only interprets existing text (never fills missing values), receives just the field
name and one cell's text (no ID, no other fields), and reports a confidence; below
`--llm-min-confidence` (default 0.8) it is ignored. Every LLM-applied cell is
`needs_review` — tell the user to check `LLM_Review`. Quality has not been evaluated for
any particular model.

## Step 2.5 — Present the review and get confirmation (always do this)

Do **not** run the formal calculation straight away. Read the console summary / log and
tell the user, in a short list:

1. **Normalization**: how many cells were changed, and especially the `needs_review` and
   `unresolved` ones (quote a few from `Field_Audit`, e.g. `HB '96 g/L' → 9.6`,
   `TP53maxvaf '85' → 0.85`). Patients rejected for unparseable values and why.
2. **Missing values**: how many patients have missing fields, by group (clinical /
   cytogenetic / tp53 / genes), and the **UNCERTAIN list** (ID, missing fields,
   Best/Mean/Worst, Range) — `auto` excludes them, `impute` keeps but flags them.
3. **Consistency conflicts** (`Consistency` sheet, `CONSISTENCY CHECKS` in the log): list the
   `conflict` rows (e.g. `TP53mut=0` but `TP53maxvaf=0.8`; `complex=1` but `CYTO_IPSSR=Good`)
   and the `note` rows that change the result (**C04: the model overrides `TP53loh=0` to 1**).
   These are warnings only — nothing was excluded or changed. Ask the physician which value
   is right; corrections go into `<stem>_standardized.csv` (flags are recomputed at the
   formal calculation, so they update). Do not "fix" these yourself.
4. **Standardized data**: how many patients will be calculated, and where
   `<stem>_standardized.csv` / `<stem>_review.xlsx` are. Offer `SendUserFile` for the review
   workbook.

Then ask (`AskUserQuestion`) whether to proceed, fix something first (the user may edit the
standardized CSV or the source file — if the source changed, re-run Step 2), or change the
policy. Only continue after an explicit yes.

## Step 3 — Formal calculation (after confirmation)

```bash
python3 ipssm_pipeline.py <stem>_standardized.csv --calc-only
```
(`--confirm` on Step 2 runs everything in one go — use it only if the user explicitly
said they don't want a review step.)

Output `<stem>_standardized_results.xlsx` — **the deliverable**:
  - `Summary` — `ID`, `Reported_Score`, `Reported_Cat`, `Confidence_Level`,
    `Missing_Fields`, `Consistency_Flags` (UNCERTAIN rows highlighted red).
  - `R_Full_Output` — full R output per patient, incl. `IPSSMscore_best/mean/worst`,
    `Range_Score`, `Used_Scenario_Analysis`, `Missing_Fields`, `Missing_Groups`,
    `Key_Missing_Fields`.
  - `Analysis` — only when `-v` was supplied.

## Step 4 — Report results

1. Tell the user how many patients were calculated, how many were excluded (and the split:
   data errors / missing-value skips / auto-UNCERTAIN), and **CONFIDENT vs UNCERTAIN** counts.
2. Explain confidence: `Range = IPSSMscore_worst − IPSSMscore_best`; `Range ≤ 1` →
   **CONFIDENT**, `> 1` → **UNCERTAIN** (missing key data spans multiple risk categories —
   interpret `Reported_Score` with care and look at best/worst). CONFIDENT does not mean
   "no missing data" — check `Missing_Fields`. `Reported_Score` is the **Mean scenario**
   (population-mean fill) for patients with missing values.
3. Hand the results Excel to the user with `SendUserFile` (`<stem>_standardized_results.xlsx`).

## Data & compliance (state briefly to the user)

Per the MSKCC IPSS-M terms mirrored in `README.md`: data must be **de-identified**
(no PHI), results are **for academic research only** — not for clinical diagnosis,
treatment, or medical reports. Nothing is transmitted anywhere; the R engine runs fully locally.

## Reference

Field specs, NA handling, karyotype parsing rules, and cohort auto-detection are
documented in `SCREENER_REFERENCE.md` at the repo root — consult it when a patient is
skipped or a value looks wrong.
