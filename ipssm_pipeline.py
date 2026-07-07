#!/usr/bin/env python
"""
IPSSM Pipeline - 一鍵完成資料驗證 + R 風險計算 + 結果整合

整合了：
  - Cohort Converter: 自動偵測並轉換不同醫院/研究隊列的欄位格式
  - Karyotype Parser: 自動解析核型字符串為 IPSS-M 細胞遺傳學欄位
  - Screener: 資料驗證、NA 標準化、數值範圍檢查
  - Translator: 呼叫 R IPSSMwrapper 執行三情境分析並標記信心等級

用法:
  python ipssm_pipeline.py data.xlsx              # 一鍵全流程
  python ipssm_pipeline.py data.csv --screen-only  # 僅執行資料驗證
  python ipssm_pipeline.py cleaned.csv --translate-only  # 僅執行 R 計算
  python ipssm_pipeline.py data.xlsx -v validation.xlsx  # 指定驗證檔案比對
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font


# ============================================================================
#  常數定義
# ============================================================================

# 標準 IPSSM 42 欄
STANDARD_COLUMNS = [
    'ID', 'HB', 'PLT', 'BM_BLAST', 'del5q', 'del7_7q', 'complex', 'CYTO_IPSSR',
    'del17_17p', 'TP53mut', 'TP53maxvaf', 'TP53loh', 'MLL_PTD', 'FLT3', 'ASXL1',
    'BCOR', 'BCORL1', 'CBL', 'CEBPA', 'DNMT3A', 'ETV6', 'EZH2', 'IDH1', 'IDH2',
    'KRAS', 'NF1', 'NPM1', 'NRAS', 'RUNX1', 'SETBP1', 'SF3B1', 'SRSF2', 'STAG2',
    'U2AF1', 'ETNK1', 'GATA2', 'GNB1', 'PHF6', 'PPM1D', 'PRPF8', 'PTPN11', 'WT1'
]

REQUIRED_FIELDS = {'HB', 'PLT', 'BM_BLAST'}

NA_STRINGS = {
    '', ' ', 'NA', 'N/A', 'n/a', 'na', 'NaN', 'nan', 'None', 'none', '.', 'ND', 'nd'
}

VALIDATION_RULES = {
    'HB':         {'min': 4,   'max': 20},
    'PLT':        {'min': 0,   'max': 2000},
    'BM_BLAST':   {'min': 0,   'max': 30},
    'TP53maxvaf': {'min': 0,   'max': 1},
    'CYTO_IPSSR': {'values': ['Very Good', 'Good', 'Intermediate', 'Poor', 'Very Poor']},
    'TP53mut':    {'values': [0, 1, 2]},
}

BINARY_FIELDS = {
    'del5q', 'del7_7q', 'complex', 'del17_17p', 'MLL_PTD', 'FLT3', 'ASXL1',
    'BCOR', 'BCORL1', 'CBL', 'CEBPA', 'DNMT3A', 'ETV6', 'EZH2', 'IDH1', 'IDH2',
    'KRAS', 'NF1', 'NPM1', 'NRAS', 'RUNX1', 'SETBP1', 'SF3B1', 'SRSF2', 'STAG2',
    'U2AF1', 'ETNK1', 'GATA2', 'GNB1', 'PHF6', 'PPM1D', 'PRPF8', 'PTPN11', 'WT1'
}

# 欄位名稱別名對照表 (用於自動偵測不同醫院的欄位名稱)
COLUMN_ALIASES = {
    'ID':          ['ID', 'Patient_ID', 'PatientID', 'patient_id', 'PID', 'Chart No.', 'Chart_No', 'ChartNo'],
    'HB':          ['HB', 'Hemoglobin', 'hemoglobin', 'Hb'],
    'PLT':         ['PLT', 'Platelet', 'platelets', 'platelet_count'],
    'BM_BLAST':    ['BM_BLAST', 'Blast', 'BM Blast', 'bone marrow blast', 'BM_Blast', 'Blast_BM', 'Blast_bm'],
    'del5q':       ['del5q', 'del(5q)', 'DEL5Q', 'Deletion 5q'],
    'del7_7q':     ['del7_7q', 'del(7)', 'del(7q)', 'DEL7', 'Deletion 7'],
    'del17_17p':   ['del17_17p', 'del(17p)', 'del17p', 'DEL17P', '-17', 'Deletion 17p'],
    'complex':     ['complex', 'complex_karyotype', 'Complex Karyotype', 'Complex'],
    'TP53loh':     ['TP53loh', 'TP53 LOH', 'TP53_LOH', 'TP53-LOH'],
    'TP53mut':     ['TP53mut', 'TP53', 'TP53_Mutation', 'TP53 mutation'],
    'TP53maxvaf':  ['TP53maxvaf', 'TP53 VAF', 'TP53_VAF', 'TP53_maxVAF', 'TP53 maxVAF'],
    'SF3B1':       ['SF3B1'],
    'BCOR':        ['BCOR'], 'BCORL1': ['BCORL1'], 'CEBPA': ['CEBPA'],
    'ETNK1':       ['ETNK1'], 'GATA2': ['GATA2'], 'GNB1': ['GNB1'],
    'IDH1':        ['IDH1'], 'NF1': ['NF1'], 'PHF6': ['PHF6'],
    'PPM1D':       ['PPM1D'], 'PRPF8': ['PRPF8'], 'PTPN11': ['PTPN11'],
    'SETBP1':      ['SETBP1'], 'STAG2': ['STAG2'], 'WT1': ['WT1'],
    'FLT3':        ['FLT3', 'FLT3-ITD', 'FLT3_ITD'],
    'MLL_PTD':     ['MLL_PTD', 'MLL-PTD', 'MLL PTD'],
    'SF3B1_5q':    ['SF3B1_5q', 'SF3B1 5q'],
    'NPM1':        ['NPM1'], 'RUNX1': ['RUNX1'], 'NRAS': ['NRAS'],
    'ETV6':        ['ETV6'], 'IDH2': ['IDH2'], 'CBL': ['CBL'],
    'EZH2':        ['EZH2'], 'U2AF1': ['U2AF1'], 'SRSF2': ['SRSF2'],
    'DNMT3A':      ['DNMT3A'], 'ASXL1': ['ASXL1'], 'KRAS': ['KRAS'],
    'SF3B1_alpha': ['SF3B1_alpha', 'SF3B1-alpha'],
    'CYTO_IPSSR':  ['CYTO_IPSSR', 'CYTO_IPSS-R', 'CYTO IPSS-R', 'Cytogenetic IPSS-R'],
    'IPSS_M_':     ['IPSS_M_', 'IPSS_M', 'IPSSM', 'IPSS-M'],
}

# IPSS-M 交互作用/延伸欄位 (非核心 42 欄，但 R 模型可利用)。
# cohort 轉換後以「附加欄」形式保留，不覆蓋 STANDARD_COLUMNS 主 schema。
EXTRA_IPSSM_COLUMNS = ['SF3B1_5q', 'SF3B1_alpha', 'IPSS_M_']

# cohort 轉換完成後應具備的完整欄位集 = 標準 42 欄 + 延伸欄位。
# 注意: 過去的 STANDARD_IPSSM_COLUMNS 缺少 complex/del17_17p/TP53maxvaf/SF3B1，
# 導致核型解析出的 complex/del17_17p 被切片丟棄 — 這裡改以聯集保留所有欄位。
STANDARD_IPSSM_COLUMNS = STANDARD_COLUMNS + EXTRA_IPSSM_COLUMNS

R_SCRIPT_TEMPLATE = r"""#!/usr/bin/env Rscript

args <- commandArgs(trailingOnly=TRUE)
input_csv    <- args[1]
output_csv   <- args[2]

# Setup library paths (雲端 + 本地 Windows)
lib_candidates <- c(
  path.expand("~/R/library"),
  path.expand("~/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Local/R/win-library/4.5"),
  path.expand("~/AppData/Local/R/win-library/4.5"),
  "C:/Users/user/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Local/R/win-library/4.5",
  "C:/Users/user/AppData/Local/R/win-library/4.5"
)

for (lib_path in lib_candidates) {{
  if (dir.exists(lib_path)) .libPaths(c(lib_path, .libPaths()))
}}

suppressPackageStartupMessages({{
  library(ipssm, warn.conflicts=FALSE, verbose=FALSE)
}})

# Read data and check for missing cytogenetic data
data <- read.csv(input_csv, na.strings=c("", " ", "NA", "N/A", "n/a", "na", "NaN", "nan", "None", "none", ".", "ND", "nd"))

# Detect patients with missing cytogenetic fields (scenario analysis triggered)
cytogenetic_fields <- c("del5q", "del7_7q", "del17_17p", "CYTO_IPSSR")
has_missing_cyto <- apply(data[, cytogenetic_fields, drop=FALSE], 1, function(x) any(is.na(x)))

# Run IPSSMwrapper
results <- IPSSMwrapper(input_csv)

# Calculate range (worst_score - best_score) for confidence determination
results$Range_Score <- results$IPSSMscore_worst - results$IPSSMscore_best

# Mark confidence level based on range
results$Confidence_Level <- ifelse(results$Range_Score < 1, "CONFIDENT", "UNCERTAIN")

# Mark if scenario analysis was used (missing cytogenetic data)
results$Used_Scenario_Analysis <- ifelse(has_missing_cyto, "YES", "NO")

# Add detail about which cytogenetic fields were missing
results$Missing_Cytogenetic_Fields <- apply(
  data[, cytogenetic_fields, drop=FALSE],
  1,
  function(x) {{
    missing <- cytogenetic_fields[is.na(x)]
    if (length(missing) > 0) paste(missing, collapse=", ") else "NONE"
  }}
)

# Save results
write.csv(results, file=output_csv, row.names=FALSE, na="NA")

cat("\n[OK] Results generated with scenario analysis and confidence indicators\n")
"""


# ============================================================================
#  核型解析器 (Karyotype Parser)
#  從核型字符串 (如 "46,XY,del(5q)") 提取 IPSS-M 所需的細胞遺傳學欄位
# ============================================================================

def parse_karyotype(karyotype_str):
    """
    解析核型字符串，回傳 IPSS-M 所需的細胞遺傳學資訊。

    參數:
        karyotype_str: 核型字符串 (例: "46,XY,del(5q)", "47,XX,+8[17]/46,XX[3]")

    回傳 dict:
        del5q    (int): 1=有 del(5q), 0=無
        del7_7q  (int): 1=有 -7/del(7q), 0=無
        del17p   (int): 1=有 -17/del(17p), 0=無
        complex  (int): 1=複雜核型(≥3異常), 0=非複雜
        cyto_ipssr (str): 'Very Good'/'Good'/'Intermediate'/'Poor'/'Very Poor'/'NA'
    """
    if not karyotype_str or pd.isna(karyotype_str):
        return {'del5q': 0, 'del7_7q': 0, 'del17p': 0, 'complex': 0, 'cyto_ipssr': 'NA'}

    karyotype = str(karyotype_str).strip().upper()
    if karyotype in ('NM', ''):
        return {'del5q': 0, 'del7_7q': 0, 'del17p': 0, 'complex': 0, 'cyto_ipssr': 'NA'}

    abn = _extract_abnormalities(karyotype)
    is_complex = abn['abnorm_count'] >= 3
    cyto = _classify_cytogenetics(abn, abn['has_del5q'], abn['has_del7'], abn['has_del17p'], is_complex)

    return {
        'del5q': 1 if abn['has_del5q'] else 0,
        'del7_7q': 1 if abn['has_del7'] else 0,
        'del17p': 1 if abn['has_del17p'] else 0,
        'complex': 1 if is_complex else 0,
        'cyto_ipssr': cyto,
    }


def _extract_abnormalities(karyotype):
    """
    從核型字符串中提取所有染色體異常。

    偵測項目:
      - del(5q), -7/del(7q), -17/del(17p), del(11q), del(12p), del(20q)
      - -Y, +8, +19, i(17q), inv(3)/i(3q)
      - 計算總異常數以判斷是否為複雜核型
    """
    abn = {
        'has_del5q': False, 'has_del7': False, 'has_del17p': False, 'has_del11q': False,
        'has_del12p': False, 'has_del20q': False, 'has_minus_y': False, 'has_plus8': False,
        'has_plus19': False, 'has_i17q': False, 'has_inv3': False, 'has_i3q': False,
        'has_minus7': False, 'abnorm_count': 0, 'is_normal': False,
    }

    if karyotype in ('46,XX', '46,XY'):
        abn['is_normal'] = True
        return abn

    karyotype_clean = re.sub(r'\[\d+\]', '', karyotype)
    abnorm_list = []

    if re.search(r'DEL\(\s*5|5Q-', karyotype_clean):
        abn['has_del5q'] = True
        abnorm_list.append('del5q')
    if re.search(r'^\s*-7\b|,\s*-7\b', karyotype_clean):
        abn['has_minus7'] = abn['has_del7'] = True
        abnorm_list.append('del7')
    elif re.search(r'DEL\(\s*7', karyotype_clean):
        abn['has_del7'] = True
        abnorm_list.append('del7')
    if re.search(r'^\s*-17\b|,\s*-17\b|DEL\(\s*17\w*P', karyotype_clean):
        abn['has_del17p'] = True
        abnorm_list.append('del17p')
    elif re.search(r'I\(\s*17\s*Q', karyotype_clean):
        abn['has_i17q'] = True
    if re.search(r'DEL\(\s*11\w*Q', karyotype_clean):
        abn['has_del11q'] = True
        abnorm_list.append('del11q')
    if re.search(r'DEL\(\s*12\w*P', karyotype_clean):
        abn['has_del12p'] = True
        abnorm_list.append('del12p')
    if re.search(r'DEL\(\s*20', karyotype_clean):
        abn['has_del20q'] = True
        abnorm_list.append('del20q')
    if re.search(r'^\s*-Y\b|,\s*-Y\b', karyotype_clean):
        abn['has_minus_y'] = True
        abnorm_list.append('-Y')
    if re.search(r',?\s*\+8\b', karyotype_clean):
        abn['has_plus8'] = True
    if re.search(r',?\s*\+19\b', karyotype_clean):
        abn['has_plus19'] = True
    if re.search(r'INV\(\s*3|I\(\s*3', karyotype_clean):
        abn['has_inv3'] = abn['has_i3q'] = True

    other_abn = (len(re.findall(r',?\s*\+\d+', karyotype_clean))
                 + len(re.findall(r'DEL\(', karyotype_clean))
                 + len(re.findall(r'DUP\(|INV\(', karyotype_clean)))
    abn['abnorm_count'] = len(abnorm_list) + max(0, other_abn - len([x for x in abnorm_list if 'del' in x]))
    return abn


def _classify_cytogenetics(abn, has_del5q, has_del7, has_del17p, is_complex):
    """
    根據 IPSS-R 標準將異常分類為細胞遺傳學風險類別。

    分類規則 (依 IPSS-R):
      Very Good: -Y, del(11q)
      Good:      Normal, del(5q), del(12p), del(20q), 含 del(5q) 的雙異常
      Intermediate: del(7q), +8, +19, i(17q), 其他單/雙獨立異常
      Poor:      -7, inv(3)/i(3q)/del(3q), 含 -7/del(7q) 雙異常, 複雜(3個異常)
      Very Poor: 複雜(>3個異常)
    """
    if abn['abnorm_count'] > 3:
        return 'Very Poor'
    if abn['has_minus_y'] or abn['has_del11q']:
        return 'Very Good'
    if abn['has_minus7'] or abn['has_inv3'] or abn['has_i3q'] or (is_complex and abn['abnorm_count'] == 3):
        return 'Poor'
    if abn['is_normal'] or (has_del5q and not has_del7 and not has_del17p) or \
       (abn['has_del12p'] and not has_del7) or (abn['has_del20q'] and not has_del7):
        return 'Good'
    if has_del7 and not abn['has_minus7'] and not has_del17p:
        return 'Intermediate'
    if abn['has_plus8'] or abn['has_plus19'] or abn['has_i17q'] or \
       (abn['abnorm_count'] in (1, 2) and not abn['has_minus7']):
        return 'Intermediate'
    return 'NA'


# ============================================================================
#  隊列格式轉換 (Cohort Converter)
#  自動偵測並轉換不同醫院/研究隊列的欄位名稱與結構
# ============================================================================

def detect_cohort_type(df):
    """
    根據欄位名稱自動偵測隊列來源類型。

    回傳 str: 'FJUH' / 'HSCT' / 'UNKNOWN'
    """
    columns_lower = [str(col).lower().strip() for col in df.columns]
    fjuh_markers = ['ethnicity', 'diagnosis', 'karyotype']
    hsct_markers = ['transplant', 'graft', 'donor', 'conditioning']
    fjuh_count = sum(1 for m in fjuh_markers if any(m in c for c in columns_lower))
    hsct_count = sum(1 for m in hsct_markers if any(m in c for c in columns_lower))
    if hsct_count > fjuh_count:
        return 'HSCT'
    elif fjuh_count > 0:
        return 'FJUH'
    return 'UNKNOWN'


# 括號內容若「整段」符合常見檢驗單位樣式才會被移除 (例如 "(g/dL)", "(%)",
# "(x10^9/L)")；否則保留括號內容，避免破壞像 del(5q)/del(17p) 這種括號本身就是
# 欄位語意一部分的別名 (5q/17p 不是單位，不該被當成單位噪音濾掉)。
_UNIT_PAREN_RE = re.compile(
    r'^[\d.]*(G/DL|MG/DL|G/L|MMOL/L|UMOL/L|X?10\^?9/?L|%|/UL|/ML|K/UL)$'
)


def _normalize_header(name):
    """
    將欄名正規化以供比對: 轉大寫、視情況移除括號內的單位標註、
    把底線/連字號/空白/句點/斜線視為分隔符移除。

    僅做正規化比對，不做模糊/相似度比對，避免誤判。
    """
    if name is None:
        return ''
    s = str(name).strip().upper()

    def _paren_repl(m):
        inner = re.sub(r'\s+', '', m.group(1))
        return '' if _UNIT_PAREN_RE.match(inner) else inner

    s = re.sub(r'\(([^)]*)\)', _paren_repl, s)  # 括號: 單位則移除, 否則保留內容
    s = re.sub(r'[\s_\-./^]+', '', s)           # 移除空白/底線/連字號/句點/斜線/^
    return s


def find_column_mapping(input_df):
    """
    根據 COLUMN_ALIASES 對照表，自動找出輸入欄位對應到哪個標準 IPSSM 欄位。

    比對時會先正規化欄名(去除單位括號、底線、空白、大小寫差異)，
    例如 "Hemoglobin (g/dL)" 或 "PLT_count" 仍能對應到標準欄位。

    回傳 dict: {原始欄名: 標準欄名}
    """
    mapping = {}
    used_cols = set()
    for std_col, aliases in COLUMN_ALIASES.items():
        normalized_aliases = {_normalize_header(a) for a in aliases}
        for input_col in input_df.columns:
            if input_col in used_cols:
                continue
            if _normalize_header(input_col) in normalized_aliases:
                mapping[input_col] = std_col
                used_cols.add(input_col)
                break  # 每個標準欄位只對應第一個匹配到的來源欄，維持一對一
    return mapping


def _read_any_table(input_path):
    """讀取 CSV 或 Excel 為 DataFrame(皆為字串型別)，供 inspect/cohort 轉換共用。"""
    input_path = Path(input_path)
    if input_path.suffix.lower() == '.xlsx':
        return pd.read_excel(input_path, sheet_name=0, dtype=str, keep_default_na=False)
    return pd.read_csv(input_path, dtype=str, keep_default_na=False)


def inspect_columns(input_path):
    """
    唯讀掃描來源檔案標題並回報欄位對應狀況，不寫任何輸出檔、不執行驗證或計算。

    用於「來源檔案不是乾淨標準表格」時，先讓使用者(或呼叫此函式的 agent skill)
    確認自動對應是否正確，再決定是否繼續執行完整流程。

    回傳 dict:
      cohort_type:            偵測到的隊列類型 (FJUH/HSCT/UNKNOWN，僅供參考)
      input_columns:          來源檔案的原始欄名列表
      matched:                {來源欄名: 標準欄名} 確定匹配的對應
      missing_required:       必填但未匹配到的標準欄位 (HB/PLT/BM_BLAST) — 會導致該病患被跳過
      missing_optional:       選填但未匹配到的標準欄位 — 安全填為 NA
      unmapped_input_columns: 來源欄位中未對應到任何標準欄位的欄名
      warnings:               健檢提示 (例如 HB 數值量級疑似單位錯誤)
    """
    df = _read_any_table(input_path)

    cohort_type = detect_cohort_type(df)
    mapping = find_column_mapping(df)
    matched_std_cols = set(mapping.values())

    missing_std_cols = [c for c in STANDARD_COLUMNS if c not in matched_std_cols]
    missing_required = [c for c in missing_std_cols if c in REQUIRED_FIELDS]
    missing_optional = [c for c in missing_std_cols if c not in REQUIRED_FIELDS]
    unmapped_input_columns = [c for c in df.columns if c not in mapping]

    warnings = []
    hb_input_col = next((src for src, std in mapping.items() if std == 'HB'), None)
    if hb_input_col is not None:
        numeric = pd.to_numeric(df[hb_input_col], errors='coerce').dropna()
        if len(numeric) > 0 and numeric.median() > 25:
            warnings.append(
                f"[UNIT CHECK] 欄位 '{hb_input_col}' (對應 HB) 數值中位數為 {numeric.median():.1f}，"
                f"明顯高於 IPSS-M 所需的 g/dL 範圍 (4-20)。若您的資料是 g/L 單位，"
                f"請先將該欄除以 10 再輸入 (只提示不自動轉換，避免靜默改資料)。"
            )

    return {
        'cohort_type': cohort_type,
        'input_columns': list(df.columns),
        'matched': mapping,
        'missing_required': missing_required,
        'missing_optional': missing_optional,
        'unmapped_input_columns': unmapped_input_columns,
        'warnings': warnings,
    }


def print_inspect_report(result):
    """將 inspect_columns() 的結果印成人類可讀的預覽報告。"""
    print(f"\n{'='*60}")
    print(f"  欄位掃描預覽 (--inspect，唯讀，不寫入任何輸出檔)")
    print(f"{'='*60}")
    print(f"  偵測隊列類型: {result['cohort_type']} (僅供參考)")
    print(f"  來源欄位數:   {len(result['input_columns'])}")

    print(f"\n--- 確定匹配 ({len(result['matched'])}) ---")
    for src, std in sorted(result['matched'].items(), key=lambda kv: kv[1]):
        print(f"  [OK] '{src}' -> {std}")

    if result['missing_required']:
        print(f"\n--- !! 必填欄位缺失 ({len(result['missing_required'])}) — 會導致病患被跳過 ---")
        for col in result['missing_required']:
            print(f"  [MISSING-REQUIRED] {col}")

    if result['missing_optional']:
        print(f"\n--- 選填欄位缺失 ({len(result['missing_optional'])}) — 將安全填為 NA ---")
        shown = result['missing_optional'][:10]
        print(f"  {', '.join(shown)}" + (f" ... 及其他 {len(result['missing_optional']) - 10} 個" if len(result['missing_optional']) > 10 else ""))

    if result['unmapped_input_columns']:
        print(f"\n--- 來源欄位中未對應到任何標準欄位 ({len(result['unmapped_input_columns'])}) ---")
        for col in result['unmapped_input_columns']:
            print(f"  [unmapped] '{col}'")

    for w in result['warnings']:
        print(f"\n  {w}")

    print()
    if result['missing_required']:
        print("  >>> 建議: 請確認上述必填欄位在來源檔案中的實際欄名，")
        print("            必要時手動重新命名欄位，或告知對應的欄名後再重新執行。")
    else:
        print("  >>> 必填欄位皆已確定匹配，可以繼續執行完整計算。")
    print()


# ============================================================================
#  第一階段：資料驗證 (Screener)
# ============================================================================

class ValidationReport:
    """收集驗證過程中的錯誤、警告、自動修復和跳過記錄"""

    def __init__(self):
        self.errors = []
        self.warnings = []
        self.auto_fixes = []
        self.skipped_patients = []
        self.input_rows = 0
        self.output_rows = 0
        self.input_cols = 0
        self.output_cols = 0
        self.format_conversions = []

    def add_error(self, row, col, msg):
        self.errors.append(f"Row {row}, {col}: {msg}")

    def add_warning(self, row, col, msg):
        self.warnings.append(f"Row {row}, {col}: {msg}")

    def add_fix(self, row, col, old_val, new_val, reason):
        self.auto_fixes.append(f"Row {row}, {col}: '{old_val}' -> '{new_val}' ({reason})")

    def add_conversion(self, source_col, target_col):
        self.format_conversions.append(f"{source_col} -> {target_col}")

    def skip_patient(self, patient_id, reason):
        self.skipped_patients.append(f"ID={patient_id}: {reason}")

    def report(self):
        lines = [
            f"\nSummary: {len(self.errors)} ERRORS, {len(self.warnings)} WARNINGS, "
            f"{len(self.auto_fixes)} AUTO-FIXES, {len(self.skipped_patients)} SKIPPED\n"
        ]
        if self.format_conversions:
            lines.append("\n--- FORMAT CONVERSIONS ---")
            for conv in self.format_conversions[:10]:
                lines.append(f"  {conv}")
            if len(self.format_conversions) > 10:
                lines.append(f"  ... and {len(self.format_conversions) - 10} more")
        if self.input_rows > 0:
            lines.append("\n--- INFO ---")
            lines.append(f"  [INFO] Input: {self.input_rows} rows x {self.input_cols} columns")
            lines.append(f"  [INFO] Output: {self.output_rows} rows x {self.output_cols} columns")
            lines.append(f"  [INFO] Skipped: {len(self.skipped_patients)} patients with missing required fields")
        if self.skipped_patients:
            lines.append(f"\n--- SKIPPED PATIENTS ({len(self.skipped_patients)}) ---")
            for skip in self.skipped_patients[:20]:
                lines.append(f"  [SKIP] {skip}")
            if len(self.skipped_patients) > 20:
                lines.append(f"  ... and {len(self.skipped_patients) - 20} more")
        if self.auto_fixes:
            lines.append(f"\n--- AUTO-FIXES APPLIED ({len(self.auto_fixes)}) ---")
            for fix in self.auto_fixes[:30]:
                lines.append(f"  {fix}")
            if len(self.auto_fixes) > 30:
                lines.append(f"  ... and {len(self.auto_fixes) - 30} more")
        if self.errors:
            lines.append(f"\n--- ERRORS ({len(self.errors)}) ---")
            for error in self.errors[:20]:
                lines.append(f"  {error}")
            if len(self.errors) > 20:
                lines.append(f"  ... and {len(self.errors) - 20} more")
        if self.warnings:
            lines.append(f"\n--- WARNINGS ({len(self.warnings)}) ---")
            for warning in self.warnings[:20]:
                lines.append(f"  {warning}")
            if len(self.warnings) > 20:
                lines.append(f"  ... and {len(self.warnings) - 20} more")
        if len(self.errors) == 0 and len(self.skipped_patients) == 0:
            lines.append("\n>>> PASS: Data is ready for R IPSSMwrapper execution.")
        elif len(self.errors) == 0:
            lines.append(f"\n>>> PASS: {self.output_rows} patients passed validation "
                         f"(after skipping {len(self.skipped_patients)} incomplete records).")
        else:
            lines.append(f"\n>>> FAIL: {len(self.errors)} validation errors found.")
        return '\n'.join(lines) + '\n'


def _read_input_file(input_path):
    """讀取 CSV 或 Excel 檔案，回傳 (rows_list, fieldnames)"""
    input_path = Path(input_path)
    if input_path.suffix.lower() == '.xlsx':
        df = pd.read_excel(input_path, sheet_name=0, dtype=str, keep_default_na=False)
        return df.to_dict('records'), list(df.columns)
    with open(input_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames if reader.fieldnames else []
    return rows, fieldnames


def _try_convert_cohort(input_path, report):
    """
    嘗試自動偵測並轉換輸入檔案的隊列格式。

    流程:
      1. 偵測隊列類型 (FJUH / HSCT / UNKNOWN)
      2. 自動對應欄位名稱到 IPSSM 標準欄位
      3. 若有 karyotype 欄位，自動解析為細胞遺傳學欄位
      4. 補齊缺失欄位為 'NA'

    回傳 (rows, fieldnames, converted: bool)
    """
    try:
        input_path = Path(input_path)
        df = _read_any_table(input_path)

        cohort_type = detect_cohort_type(df)
        mapping = find_column_mapping(df)
        is_standard = len(mapping) == len(df.columns) and set(mapping.values()) == set(df.columns)

        # 欄位對應不再只在偵測到已知隊列類型(FJUH/HSCT)時才嘗試 — 完全自訂的醫院
        # 格式(無 ethnicity/diagnosis/karyotype/transplant 等特徵欄)先前會讓 cohort_type
        # 停在 UNKNOWN、整段轉換邏輯被跳過，導致欄位對應完全不執行。只要找到任何
        # 可用的別名對應就進行轉換；cohort_type 僅作為報告用的描述性標籤。
        if mapping and not is_standard:
            print(f"\n  [COHORT DETECTION] Detected format: {cohort_type}")
            print(f"  [COLUMN MAPPING] Found {len(mapping)}/{len(df.columns)} matching columns")

            df_converted = df.rename(columns=mapping)

            # 自動解析核型欄位
            karyotype_col = None
            for col in df.columns:
                if 'karyotype' in col.lower():
                    karyotype_col = col
                    break

            if karyotype_col and 'del5q' not in df_converted.columns:
                print(f"  [AUTO] Parsing karyotype from '{karyotype_col}'...")
                parsed_count = 0
                for idx, row in df_converted.iterrows():
                    karyotype_val = df.loc[idx, karyotype_col]
                    if karyotype_val:
                        parsed = parse_karyotype(karyotype_val)
                        df_converted.at[idx, 'del5q'] = str(parsed['del5q'])
                        df_converted.at[idx, 'del7_7q'] = str(parsed['del7_7q'])
                        df_converted.at[idx, 'del17_17p'] = str(parsed['del17p'])
                        df_converted.at[idx, 'complex'] = str(parsed['complex'])
                        df_converted.at[idx, 'CYTO_IPSSR'] = parsed['cyto_ipssr']
                        parsed_count += 1
                    else:
                        df_converted.at[idx, 'del5q'] = '0'
                        df_converted.at[idx, 'del7_7q'] = '0'
                        df_converted.at[idx, 'del17_17p'] = '0'
                        df_converted.at[idx, 'complex'] = '0'
                        df_converted.at[idx, 'CYTO_IPSSR'] = 'NA'
                print(f"  [OK] Parsed {parsed_count} karyotypes")

            missing_cols = set(STANDARD_IPSSM_COLUMNS) - set(df_converted.columns)
            if missing_cols:
                print(f"  [MISSING COLUMNS] Adding {len(missing_cols)} missing columns: "
                      f"{', '.join(sorted(missing_cols)[:5])}{'...' if len(missing_cols) > 5 else ''}")
                for col in missing_cols:
                    df_converted[col] = 'NA'

            # 依 STANDARD_IPSSM_COLUMNS 排序，但保留任何額外已算出的欄位(如核型解析的
            # complex/del17_17p)，避免像舊版一樣被切片丟棄。
            ordered = STANDARD_IPSSM_COLUMNS + [
                c for c in df_converted.columns if c not in STANDARD_IPSSM_COLUMNS
            ]
            df_converted = df_converted[ordered]
            rows = df_converted.to_dict('records')
            fieldnames = list(df_converted.columns)
            report.add_conversion(f"Cohort type '{cohort_type}'", "Standard IPSSM format")
            return rows, fieldnames, True

        # 非 cohort 路徑: 直接重用已載入的 DataFrame，避免二次讀檔。
        return df.to_dict('records'), list(df.columns), False

    except Exception as e:
        print(f"  [CONVERSION WARNING] Could not auto-detect cohort format: {e}")
        rows, fieldnames = _read_input_file(input_path)
        return rows, fieldnames, False


def _convert_fjuh_format(rows, fieldnames, report):
    """偵測並修復欄名尾部空格格式"""
    has_fjuh = any(col.endswith(' ') for col in fieldnames)
    if not has_fjuh:
        return rows

    print("  偵測到異常格式（欄名有尾部空格），自動修復中...")
    mapping = {}
    for col in fieldnames:
        clean_col = col.strip()
        if col != clean_col:
            mapping[col] = clean_col
            report.add_conversion(col, clean_col)

    converted = []
    for row in rows:
        new_row = {}
        for key, value in row.items():
            clean_key = mapping.get(key, key.strip() if isinstance(key, str) else key)
            new_row[clean_key] = value
        converted.append(new_row)
    return converted


def _validate_row(row_idx, row, report):
    """
    驗證單一資料列，回傳 True=有效 / False=跳過。

    驗證步驟:
      1. NA 標準化 (含 ND -> NA 轉換)
      2. 必填欄位檢查 (HB, PLT, BM_BLAST)
      3. 數值範圍檢查
      4. 二元欄位 (0/1) 檢查
      5. 分類欄位 (CYTO_IPSSR, TP53mut) 檢查
    """
    patient_id = row.get('ID', f'Row{row_idx}')
    # 記錄進入本列前的錯誤數，讓有效性判斷只依「本列」是否新增錯誤，
    # 避免前面某列的錯誤導致其後所有正常病患被誤判為無效而遭丟棄。
    errors_before = len(report.errors)

    # NA 標準化 (including ND -> NA for CYTO_IPSSR)
    for col, value in row.items():
        if isinstance(value, str):
            if col == 'CYTO_IPSSR' and value.strip().upper() == 'ND':
                row[col] = 'NA'
                report.add_fix(row_idx, col, value, 'NA', 'Converted ND (not detected) to NA')
            elif value in NA_STRINGS:
                row[col] = 'NA'
                report.add_fix(row_idx, col, value, 'NA', 'Converted NA-like value to standard NA')

    # 必填欄位檢查
    missing = [f for f in REQUIRED_FIELDS if row.get(f, '').strip() in NA_STRINGS or row.get(f, '').strip() == '']
    if missing:
        report.skip_patient(patient_id, f"Missing required field(s): {', '.join(missing)}")
        return False

    # 數值範圍檢查
    for col in ['HB', 'PLT', 'BM_BLAST', 'TP53maxvaf']:
        value = row.get(col, 'NA')
        if isinstance(value, str):
            value = value.strip()
        if value != 'NA':
            try:
                num = float(value)
                rule = VALIDATION_RULES.get(col)
                if rule and 'min' in rule:
                    if num < rule['min'] or num > rule['max']:
                        report.add_error(row_idx, col, f"Value {num} out of range [{rule['min']}-{rule['max']}]")
            except ValueError:
                report.add_error(row_idx, col, f"Invalid number: {value}")

    # 二元欄位檢查
    for col in BINARY_FIELDS:
        value = row.get(col, 'NA')
        if isinstance(value, str):
            value = value.strip()
        if value != 'NA' and value not in ('0', '1'):
            report.add_error(row_idx, col, f"Binary field must be 0 or 1, got: {value}")

    # 分類欄位檢查
    cyto = row.get('CYTO_IPSSR', 'NA')
    if isinstance(cyto, str):
        cyto = cyto.strip()
    if cyto not in ('NA', 'Very Good', 'Good', 'Intermediate', 'Poor', 'Very Poor'):
        report.add_error(row_idx, 'CYTO_IPSSR', f"Invalid category: {cyto}")

    tp53 = row.get('TP53mut', 'NA')
    if isinstance(tp53, str):
        tp53 = tp53.strip()
    if tp53 not in ('NA', '0', '1', '2', '2 or more'):
        report.add_error(row_idx, 'TP53mut', f"Invalid value: {tp53}")

    # 只依本列是否新增錯誤來判定有效性 (見上方 errors_before)。
    return len(report.errors) == errors_before


def clean_dataframe(raw_df, collapse_tp53=False):
    """
    輕量級 DataFrame 清理，供 Streamlit API 引擎與其他呼叫端重用。

    步驟:
      1. 去除欄名尾部空格
      2. 跳過缺少必填欄位 (HB/PLT/BM_BLAST) 的列 (記於 report.skipped_patients)
      3. NA 標準化 (NA_STRINGS -> 'NA')
      4. 對齊為 STANDARD_COLUMNS 42 欄
      5. collapse_tp53=True 時，將 TP53mut 的 '2'/'>1'/'2 or more' 統一為 '2 or more'
         (官方 REST API 需要此格式)

    參數:
        raw_df: 原始 pandas DataFrame
        collapse_tp53: 是否將 TP53mut 折疊為 '2 or more' (API 引擎用)

    回傳 (cleaned_df: DataFrame[STANDARD_COLUMNS], report: ValidationReport)
    """
    report = ValidationReport()
    df = raw_df.astype(str)

    # 1. 去除欄名尾部空格
    rename_map = {col: col.strip() for col in df.columns if col != col.strip()}
    if rename_map:
        df = df.rename(columns=rename_map)
        for src, dst in rename_map.items():
            report.add_conversion(src, dst)

    report.input_rows = len(df)
    report.input_cols = len(df.columns)

    valid_rows = []
    for idx, row in df.iterrows():
        row_dict = row.to_dict()
        patient_id = str(row_dict.get('ID', f'Row{idx + 2}')).strip()

        missing = [
            f for f in REQUIRED_FIELDS
            if str(row_dict.get(f, '')).strip() in NA_STRINGS
        ]
        if missing:
            report.skip_patient(patient_id, f"Missing required field(s): {', '.join(missing)}")
            continue

        cleaned_row = {}
        for col_name, value in row_dict.items():
            clean_val = str(value).strip()
            cleaned_row[col_name.strip()] = 'NA' if clean_val in NA_STRINGS else clean_val

        if collapse_tp53 and cleaned_row.get('TP53mut') in ('2', '>1', '2 or more'):
            cleaned_row['TP53mut'] = '2 or more'

        valid_rows.append({col: cleaned_row.get(col, 'NA') for col in STANDARD_COLUMNS})

    report.output_rows = len(valid_rows)
    report.output_cols = len(STANDARD_COLUMNS)
    return pd.DataFrame(valid_rows, columns=STANDARD_COLUMNS), report


def run_screening(input_path, output_path, log_path):
    """
    執行資料驗證（第一階段），回傳是否成功。

    流程:
      1. 嘗試隊列格式轉換 (_try_convert_cohort)
      2. 修復欄名空格 (_convert_fjuh_format)
      3. 逐列驗證 (_validate_row)
      4. 輸出清理後 CSV 與驗證日誌
    """
    print(f"\n{'='*60}")
    print(f"  [階段 1] 資料驗證 & 格式轉換")
    print(f"{'='*60}")
    print(f"  輸入:  {input_path}")
    print(f"  輸出:  {output_path}")
    print(f"  日誌:  {log_path}\n")

    report = ValidationReport()
    valid_rows = []

    try:
        rows, fieldnames, converted = _try_convert_cohort(input_path, report)
        if converted:
            print(f"  [OK] Successfully converted to standard IPSSM format\n")

        report.input_cols = len(fieldnames)
        report.input_rows = len(rows)
        rows = _convert_fjuh_format(rows, fieldnames, report)

        for row_idx, row in enumerate(rows, start=2):
            cleaned = {
                (k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
                for k, v in row.items()
            }
            errors_before = len(report.errors)
            if _validate_row(row_idx, cleaned, report):
                valid_rows.append(cleaned)
            else:
                # 有錯誤的列不進 R 計算，但明確記錄為 SKIP(而非靜默丟棄)，
                # 且不影響其他正常病患。
                new_errors = report.errors[errors_before:]
                reason = new_errors[0] if new_errors else "Validation error"
                report.skip_patient(cleaned.get('ID', f'Row{row_idx}'), reason)

        final_rows = []
        for row in valid_rows:
            final_row = {col: row.get(col, 'NA') for col in STANDARD_COLUMNS}
            final_rows.append(final_row)
        report.output_rows = len(final_rows)
        report.output_cols = len(STANDARD_COLUMNS)

        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=STANDARD_COLUMNS)
            writer.writeheader()
            writer.writerows(final_rows)

        report_text = report.report()
        print(report_text)

        with open(log_path, 'w', encoding='utf-8') as f:
            f.write("=" * 70 + "\n")
            f.write("IPSSM Screener Validation Report\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 70 + "\n")
            f.write(report_text)

        print(f"  [OK] Cleaned CSV: {output_path}")
        print(f"  [OK] 驗證日誌:    {log_path}")
        # 只要有任何有效輸出列即視為成功；有問題的列已被記錄為 SKIP，
        # 不再因單筆錯誤而讓整批作廢。
        return report.output_rows > 0

    except Exception as e:
        print(f"  [ERROR] ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================================================
#  第二階段：R 計算 + 結果整合 (Translator)
# ============================================================================

def _find_rscript():
    """自動尋找 Rscript 路徑 (跨平台: 環境變數 > PATH > 常見安裝路徑)"""
    # 1. 明確覆寫 (最高優先)
    override = os.environ.get('IPSSM_RSCRIPT')
    if override and Path(override).exists():
        return override

    # 2. PATH 搜尋 (posix 與 Windows 皆適用)
    for name in ('Rscript', 'Rscript.exe'):
        found = shutil.which(name)
        if found:
            return found

    # 3. 常見 Windows 安裝路徑後備
    candidates = [
        r"C:\Program Files\R\R-4.5.2\bin\Rscript.exe",
        r"C:\Program Files\R\R-4.4.2\bin\Rscript.exe",
        r"C:\Program Files\R\R-4.3.0\bin\Rscript.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _read_r_output(csv_path):
    """讀取 R 輸出 CSV"""
    with open(csv_path, 'r', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _read_validation_data(validation_path):
    """從 Excel 讀取驗證數據"""
    try:
        if validation_path and validation_path.exists():
            df = pd.read_excel(validation_path, sheet_name=0, dtype=str)
            result = {}
            for _, row in df.iterrows():
                pid = str(row.get('ID', '')).strip() if 'ID' in row else None
                if pid:
                    result[pid] = dict(row)
            return result
    except Exception as e:
        print(f"  Warning: 無法讀取驗證檔案: {e}")
    return {}


def _save_excel(r_results, output_path, validation_data=None):
    """儲存結果到多工作表 Excel"""
    wb = Workbook()
    wb.remove(wb.active)

    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    uncertain_fill = PatternFill(start_color="FF6B6B", end_color="FF6B6B", fill_type="solid")
    uncertain_font = Font(bold=True, color="FFFFFF")

    # --- Sheet 1: Summary ---
    ws = wb.create_sheet("Summary", 0)
    ws.append(['ID', 'Confidence_Level'])
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
    for row in r_results:
        ws.append([row.get('ID', ''), row.get('Confidence_Level', 'NA')])
        if row.get('Confidence_Level') == 'UNCERTAIN':
            for cell in ws[ws.max_row]:
                cell.fill = uncertain_fill
                cell.font = uncertain_font
    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 16

    # --- Sheet 2: R_Full_Output ---
    ws_r = wb.create_sheet("R_Full_Output", 1)
    if r_results:
        headers = list(r_results[0].keys())
        ws_r.append(headers)
        for cell in ws_r[1]:
            cell.fill = header_fill
            cell.font = header_font
        for row in r_results:
            ws_r.append([row.get(h, '') for h in headers])
        for col in ws_r.columns:
            ws_r.column_dimensions[col[0].column_letter].width = 14

    # --- Sheet 3: Analysis (if validation data available) ---
    if validation_data:
        ws_a = wb.create_sheet("Analysis", 2)
        a_headers = ['ID', 'R_Confidence', 'R_Category', 'Validation_Result', 'Match', 'Range_Score', 'Notes']
        ws_a.append(a_headers)
        for cell in ws_a[1]:
            cell.fill = header_fill
            cell.font = header_font

        match_fill = PatternFill(start_color="92D050", end_color="92D050", fill_type="solid")
        mismatch_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

        for row in r_results:
            pid = row.get('ID', '')
            r_conf = row.get('Confidence_Level', 'NA')
            r_cat = row.get('IPSSMcat', 'NA')
            r_range = row.get('Range_Score', 'NA')
            val = validation_data.get(pid, {})
            val_result = val.get('IPSS_M_', 'N/A') if val else 'N/A'
            match = 'YES' if r_cat == val_result else ('NO' if val_result != 'N/A' else 'N/A')
            notes = ''
            if match == 'NO':
                notes = f"Expected: {val_result}, Got: {r_cat}"
            elif r_conf == 'UNCERTAIN':
                notes = "Wide range, check carefully"

            ws_a.append([pid, r_conf, r_cat, val_result, match, r_range, notes])
            cur = ws_a.max_row
            if match == 'YES':
                for cell in ws_a[cur]:
                    cell.fill = match_fill
            elif match == 'NO':
                for cell in ws_a[cur]:
                    cell.fill = mismatch_fill

        for letter, width in zip('ABCDEFG', [12, 14, 14, 16, 10, 12, 30]):
            ws_a.column_dimensions[letter].width = width

    wb.save(output_path)


def run_translation(input_csv, rscript_path=None, validation_path=None):
    """執行 R 計算 + 結果整合（第二階段），回傳是否成功"""
    print(f"\n{'='*60}")
    print(f"  [階段 2] R 風險計算 & 信心等級標記")
    print(f"{'='*60}")

    input_path = Path(input_csv)
    rscript = rscript_path or _find_rscript()
    if not rscript:
        print("  [ERROR] ERROR: 找不到 Rscript！請安裝 R >= 4.3.0")
        return False

    output_dir = input_path.parent
    r_output_csv = output_dir / f"{input_path.stem}_r_output.csv"
    excel_output = output_dir / f"{input_path.stem}_results.xlsx"

    if not validation_path:
        potential = output_dir / "IPSSM_validation_result.xlsx"
        if potential.exists():
            validation_path = potential

    print(f"  輸入:    {input_path}")
    print(f"  Rscript: {rscript}")
    print(f"  輸出:    {excel_output}")
    if validation_path:
        print(f"  驗證檔:  {validation_path}")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.R', delete=False) as f:
        f.write(R_SCRIPT_TEMPLATE)
        r_script_file = f.name

    try:
        print("\n  執行 R IPSSMwrapper...")
        cmd = [rscript, r_script_file, str(input_path), str(r_output_csv)]

        r_env = os.environ.copy()
        r_env["R_LIBS_USER"] = os.path.expanduser("~/R/library")
        result = subprocess.run(cmd, capture_output=True, text=True, env=r_env)

        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("  [STDERR]:", result.stderr)

        if result.returncode != 0:
            print("  [ERROR] ERROR: R 執行失敗")
            with open(output_dir / "r_error.log", "w", encoding="utf-8") as f:
                f.write(result.stderr or "No stderr output recorded.")
            return False

        if not r_output_csv.exists():
            print("  [ERROR] ERROR: R 輸出檔案未產生")
            with open(output_dir / "r_error.log", "w", encoding="utf-8") as f:
                f.write("R completed but output file was not generated.\n\n[STDOUT]\n"
                        + (result.stdout or "") + "\n\n[STDERR]\n" + (result.stderr or ""))
            return False

        print("  處理結果中...")
        r_results = _read_r_output(r_output_csv)

        validation_data = {}
        if validation_path:
            validation_data = _read_validation_data(validation_path)

        confident = sum(1 for r in r_results if r.get('Confidence_Level') == 'CONFIDENT')
        uncertain = sum(1 for r in r_results if r.get('Confidence_Level') == 'UNCERTAIN')

        print(f"\n  === 信心等級統計 ===")
        print(f"  CONFIDENT (range < 1):  {confident}")
        print(f"  UNCERTAIN (range >= 1): {uncertain}")
        print(f"  總計: {len(r_results)}\n")

        _save_excel(r_results, excel_output, validation_data)

        print(f"  [OK] 結果已儲存: {excel_output}")
        print(f"    Sheet 'Summary':       ID + Confidence_Level")
        print(f"    Sheet 'R_Full_Output':  完整 R 計算數據")
        if validation_data:
            print(f"    Sheet 'Analysis':      與驗證數據比對")
        return True

    finally:
        Path(r_script_file).unlink(missing_ok=True)


# ============================================================================
#  主程式入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='IPSSM Pipeline - 一鍵完成資料驗證 + R 風險計算',
        epilog='支援 CSV 和 Excel 輸入，自動修復異常空格格式。'
    )
    parser.add_argument('input_file', help='輸入檔案（CSV 或 Excel）')
    parser.add_argument('-v', '--validation', help='手動驗證結果 Excel 檔案（比對用）')
    parser.add_argument('--rscript', help='Rscript 執行路徑')
    parser.add_argument('--screen-only', action='store_true', help='僅執行資料驗證，不執行 R 計算')
    parser.add_argument('--translate-only', action='store_true', help='僅執行 R 計算（輸入須為已清理的 CSV）')
    parser.add_argument('--inspect', action='store_true',
                         help='唯讀掃描來源檔案標題並顯示欄位對應預覽，不執行驗證/計算、不寫任何檔案。'
                              '適合用於不確定欄名是否能自動對應的「不乾淨」來源檔案。')

    args = parser.parse_args()
    input_path = Path(args.input_file)

    if not input_path.exists():
        print(f"ERROR: 輸入檔案不存在: {input_path}")
        sys.exit(1)

    if args.inspect:
        print_inspect_report(inspect_columns(str(input_path)))
        sys.exit(0)

    validation_path = Path(args.validation) if args.validation else None

    print(f"\n{'#'*60}")
    print(f"  IPSSM Pipeline")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")

    if args.translate_only:
        success = run_translation(str(input_path), args.rscript, validation_path)
        print(f"\n{'='*60}")
        print(f"  完成！{'[OK] 成功' if success else '[ERROR] 失敗'}")
        print(f"{'='*60}")
        sys.exit(0 if success else 1)

    cleaned_csv = input_path.parent / f"{input_path.stem}_cleaned.csv"
    log_path = input_path.parent / f"{input_path.stem}_screening_log.txt"
    screen_ok = run_screening(input_path, cleaned_csv, log_path)

    if not screen_ok:
        print("\n  [ERROR] 驗證後沒有任何有效資料列可供計算，請檢查日誌。")
        sys.exit(1)

    if args.screen_only:
        print(f"\n{'='*60}")
        print(f"  完成！（僅驗證模式）")
        print(f"{'='*60}")
        sys.exit(0)

    translate_ok = run_translation(str(cleaned_csv), args.rscript, validation_path)

    print(f"\n{'='*60}")
    print(f"  Pipeline 完成！{'[OK] 全部成功' if translate_ok else '[ERROR] R 計算失敗'}")
    print(f"{'='*60}")
    sys.exit(0 if translate_ok else 1)


if __name__ == '__main__':
    main()
