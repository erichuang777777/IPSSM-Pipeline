# IPSSM Pipeline — MDS 風險評分批次處理工具

一鍵完成 IPSS-M（Revised International Scoring System for MDS）批次計算，包含資料驗證、R 風險計算、信心等級標記。

## 快速開始

```bash
# 一鍵全流程（驗證 → R 計算 → Excel 結果）
python ipssm_pipeline.py data.xlsx

# 分步驟
python ipssm_pipeline.py data.xlsx --screen-only      # 僅驗證
python ipssm_pipeline.py cleaned.csv --translate-only  # 僅 R 計算

# 帶驗證比對
python ipssm_pipeline.py data.xlsx -v validation.xlsx

# 來源檔案欄名不確定能否自動對應時，先唯讀掃描預覽(不寫檔、不計算)
python ipssm_pipeline.py data.xlsx --inspect
```

## 作為本地 Agent Skill 使用（Claude Code）

本專案內附一個 Claude Code agent skill（`.claude/skills/ipssm/`），讓你不必開網頁、
直接在本機用自然語言請 Claude 對一份病患 CSV/Excel 批次計算 IPSS-M。

**用法**：在本專案目錄開 Claude Code，然後說例如「幫我算這個 `cohort.xlsx` 的 IPSS-M」。
Claude 會自動載入 skill，先檢查依賴（Python 套件 / R / `ipssm` 套件，缺的話引導安裝），
再執行 pipeline，最後把結果 Excel 交給你並摘要 CONFIDENT / UNCERTAIN 筆數。

skill 也附了一個從任意目錄都能執行的包裝腳本：

```bash
bash .claude/skills/ipssm/scripts/run_ipssm.sh <input.csv|xlsx> [--screen-only ...]
```

> 引擎：只有**本地 R 引擎**（離線、支援缺失值的 Best/Mean/Worst 情境分析）。
> 已不再提供 Streamlit 網頁版與官方 REST API 引擎。

## 流程說明

```
輸入 (Excel/CSV)
    │
    ▼
[階段 1] 資料驗證 & 格式轉換
    ├─ 自動偵測隊列格式 (FJUH / HSCT / 自訂)
    ├─ 自動解析核型字符串 (Karyotype → 細胞遺傳學欄位)
    ├─ 必填欄位檢查 (HB, PLT, BM_BLAST)
    ├─ NA 標準化 & 數值範圍驗證
    ├─ 產出: *_cleaned.csv + *_screening_log.txt
    │
    ▼
[階段 2] R 風險計算 & 信心標記
    ├─ 呼叫 R IPSSMwrapper 計算三種情景分數
    ├─ Range = Worst - Best → 判斷信心等級
    ├─ 產出: *_results.xlsx (多工作表)
    │
    ▼
最終輸出 Excel
    ├─ Summary:       ID + Confidence_Level
    ├─ R_Full_Output:  完整 R 計算數據 (65 欄)
    └─ Analysis:       與手動驗證結果比對
```

## 信心等級邏輯

| 條件 | 標記 | 意義 |
|------|------|------|
| Range < 1 | **CONFIDENT** ✅ | 缺失數據影響小，Mean 分數可信 |
| Range ≥ 1 | **UNCERTAIN** ⚠️ | 缺失關鍵數據，結果跨多個風險類別 |

> 詳見 [SCREENER_REFERENCE.md](SCREENER_REFERENCE.md) 了解完整欄位規格、函數說明、與核型解析規則。

## 驗證行為 (v2)

- **逐列獨立驗證**：某一列有錯誤時，只會將「該列」標記為 SKIP 並記錄原因，
  **不會**影響其他正常病患，也不會讓整批作廢。只有在 0 筆有效資料時才回報失敗。
- **核型欄位保留**：cohort 自動轉換時，由核型解析出的 `complex` / `del17_17p`
  等欄位會完整帶入輸出，不再被丟棄。
- **欄位自動對應不再限定隊列類型**：只要欄名(或其正規化後的形式，例如去除單位括號、
  底線)能對到 `COLUMN_ALIASES`，無論是否偵測到已知的 FJUH/HSCT 特徵欄都會嘗試轉換。
  來源檔案欄名不確定時，建議先跑 `--inspect` 預覽對應結果，見上方「快速開始」。

## 計算引擎

只使用本地 R 引擎（官方 `ipssm` 套件的 `IPSSMwrapper`），完全離線，病患資料不會離開本機。
缺失值（空白、`NA`、`ND` 等）一律標準化為 `NA`，由 R 套件做 Best / Mean / Worst 三情境分析。

## 測試

```bash
pip install -r requirements.txt pytest
pytest -q          # 純 Python 測試，不需 R 或網路
```

## 環境需求

- **Python** ≥ 3.8（需安裝 `pandas`, `openpyxl`；測試另需 `pytest`）
- **R** ≥ 4.3.0（需安裝 `ipssm` 套件，可執行 `Rscript install.R` 安裝）
- Rscript 路徑可用環境變數 `IPSSM_RSCRIPT` 覆寫（跨平台自動偵測）

## 專案結構

```
├── ipssm_pipeline.py          # 核心腳本 (含 Screener + Cohort Converter + Karyotype Parser + R Translator)
├── .claude/skills/ipssm/      # 本地 Claude Code agent skill (SKILL.md + run_ipssm.sh)
├── tests/                     # pytest 測試 (karyotype / validation / cohort)
├── SCREENER_REFERENCE.md      # Screener 完整參考手冊（欄位規格、函數說明）
├── README.md                  # 本文件
│
├── install.R                  # 本地安裝 ipssm R 套件
├── requirements.txt           # Python 依賴
├── .gitignore                 # Git 忽略規則
│
├── 1.IPSSMexample.csv         # 官方範例數據 (3 patients)
└── backup/                    # 原始測試數據備份
    ├── IPSSM_FJHUcohort.xlsx
    ├── HSCT cohort.xlsx
    └── IPSSM_validation_result.xlsx
```

## 故障排除

| 問題 | 解決 |
|------|------|
| "Missing required field" | 患者缺少 HB/PLT/BM_BLAST，檢查原始數據 |
| "R execution failed" | 確認 R ≥ 4.3.0 且已安裝 `ipssm` 套件 |
| 大量 UNCERTAIN 結果 | 正常（缺少細胞遺傳學數據），詳見 SCREENER_REFERENCE.md |
| "Invalid category: CYTO_IPSSR" | CYTO_IPSSR 必須為 Very Good/Good/Intermediate/Poor/Very Poor |

## 法律與合規注意事項 (Terms of Use / Compliance)

開發與使用本系統需嚴格遵守 MSKCC (Memorial Sloan Kettering Cancer Center) 官方 IPSS-M 使用條款。請注意以下限制：

1. **🚫 絕對禁止用於臨床醫療與診斷**：
   本工具算出的分數 **僅限學術研究 (Academic Research) 參考使用**。嚴禁將計算結果用於真實病患之診斷、治療決策，或產生任何醫院的官方醫療報告。
2. **🚫 禁止傳輸病患個資 (PHI/PII)**：
   不得上傳含有「真實姓名」、「身分證字號」等足資辨識個人身分的Protected Health Information (PHI)。本工具完全在本機執行、不傳送資料，但輸入檔請確保已去識別化。
3. **🚫 禁止商業用途**：
   嚴格禁止將此工具或產生之結果用於任何商業目的，除非已取得 MSK 之明示書面授權。
4. **⚠️ 發表論文限制**：
   若使用此批次計算工具產出學術論文或期刊報告，事前應先發信至 `papaemme@mskcc.org` 尋求授權許可並妥善引用原作者權益。

---

**開發者**: erichuang777777 | ⚙️ Powered by: [MSKCC IPSS-M Engine](https://mds-risk-model.com/)  
**最後更新**: 2026-03-10  
**參考文獻**: Bernard et al., NEJM Evid (2022) — IPSS-M
