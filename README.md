# IPSSM Pipeline — MDS 風險評分批次處理工具

一鍵完成 IPSS-M（Revised International Scoring System for MDS）批次計算，包含資料驗證、R 風險計算、信心等級標記。

## 快速開始

流程分四個階段，**預設在階段 3 停下來讓你核對標準欄位資料**，確認後才正式計算：

```bash
# 階段 1~3：正規化(含逐欄位紀錄) → 試算缺失值風險 → 產生標準欄位資料與核對檔
python ipssm_pipeline.py data.xlsx --missing auto

# 核對 data_review.xlsx 與 data_standardized.csv (必要時直接編輯 CSV)，確認後：
# 階段 4：正式計算風險
python ipssm_pipeline.py data_standardized.csv --calc-only

# 不需要核對、想一次跑完 (階段 1~4)
python ipssm_pipeline.py data.xlsx --missing auto --confirm

# 其他
python ipssm_pipeline.py data.xlsx --screen-only      # 只做階段 1 (正規化 + 驗證)
python ipssm_pipeline.py data.xlsx -v validation.xlsx # 帶驗證比對
python ipssm_pipeline.py data.xlsx --inspect          # 唯讀預覽分頁與欄位對應 (不寫檔、不計算)
python ipssm_pipeline.py data.xlsx --sheet 臨床,NGS    # 指定/合併 Excel 分頁 (見「讀取原始 Excel」)
```

## 作為本地 Agent Skill 使用（Claude Code）

本專案內附一個 Claude Code agent skill（`.claude/skills/ipssm/`），讓你不必開網頁、
直接在本機用自然語言請 Claude 對一份病患 CSV/Excel 批次計算 IPSS-M。

**用法**：在本專案目錄開 Claude Code，然後說例如「幫我算這個 `cohort.xlsx` 的 IPSS-M」。
Claude 會自動載入 skill，先檢查依賴（Python 套件 / R / `ipssm` 套件，缺的話引導安裝），
再執行 pipeline，最後把結果 Excel 交給你並摘要 CONFIDENT / UNCERTAIN 筆數。

skill 也附了一個從任意目錄都能執行的包裝腳本：

```bash
bash .claude/skills/ipssm/scripts/run_ipssm.sh <input.csv|xlsx> [--missing auto] [--confirm ...]
```

> 引擎：只有**本地 R 引擎**（離線、支援缺失值的 Best/Mean/Worst 情境分析）。
> 已不再提供 Streamlit 網頁版與官方 REST API 引擎。

## 網頁介面（給不使用終端機的使用者）

不想打指令的人，可以用瀏覽器完成「上傳 → 設定 → 整理與檢查 → 核對/修改 → 確認 → 計算 → 下載」。
所有計算與檢查仍由同一套程式完成，網頁只是外殼。

**啟動**（只需準備一次：安裝 [Python 3.8+](https://www.python.org/downloads/)（Windows 安裝時勾選 *Add python.exe to PATH*），以及 R 與 ipssm 套件，網頁上會有安裝指引）：

| 系統 | 做法 |
|---|---|
| Windows | 雙擊 `start_web.bat` |
| macOS | 雙擊 `start_web.command` |
| Linux | 執行 `./start_web.sh` |
| 任何系統 | `python3 ipssm_web.py` |

第一次執行會自動安裝 `pandas`、`openpyxl`（需要網路）。程式會自動開啟瀏覽器；若沒有開，請複製視窗裡顯示的網址。
使用完畢請按網頁右上角「**結束程式**」。

**畫面流程**
0. **環境檢查與條款確認**（首頁）：自動檢查 Python、pandas / openpyxl、R、官方 ipssm 套件是否可用；缺 ipssm 時可按
   「自動安裝」（需要網路）。上傳前必須勾選「使用條款與合規確認」（伺服器端強制，並記錄在結果檔）。
1. **選擇檔案**：拖放或點選 Excel / CSV。有多個分頁時由你決定要用哪一頁、如何合併。
2. **設定與檢查**：確認欄位對應，選擇缺失值怎麼處理（自動判斷 / 全部補平均 / 只算完整），可選用 LLM 輔助。
3. **核對資料**：待確認清單（資料矛盾、程式替你轉換的格子）、缺失與 UNCERTAIN 的病人與風險區間、被排除的病人、
   **可直接修改的標準欄位資料表**。填入核對者姓名並勾選確認後才會計算。
4. **結果**：分數、風險分類、信心等級、Best–Worst 區間圖；下載結果 Excel（含核對簽核 `Review_SignOff` 與你的
   手動修改紀錄 `Manual_Edits`）、核對檔、標準欄位 CSV、逐欄位紀錄。

**可以放心的地方**
- 只綁定本機 `127.0.0.1`，其他電腦連不到；不載入任何外部資源（沒有 CDN、字型、追蹤）。
- 啟動時產生隨機 token，只有用啟動程式開的網址能用；也防範其他網站偷打本機服務（CSRF、DNS rebinding）。
- 檔案放在工作資料夾（預設 `~/IPSSM_Web_Workspace`，可用 `--workspace` 指定），結果頁可按「清除這次的資料」永久刪除。
- 關閉分頁或重新整理不會丟失進度：回到網址按「繼續上次的工作」。
- LLM 的 API key 只存在記憶體，不會寫入檔案；遠端 LLM 必須勾選同意才會使用。
- 右上角「隱藏病人 ID」：截圖、投影或共用螢幕時，把畫面上的病人 ID 與檔名換成代號（只影響顯示）。

**限制**：一次只處理一個計算工作；供單人在自己的電腦使用，不是多人共用的伺服器（請勿用 `--host` 之類方式對外開放，程式本來也不支援）。
`start_web.bat` 未在 Windows 上實測（`.sh` 與主程式已在 Linux 上實測）。

## 環境檢查與自動安裝（`ipssm_env.py`）

這台電腦能不能執行？缺什麼？怎麼補？網頁介面、Claude skill / agent 與命令列共用同一份檢查：

```bash
python3 ipssm_env.py                  # 人類可讀的檢查報告（全部就緒 exit 0，否則 exit 1）
python3 ipssm_env.py --json           # 機器可讀
python3 ipssm_env.py --install-ipssm  # 自動安裝 R 套件 ipssm（需要網路，首次約數分鐘）
python3 ipssm_env.py --install-ipssm --ipssm-source <ipssm 原始碼資料夾>   # 離線安裝
```

| 檢查項目 | 必要 | 判斷方式 |
|---|---|---|
| Python ≥ 3.8 | ✅ | 版本 |
| pandas ≥ 2.0、openpyxl ≥ 3.1 | ✅ | 版本，且必須能實際載入（與 `requirements.txt` 同步，有測試比對） |
| R | ✅ | 找得到 `Rscript` 且能執行；低於 4.3 只給**警告**（ipssm 套件本身沒有宣告最低 R 版本，4.3 是本專案測試的版本） |
| R 套件 ipssm | ✅ | **用套件內附的範例資料實際算一次**，不是只檢查「套件存在」 |
| 工作資料夾 | ✅ | 可寫入、剩餘空間（檢查本身不會建立任何資料夾） |
| 預設瀏覽器 | 選用 | 找不到只會提示手動開啟網址 |

為什麼用「實際計算」判斷 ipssm：它的相依套件（`car`、`readxl`）在**計算時才會被用到**，載入套件時不會檢查，所以
`library(ipssm)` 成功不代表真的能算。檢查會分辨三種失敗並給不同說明：`尚未安裝`、`已安裝，但缺少相依套件：…`、
`已安裝但無法完成範例計算`。

`install.R` 會選一個**可寫入**的位置安裝（系統資料夾沒有權限時改用使用者自己的 R 資料夾，不需要管理員權限），
並補齊 ipssm 宣告的相依套件。尚無法在所有環境驗證的部分：實際從 GitHub / CRAN 下載（取決於你的網路；
GitHub 對未登入請求有限流，遇到 HTTP 403 稍後再試或改離線安裝）。

## 用 Claude Code 引導使用者（skill 與 agent）

專案內附兩個給 Claude Code 使用的檔案，讓 Claude 帶不熟悉終端機的人用網頁處理**自己電腦上**的檔案：

| 檔案 | 角色 | 做什麼 |
|---|---|---|
| `.claude/skills/ipssm-web/SKILL.md` | **互動式引導**（在主對話中使用） | 先檢查環境、缺什麼就帶使用者補齊（安裝前先徵求同意）、啟動網頁、陪使用者走完四個畫面並解釋每個選項、疑難排解 |
| `.claude/agents/ipssm-web-guide.md` | **被委派的執行與診斷員** | 檢查環境、（經授權）補齊、啟動網頁並回報網址、診斷失敗的工作；以結構化報告交回 |

用法：在專案資料夾開 Claude Code，說「我想用網頁處理我的 Excel」即可（會自動套用 skill）。
兩者分工的原因：Claude Code 的 subagent **無法在執行中向使用者提問**，所以需要和使用者來回確認的部分放在 skill，
agent 只負責「做事與回報」，需要使用者決定的事會列在報告裡由主對話轉達。

**隱私與安全規則**（寫在兩個檔案裡，並有測試防止被誤刪）：不讀取或複述病患資料、只在本機執行且不對外開放、
不替使用者做醫學或資料判斷、不替使用者勾選核對確認、會改動電腦的動作（安裝套件）需明確授權，
agent 刻意**沒有寫入檔案的工具**。這些文件與實際程式的一致性（引用的檔案、旗標、網址擷取方式）也有測試把關。

指令列使用者請改用 `ipssm` skill（`.claude/skills/ipssm/SKILL.md`）。

## 讀取原始 Excel（多分頁）

可以直接使用醫院匯出的原始檔案，不需先手動整理：

| 狀況 | 處理方式 |
|---|---|
| **多個分頁**（封面、說明、資料…）| 依「欄名能對應到幾個標準欄位」評分，自動選出病患資料頁 |
| **資料分散在多頁**（例如「臨床」頁 + 「NGS」頁）| `--sheet 臨床,NGS` 依病患 ID 合併；某頁缺的病患該頁欄位為缺失，並提示 |
| **多批次同格式**（例如 2023、2024 各一頁）| `--sheet all` 或 `--sheet 2023,2024` 上下串接 |
| **無法明確判斷**（兩頁同分、或像是分散在多頁）| **不猜測**，停止並說明各頁差異，請用 `--sheet` 指定 |
| **表頭不在第一列**（上方有標題、日期列）| 掃描前 20 列，自動找到表頭 |
| **隱藏分頁**（舊備份等）| 不參與自動選擇；可用 `--sheet` 明確指定 |
| **百分比格式儲存格**（顯示 9%、實際存 0.09）| 還原為 `9%` 再正規化，blast 不會被讀成 0.09 |
| 公式、`TRUE`/`FALSE`、整數型小數 `1.0` | 讀取計算結果；轉成文字後交給正規化規則 |
| **CSV 編碼** | 自動辨識 UTF-8 與 cp950（Big5，繁中 Excel 另存 CSV 的預設）|
| 舊版 `.xls` | 請另存為 `.xlsx` |

```bash
python ipssm_pipeline.py raw.xlsx --inspect                       # 先看每個分頁的評分與自動選擇結果
python ipssm_pipeline.py raw.xlsx --missing auto --sheet 臨床資料,NGS結果   # 依 ID 合併兩頁
python ipssm_pipeline.py raw.xlsx --missing auto --sheet all --sheet-join stack
```

另外會提示：未被選用但看起來也是資料的分頁、整欄缺失的必填欄位（例如找不到 HB 欄）、重複的病患 ID、
合併時兩頁數值衝突的欄位、沒有 ID 的列（以「分頁!列號」識別）。逐欄位紀錄的 `Row` 會標示
`分頁!Excel 列號`，可直接回原始檔對照；核對活頁簿新增 `Source` 工作表。

## 缺失值處理：`--missing skip | impute | auto`

病患資料常有缺漏。三種處理方式：

| 模式 | 作法 | 適用情境 |
|---|---|---|
| `skip` | 任何**模型輸入欄位**缺失的病患一律跳過，只計算資料完整者 | 要求結果完全建立在實測資料上 |
| `impute`（預設） | 保留 `NA`，由官方 R 套件做 Best / Mean / Worst 三情境分析；**Mean 情境 = 以族群平均值（HB、PLT…）/ 平均突變頻率（基因）補值**；範圍過大者只標 UNCERTAIN，仍會計算 | 想保留所有病患並看出不確定性 |
| `auto` | 先試算；缺失造成的範圍 (Worst − Best) **不超過 `--max-range`（預設 1）** 者補平均值計算，**超過者列入 UNCERTAIN 清單並排除**（區間仍保留供參考）。需要 R | 想自動決定「哪些缺失可以補、哪些不能」 |

### 缺失的定義

| 儲存格內容 | 意義 | 正規化結果 |
|---|---|---|
| 空白、`NA`、`N/A`、`None`、`.` … | **未知**（Not Assessed）| `NA` |
| `ND` / `Not Detected` | **有檢測、未偵測到突變**（Not Detected）| 基因、`del5q` 等二元欄位與 `TP53mut` → **`0`**；數值欄位與 `CYTO_IPSSR` 無法對應成值 → `NA` |
| `0` | 檢測過、沒有突變 | `0` |
| `1` | 突變 / 存在 | `1` |

- 空白與 `NA` **完全等價**，未知 **≠** `0`。基因欄位把未檢測誤填成 `0` 會低估風險。
- **請勿自行手動補平均值再送入**：程式會誤以為資料完整而標成 CONFIDENT；保留空白/`NA` 才能被量化。
- 「模型輸入欄位」= 臨床（`HB`/`PLT`/`BM_BLAST`）、細胞遺傳學（`del5q`/`del7_7q`/`del17_17p`/`complex`/`CYTO_IPSSR`）、
  `TP53mut`，以及 30 個基因欄位。`TP53maxvaf`/`TP53loh` 只有在 `TP53mut = 1` 且無法判定 multi-hit 時才算缺失。

### 哪些欄位缺失最「傷」？（單一欄位缺失造成的 Worst − Best 範圍）

依官方套件公式計算，並由 `tests/test_r_integration.py` 以 R 實測驗證：

| 欄位 | 範圍 | 單獨缺失即 UNCERTAIN? |
|---|---|---|
| `HB` | 3.95 | ✅ |
| `BM_BLAST` | 2.03 | ✅ |
| `TP53`（`TP53mut`，或 `TP53mut=1` 且無法判定 LOH）| 1.70 | ✅ |
| `CYTO_IPSSR` | 1.66 | ✅ |
| `FLT3`、`MLL_PTD` | 1.15 | ✅ |
| `PLT` | 0.80 | 否 |
| `NPM1`/`RUNX1`/`NRAS`/`ETV6`/`IDH2` | 0.55–0.62 | 否 |
| 其餘基因（`CBL`/`EZH2`/`U2AF1`/`SRSF2`/`DNMT3A`/`ASXL1`/`KRAS`…）| 0.29–0.43 | 否 |

單獨不超過 1 的欄位，**多個一起缺失時範圍會相加**，仍可能超過門檻。`Key_Missing_Fields` 欄位會標出
單獨就足以造成 UNCERTAIN 的缺失欄位。

## 流程說明（四階段）

```
輸入 (Excel/CSV)
    │
    ▼
[階段 1] 正規化 & 逐欄位紀錄
    ├─ 讀取原始檔: 自動選分頁 / 多分頁合併 (--sheet)、表頭偵測、百分比格式、CSV 編碼
    ├─ 自動偵測隊列格式、欄位別名對應、核型字串解析
    ├─ 關鍵字/格式規則: Positive→1、neg→0、ND→0、very_good→Very Good、
    │    "9.6 g/dL"→9.6、"96 g/L"→9.6(需確認)、TP53maxvaf "85"→0.85(需確認)…
    ├─ (選用) LLM 輔助: 只處理規則無法解析的儲存格，附信心程度，一律需人工確認
    ├─ 每個被改動/無法解析的儲存格都記錄: 原值 → 新值、規則、來源(rule/llm)、狀態、信心
    ├─ 數值範圍 / 0-1 / 類別驗證 (有問題的病患排除並記錄原因，不影響其他人)
    ├─ 跨欄位一致性檢查 (C01–C09，只警告)
    │
    ▼
[階段 2] 試算缺失值造成的風險 (呼叫 R，Best/Mean/Worst)
    ├─ 每位病患: 缺失欄位、缺失分組、Key_Missing_Fields、Range
    ├─ UNCERTAIN 清單 (Range > 1)
    ├─ auto 模式: Range > --max-range 者排除
    │
    ▼
[階段 3] 產生標準欄位資料，供使用者核對與確認  ⏸ 預設停在這裡
    ├─ *_standardized.csv   42 個標準欄位 (進入正式計算的病患，可直接編輯)
    ├─ *_review.xlsx        Summary / Source / Standardized / Field_Audit / Column_Summary / Consistency /
    │                        Missing_Check / Uncertain / Excluded / LLM_Review
    ├─ *_field_audit.csv    逐欄位正規化紀錄 (機器可讀)
    └─ *_screening_log.txt  驗證日誌
    │
    ▼  使用者確認 (--calc-only 或 --confirm)
[階段 4] 正式計算風險
    └─ *_standardized_results.xlsx
         ├─ Summary:         ID + Reported_Score/Cat + Confidence_Level + Missing_Fields + Consistency_Flags
         ├─ R_Full_Output:   完整 R 計算數據
         ├─ Uncertain_Excluded: auto 模式排除者的 Best/Mean/Worst 區間 (--confirm 時)
         └─ Analysis:        與手動驗證結果比對 (有給 -v 時)
```

輸出欄位：`Reported_Score` / `Reported_Cat` 一律是 **Mean 情境**結果；`IPSSMscore_best` / `IPSSMscore_worst`
是缺失值最有利/最不利時的範圍。

## 跨欄位一致性檢查（只警告）

欄位各自合法、但**彼此矛盾**的資料，會在核對活頁簿的 `Consistency` 工作表、日誌，以及最終結果的
`Consistency_Flags` 欄位標出。**只警告：不排除病患、不修改資料**，由醫師判斷是否為真正的錯誤。
缺失值不會觸發旗標。

| 規則 | 嚴重度 | 內容 |
|---|---|---|
| C01 | conflict | `TP53mut = 0` 但 `TP53maxvaf > 0` |
| C02 | conflict | `TP53mut ≥ 1` 但 `TP53maxvaf = 0` |
| C03 | note | `TP53mut = 0` 但 `TP53loh = 1`（對結果沒有影響）|
| C04 | note | `TP53mut = 1`、`TP53loh = 0`，但 VAF > 0.55 或 `del17_17p = 1`：**模型會把 TP53loh 覆寫為 1**，TP53 判為 multi-hit，分數明顯變高 |
| C05 | conflict | `complex = 1` 但 `CYTO_IPSSR` 是 Very Good / Good / Intermediate |
| C06 | conflict | `CYTO_IPSSR = Very Poor`（複雜核型 >3 個異常）但 `complex = 0` |
| C07 | conflict | `del7_7q = 1` 但 `CYTO_IPSSR` 是 Very Good / Good |
| C08 | conflict | `del5q = 1` 但 `CYTO_IPSSR = Very Good` |
| C09 | note | `BM_BLAST > 20%`：模型以 20% 計算 |

- C01–C04 對應 `ipssm` 套件對 TP53 的處理；C05–C08 依據 IPSS-R 細胞遺傳學風險分組的定義。
  由核型字串自動解析出的細胞遺傳學欄位彼此本來就一致，矛盾通常來自人工填寫。
- **不含「臨床正常值」檢查**（例如 HB 多高不合理），因為沒有權威的 MDS 參考範圍可依據；
  輸入範圍（HB 4–20 等）檢查另有，超出範圍的病患會被排除。
- 正式計算（階段 4）會依「核對後的標準欄位資料」重新計算旗標，所以醫師修正資料後，旗標會跟著更新。

## LLM 輔助檢核（選用）

規則無法解析的儲存格（例如 `"weakly positive"`）可交給 LLM 對應到允許值並給出信心程度。
支援本地或雲端端點：

```bash
# 本地 (Ollama / LM Studio / vLLM / llama.cpp server 等 OpenAI 相容端點)，資料不離開本機
python ipssm_pipeline.py data.xlsx --missing auto --llm \
    --llm-endpoint http://localhost:11434/v1 --llm-model llama3.1

# 雲端 (需明確同意；API key 放環境變數，不要寫在指令列)
export ANTHROPIC_API_KEY=...
python ipssm_pipeline.py data.xlsx --missing auto --llm --llm-provider anthropic --llm-allow-remote
```

- **只解讀、不補值**：LLM 只能把「已存在的文字」對應到允許值；無法解讀必須回 `null`。缺失值不會由 LLM 推測。
- **最小揭露**：只送出「欄位名稱 + 允許值 + 單一儲存格原文」，**不含 ID 或同列其他欄位**；相同文字只查詢一次。
- **遠端需明確同意**：非本地端點必須加 `--llm-allow-remote`。請先確認資料已去識別化並符合使用條款。
- **信心門檻**：`--llm-min-confidence`（預設 0.8）；低於門檻或不在允許值內者不採用，該病患照常因資料錯誤被排除。
- **一律人工確認**：LLM 採用的每個儲存格在 `Field_Audit` / `LLM_Review` 中標為 `needs_review`。
- 目前僅以模擬伺服器測試傳輸與判斷邏輯，**尚未針對特定模型的實際判讀品質做評估**，請務必核對 `LLM_Review`。

## 信心等級邏輯

| 條件 | 標記 | 意義 |
|------|------|------|
| Range ≤ 1 | **CONFIDENT** ✅ | 缺失數據影響小，Mean 分數可信 |
| Range > 1 | **UNCERTAIN** ⚠️ | 缺失關鍵數據，結果跨多個風險類別 |

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
缺失值（空白、`NA` 等）標準化為 `NA`，由 R 套件做 Best / Mean / Worst 三情境分析；`ND` 視為「檢測過、無突變」。

## 測試

```bash
pip install -r requirements.txt pytest
pytest -q          # 含 R 端對端測試；找不到 Rscript / ipssm 時會自動略過那部分，不需網路
```

## 環境需求

- **Python** ≥ 3.8（需安裝 `pandas`, `openpyxl`；測試另需 `pytest`）
- **R**（建議 4.3 以上，本專案以此版本測試；`ipssm` 套件本身沒有宣告最低版本）與官方 `ipssm` 套件（`python3 ipssm_env.py --install-ipssm` 或 `Rscript install.R` 安裝）。可用 `python3 ipssm_env.py` 一次檢查全部條件
- Rscript 路徑可用環境變數 `IPSSM_RSCRIPT` 覆寫（跨平台自動偵測）

## 專案結構

```
├── ipssm_web.py + web/        # 本地網頁介面 (後端 + 前端，無外部依賴)
├── ipssm_env.py               # 環境檢查與 ipssm 自動安裝 (只用標準函式庫)
├── install.R                  # 安裝 R 套件 ipssm (可寫入位置、補齊相依、支援離線來源)
├── .claude/skills/ipssm-web/  # 引導使用者用網頁處理本機檔案 (互動式)
├── .claude/agents/            # ipssm-web-guide: 執行與診斷員
├── start_web.bat / .command / .sh   # 雙擊啟動網頁介面
├── ipssm_pipeline.py          # 核心腳本 (含 Screener + Cohort Converter + Karyotype Parser + R Translator)
├── .claude/skills/ipssm/      # 本地 Claude Code agent skill (SKILL.md + run_ipssm.sh)
├── tests/                     # pytest 測試 (karyotype / validation / cohort)
├── SCREENER_REFERENCE.md      # Screener 完整參考手冊（欄位規格、函數說明）
├── README.md                  # 本文件
│
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
