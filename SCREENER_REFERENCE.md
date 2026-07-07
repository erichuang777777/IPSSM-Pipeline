# IPSSM Screener 參考手冊

> 本文件詳細記錄 `ipssm_pipeline.py` 中 **Screener（資料驗證器）** 的每個欄位規格、缺失值處理邏輯、以及各 function 的用途。  
> 使用者可根據本手冊追溯任何數值錯誤或被跳過的資料列原因。

---

## 1. 標準 IPSSM 欄位規格 (42 欄)

### 1.1 必填欄位 (Required Fields)

缺少以下欄位的資料列會被 **直接跳過 (SKIP)**，不會進入 R 計算。

| 欄位 | 全名 | 類型 | 有效範圍 | 單位 | 缺失處理 |
|------|------|------|----------|------|----------|
| `HB` | Hemoglobin | float | 4 ~ 20 | g/dL | ❌ 不可缺失 → 跳過該筆 |
| `PLT` | Platelet count | float | 0 ~ 2000 | ×10⁹/L | ❌ 不可缺失 → 跳過該筆 |
| `BM_BLAST` | Bone marrow blast % | float | 0 ~ 30 | % | ❌ 不可缺失 → 跳過該筆 |

> ⚠️ **HB 單位注意**: IPSS-M 標準為 **g/dL**。若您的資料使用 g/L，需先除以 10 再輸入。

### 1.2 細胞遺傳學欄位 (Cytogenetic Fields)

| 欄位 | 全名 | 類型 | 有效值 | 缺失處理 |
|------|------|------|--------|----------|
| `del5q` | Deletion 5q | binary | 0 / 1 | → `NA`，R 啟動**情境分析** |
| `del7_7q` | -7 or del(7q) | binary | 0 / 1 | → `NA`，R 啟動情境分析 |
| `del17_17p` | -17 or del(17p) | binary | 0 / 1 | → `NA`，R 啟動情境分析 |
| `complex` | Complex karyotype (≥3 abnormalities) | binary | 0 / 1 | → `NA`，R 啟動情境分析 |
| `CYTO_IPSSR` | Cytogenetic risk category | category | Very Good / Good / Intermediate / Poor / Very Poor | → `NA`，R 啟動情境分析 |

> 當上述任一欄位缺失 (NA) 時，R 的 `IPSSMwrapper` 會自動啟動**三情境分析** (Best/Mean/Worst)。

### 1.3 TP53 相關欄位

| 欄位 | 全名 | 類型 | 有效值 | 缺失處理 |
|------|------|------|--------|----------|
| `TP53mut` | TP53 mutation count | int | 0 / 1 / 2 | → `NA`，R 自動推算 |
| `TP53maxvaf` | TP53 maximum VAF | float | 0 ~ 1 | → `NA`，R 自動推算 |
| `TP53loh` | TP53 loss of heterozygosity | binary | 0 / 1 | → `NA`，R 自動推算 |

### 1.4 基因突變欄位 (31 個二元欄位)

以下欄位均為二元 (binary) 格式，有效值為 `0` (未突變) 或 `1` (有突變)。  
**缺失統一設為 `NA`**，R 會將其視為「未檢測」。

| 欄位 | 欄位 | 欄位 | 欄位 |
|------|------|------|------|
| `MLL_PTD` | `FLT3` | `ASXL1` | `BCOR` |
| `BCORL1` | `CBL` | `CEBPA` | `DNMT3A` |
| `ETV6` | `EZH2` | `IDH1` | `IDH2` |
| `KRAS` | `NF1` | `NPM1` | `NRAS` |
| `RUNX1` | `SETBP1` | `SF3B1` | `SRSF2` |
| `STAG2` | `U2AF1` | `ETNK1` | `GATA2` |
| `GNB1` | `PHF6` | `PPM1D` | `PRPF8` |
| `PTPN11` | `WT1` | | |

### 1.5 其他欄位

| 欄位 | 說明 | 缺失處理 |
|------|------|----------|
| `ID` | 病患編號 | 若無則自動以 `Row{N}` 代替 |

---

## 2. NA 標準化規則

Screener 會將以下字串**自動統一轉換為標準的 `NA`**：

```
"", " ", "NA", "N/A", "n/a", "na",
"NaN", "nan", "None", "none", ".", "ND", "nd"
```

### 2.1 特殊規則: `CYTO_IPSSR` 的 `ND`
- `ND` (Not Detected/Not Done) 在 `CYTO_IPSSR` 欄位有特殊意義
- 被明確轉換為 `NA`，觸發 R 情境分析
- 會在驗證日誌中記錄為 `"Converted ND (not detected) to NA"`

---

## 3. 信心等級判定

使用 R IPSSMwrapper 計算後，根據三情境得分的**範圍 (Range)** 判定信心等級。

| 公式 | 結果 | 意義 |
|------|------|------|
| `Range = Worst - Best` | — | 最悲觀與最樂觀情境之間的差距 |
| `Range < 1` | ✅ **CONFIDENT** | 缺失數據影響小，Mean 分數可信 |
| `Range ≥ 1` | ⚠️ **UNCERTAIN** | 缺失關鍵數據，結果可能跨多個風險類別 |

---

## 4. 核型解析器 (Karyotype Parser)

當偵測輸入資料含 `karyotype` 欄位時，Screener 會自動呼叫核型解析器，  
將核型字符串轉換為 IPSS-M 所需的 5 個細胞遺傳學欄位。

### 4.1 解析範例

| 輸入核型 | del5q | del7_7q | del17p | complex | CYTO_IPSSR |
|----------|-------|---------|--------|---------|------------|
| `46,XY` | 0 | 0 | 0 | 0 | Good |
| `46,XY,del(5q)` | 1 | 0 | 0 | 0 | Good |
| `46,XY,-7` | 0 | 1 | 0 | 0 | Poor |
| `45,XY,-Y` | 0 | 0 | 0 | 0 | Very Good |
| `47,XY,+8,del(5q),del(7q)` | 1 | 1 | 0 | 1 | Poor |
| `NM` | 0 | 0 | 0 | 0 | NA |

### 4.2 CYTO_IPSSR 分類規則 (依 IPSS-R)

| 類別 | 條件 |
|------|------|
| **Very Good** | -Y, del(11q) |
| **Good** | Normal (46,XX/46,XY), del(5q), del(12p), del(20q) |
| **Intermediate** | del(7q), +8, +19, i(17q), 其他單/雙獨立異常 |
| **Poor** | -7, inv(3)/i(3q), 複雜(3個異常) |
| **Very Poor** | 複雜(>3個異常) |

---

## 5. 隊列格式自動偵測 (Cohort Converter)

Screener 能自動偵測不同醫院的資料格式，並轉換為標準 IPSSM 欄位。

### 5.1 偵測邏輯
- **FJUH**: 含 `ethnicity`, `diagnosis`, `karyotype` 欄位
- **HSCT**: 含 `transplant`, `graft`, `donor`, `conditioning` 欄位
- **UNKNOWN**: 沒有上述特徵欄 — 僅為報告用的描述性標籤，**仍會嘗試**標準欄位對應
  (v2 起不再限定只有偵測到 FJUH/HSCT 才轉換；任何檔案只要有可用的別名對應就會轉換)

### 5.2 欄位別名對照 (部分)

| 標準欄位 | 可接受的別名 |
|----------|-------------|
| `ID` | `Patient_ID`, `PatientID`, `PID`, `Chart No.` |
| `HB` | `Hemoglobin`, `Hb` |
| `PLT` | `Platelet`, `platelets`, `platelet_count` |
| `BM_BLAST` | `Blast`, `BM Blast`, `Blast_BM` |
| `CYTO_IPSSR` | `CYTO_IPSS-R`, `CYTO IPSS-R`, `Cytogenetic IPSS-R` |

> 完整對照表請參見 `ipssm_pipeline.py` 中的 `COLUMN_ALIASES` 字典。

### 5.3 正規化比對規則 (v2)

比對欄名時會先正規化 (`_normalize_header`)：轉大寫、去除空白/底線/連字號/句點/斜線，
並在括號內容「整段看起來像檢驗單位」時移除該括號內容。例如：

| 來源欄名 | 正規化後 | 對應到 |
|----------|----------|--------|
| `Hemoglobin (g/dL)` | `HEMOGLOBIN` | `HB` |
| `BM Blast (%)` | `BMBLAST` | `BM_BLAST` |
| `Platelet_count` | `PLATELETCOUNT` | `PLT` |
| `del(5q)` | `DEL5Q` | `del5q` (括號內容 `5q` 不是單位，會被保留) |

僅做規則式的正規化比對，**不做模糊/相似度比對**，避免誤判。

### 5.4 `--inspect` 唯讀欄位掃描預覽

不確定來源檔案欄名能否自動對應時，可先執行唯讀預覽(不寫任何輸出檔、不驗證、不計算)：

```bash
python ipssm_pipeline.py data.xlsx --inspect
```

會回報：
- **確定匹配**的欄位對應
- **必填欄位缺失**(`HB`/`PLT`/`BM_BLAST` 未匹配 → 該病患會被跳過)
- **選填欄位缺失**(其餘標準欄位未匹配 → 安全填為 `NA`)
- 來源檔案中**未對應到任何標準欄位**的欄名(可能是無關的中繼資料，也可能是別名表未收錄的欄位)
- `HB` 的**單位健檢**提示(數值中位數明顯偏高時，提示可能是 g/L 而非 g/dL；僅提示不自動轉換)

---

## 6. 函數參考 (Function Reference)

### 6.1 Screener 核心函數

| 函數 | 參數 | 回傳 | 說明 |
|------|------|------|------|
| `run_screening(input_path, output_path, log_path)` | 輸入檔路徑, 輸出CSV路徑, 日誌路徑 | `bool` | **主入口**：完整執行資料驗證流程 |
| `_try_convert_cohort(input_path, report)` | 輸入檔路徑, ValidationReport | `(rows, fieldnames, converted)` | 嘗試自動偵測並轉換隊列格式 |
| `_convert_fjuh_format(rows, fieldnames, report)` | 資料列, 欄位名, ValidationReport | `rows` | 修復欄名尾部空格 |
| `_validate_row(row_idx, row, report)` | 列索引, 資料dict, ValidationReport | `bool` | 驗證單筆資料：NA標準化→必填→範圍→二元→分類 |
| `_read_input_file(input_path)` | 檔案路徑 | `(rows, fieldnames)` | 讀取 CSV 或 Excel |

### 6.2 核型解析函數

| 函數 | 參數 | 回傳 | 說明 |
|------|------|------|------|
| `parse_karyotype(karyotype_str)` | 核型字符串 | `dict` | 解析為 del5q/del7_7q/del17p/complex/cyto_ipssr |
| `_extract_abnormalities(karyotype)` | 大寫核型字符串 | `dict` | 提取所有染色體異常並計算總數 |
| `_classify_cytogenetics(abn, ...)` | 異常dict, flags | `str` | 分類為 IPSS-R 細胞遺傳學風險類別 |

### 6.3 隊列轉換函數

| 函數 | 參數 | 回傳 | 說明 |
|------|------|------|------|
| `detect_cohort_type(df)` | DataFrame | `str` | 偵測隊列來源 (FJUH/HSCT/UNKNOWN，僅供參考標籤) |
| `find_column_mapping(input_df)` | DataFrame | `dict` | 根據別名表(正規化比對)對應欄位名稱 |
| `_normalize_header(name)` | 欄名字串 | `str` | 正規化欄名以供比對(去單位括號/底線/空白/大小寫) |
| `inspect_columns(input_path)` | 檔案路徑 | `dict` | 唯讀掃描，回報 matched/missing/unmapped 欄位分類 |
| `print_inspect_report(result)` | `inspect_columns()` 的回傳值 | — | 將掃描結果印成人類可讀報告 |

### 6.4 R 計算函數

| 函數 | 參數 | 回傳 | 說明 |
|------|------|------|------|
| `run_translation(input_csv, rscript, validation)` | CSV路徑, Rscript路徑, 驗證檔 | `bool` | **主入口**：執行 R 並整合結果 |
| `_find_rscript()` | — | `str or None` | 自動尋找 Rscript 安裝路徑 |
| `_save_excel(r_results, output_path, validation)` | 結果list, 輸出路徑, 驗證dict | — | 儲存多工作表 Excel |

### 6.5 驗證報告類別

| 類別/方法 | 說明 |
|----------|------|
| `ValidationReport` | 收集所有驗證過程的記錄 |
| `.add_error(row, col, msg)` | 記錄驗證錯誤 |
| `.add_warning(row, col, msg)` | 記錄警告 |
| `.add_fix(row, col, old, new, reason)` | 記錄自動修復 |
| `.skip_patient(id, reason)` | 記錄被跳過的病患 |
| `.report()` | 產生完整的文字報告 |

---

## 7. 驗證流程圖

```
輸入 (Excel/CSV)
    │
    ▼
[1] _try_convert_cohort()
    ├─ detect_cohort_type() → FJUH/HSCT/UNKNOWN
    ├─ find_column_mapping() → 欄位名稱對應
    ├─ parse_karyotype()    → 核型 → 細胞遺傳學欄位
    └─ 補齊缺失欄位為 NA
    │
    ▼
[2] _convert_fjuh_format()
    └─ 修復欄名尾部空格
    │
    ▼
[3] _validate_row() (逐列，錯誤僅影響該列)
    ├─ NA 標準化 (ND → NA)
    ├─ 必填欄位檢查 → 缺失則 SKIP
    ├─ 數值範圍檢查 → 超出則 ERROR → 該列 SKIP
    ├─ 二元欄位檢查 → 非 0/1 則 ERROR → 該列 SKIP
    └─ 分類欄位檢查 → 無效值則 ERROR → 該列 SKIP
    (註: 有錯誤的列只跳過自己並記錄原因，不影響其他病患，也不讓整批失敗)
    │
    ▼
[4] 輸出 cleaned.csv + screening_log.txt
    │
    ▼
[5] run_translation() → R IPSSMwrapper
    ├─ 三情境分析 (Best/Mean/Worst)
    ├─ Range = Worst - Best
    ├─ Confidence = CONFIDENT / UNCERTAIN
    └─ 輸出 results.xlsx (多工作表)
```

---

**最後更新**: 2026-03-10  
**參考文獻**: Bernard et al., NEJM Evid (2022) — IPSS-M  
