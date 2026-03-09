# IPSSM Pipeline — MDS 風險評分批次處理工具

一鍵完成 IPSS-M（Revised International Scoring System for MDS）批次計算，包含資料驗證、R 風險計算、信心等級標記。

## 快速開始

```bash
# 一鍵全流程（驗證 → R 計算 → Excel 結果）
python ipssm_pipeline.py IPSSM_FJHUcohort.xlsx

# 分步驟
python ipssm_pipeline.py data.xlsx --screen-only      # 僅驗證
python ipssm_pipeline.py cleaned.csv --translate-only  # 僅 R 計算

# 帶驗證比對
python ipssm_pipeline.py data.xlsx -v validation.xlsx
```

## 流程說明

```
輸入 (Excel/CSV)
    │
    ▼
[階段 1] 資料驗證 & 格式轉換
    ├─ 自動偵測 FJUH 格式（欄名空格修復）
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

> 詳見 [MISSING_DATA_HANDLING.md](MISSING_DATA_HANDLING.md) 了解三情景計算與信心判定的完整數學基礎。

## 環境需求

- **Python** ≥ 3.8（需安裝 `pandas`, `openpyxl`）
- **R** ≥ 4.3.0（需安裝 `ipssm` 套件）

## 檔案結構

```
├── ipssm_pipeline.py              # 唯一核心腳本
├── README.md                       # 本文件
├── MISSING_DATA_HANDLING.md        # 缺失數據處理說明
│
├── 1.IPSSMexample.csv              # 官方範例數據 (3 patients)
├── IPSSM_FJHUcohort.xlsx           # FJUH 隊列原始數據
├── IPSSM_validation_result.xlsx    # 手動驗證結果
│
└── [執行後自動產生]
    ├── *_cleaned.csv               # 清理後 CSV
    ├── *_r_output.csv              # R 計算中間輸出
    ├── *_results.xlsx              # 最終 Excel 結果
    └── *_screening_log.txt         # 驗證日誌
```

## FJUH 隊列結果

| 項目 | 數值 |
|------|------|
| 輸入患者 | 356 |
| 通過驗證 | 355（1 人因缺少 PLT 被跳過） |
| CONFIDENT | 248 (69.9%) |
| UNCERTAIN | 107 (30.1%) |

## 故障排除

| 問題 | 解決 |
|------|------|
| "Missing required field" | 患者缺少 HB/PLT/BM_BLAST，檢查原始數據 |
| "R execution failed" | 確認 R ≥ 4.3.0 且已安裝 `ipssm` 套件 |
| 大量 UNCERTAIN 結果 | 正常（缺少細胞遺傳學數據），詳見 MISSING_DATA_HANDLING.md |

---

**最後更新**: 2026-03-10  
**參考文獻**: Greenberg et al., Blood (2022) — IPSS-M: Revised International Scoring System for MDS
