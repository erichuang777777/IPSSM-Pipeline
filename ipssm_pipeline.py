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

from ipssm_checks import CONSISTENCY_RULES, check_consistency, flag_ids
from ipssm_source import (
    META_COLUMNS, META_ROW, META_SHEET, SHEET_JOINS, SourceSelectionError,
    describe_source, load_table,
)


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

# 缺失值處理模式:
#   skip   - 任何「模型輸入欄位」缺失的病患一律跳過，只計算資料完整者
#   impute - 保留 NA，由官方 R 套件做 Best/Mean/Worst 情境分析
#            (Mean 情境 = 以族群平均值/平均突變頻率補值)
#   auto   - 先全部試算，缺失造成的風險範圍 (Worst-Best) 不超過 max_range 者補平均值
#            計算；超過者列入 UNCERTAIN 清單並排除 (需要 R)
MISSING_MODES = ('skip', 'impute', 'auto')
DEFAULT_MISSING_MODE = 'impute'
DEFAULT_MAX_RANGE = 1.0

# 缺失欄位分組 (用於輸出 Missing_Groups)
CLINICAL_FIELDS = ['HB', 'PLT', 'BM_BLAST']
CYTOGENETIC_FIELDS = ['del5q', 'del7_7q', 'del17_17p', 'complex', 'CYTO_IPSSR']
GENE_FIELDS = [
    'MLL_PTD', 'FLT3', 'ASXL1', 'CBL', 'DNMT3A', 'ETV6', 'EZH2', 'IDH2', 'KRAS',
    'NPM1', 'NRAS', 'RUNX1', 'SF3B1', 'SRSF2', 'U2AF1',
    # 殘餘基因 (R 套件以 Nres2 一般化公式處理缺失)
    'BCOR', 'BCORL1', 'CEBPA', 'ETNK1', 'GATA2', 'GNB1', 'IDH1', 'NF1',
    'PHF6', 'PPM1D', 'PRPF8', 'PTPN11', 'SETBP1', 'STAG2', 'WT1',
]
# TP53mut 一律需要; TP53maxvaf / TP53loh 只有在無法判定 TP53 multi-hit 時才算缺失
TP53_FIELDS = ['TP53mut', 'TP53maxvaf', 'TP53loh']

# TP53mut 的各種寫法統一為 R 套件唯一接受的 '2 or more'
TP53_MULTI_ALIASES = {'2', '>1', '2+', '2 or more'}

# 單一欄位缺失就足以讓 Worst-Best 範圍超過 1 (=> 必為 UNCERTAIN) 的欄位。
# 依 ipssm 套件公式 |係數|*|worst-best|/ln2 計算並由 tests/test_r_integration.py 以 R 驗證。
# 其餘欄位 (PLT、各基因…) 單獨缺失不會超過 1，但多個加總後可能超過。
STRONG_MISSING_FIELDS = {
    'HB', 'BM_BLAST', 'CYTO_IPSSR', 'TP53mut', 'TP53loh', 'TP53maxvaf', 'FLT3', 'MLL_PTD',
}

# ---- 正規化規則 (Stage 1) --------------------------------------------------
# ND = Not Detected: 有檢測、未偵測到突變 => 二元/TP53mut 欄位視為 0。
# 其他型態 (數值、CYTO_IPSSR) 無法對應成值，只能視為 NA。
ND_ALIASES = {'nd', 'n.d.', 'n.d', 'not detected', 'not detect', 'notdetected'}

# 二元欄位的文字寫法對照 (high = 可自動套用; medium = 套用但標記「需人工確認」)
BINARY_TRUE_HIGH = {'1', 'yes', 'true', 'pos', 'positive', 'mutated', 'mut', 'mutant'}
BINARY_FALSE_HIGH = {'0', 'no', 'false', 'neg', 'negative', 'wt', 'wildtype', 'wild type',
                     'wild-type', 'not mutated', 'unmutated', 'absent'}
BINARY_TRUE_MEDIUM = {'+', 'present', 'detected', 'y'}
BINARY_FALSE_MEDIUM = {'-', '--', 'not present', 'n'}

CYTO_CATEGORIES = ['Very Good', 'Good', 'Intermediate', 'Poor', 'Very Poor']
CYTO_LOOKUP = {c.lower(): c for c in CYTO_CATEGORIES}

NUMERIC_FIELDS = {'HB', 'PLT', 'BM_BLAST', 'TP53maxvaf'}
# 各數值欄位可接受、可直接剝除的單位字串 (小寫)
NUMERIC_UNITS_IGNORED = {
    'HB': {'g/dl', 'gdl'},
    'PLT': {'k/ul', 'k/µl', 'x10^9/l', '10^9/l', 'x10e9/l', 'giga/l', 'g/l', '/nl', '10*9/l'},
    'BM_BLAST': {'%'},
    'TP53maxvaf': set(),
}


def _field_kind(col):
    """欄位的正規化型態: binary / cyto / tp53mut / number / None (非模型欄位)。"""
    if col in BINARY_FIELDS:
        return 'binary'
    if col == 'CYTO_IPSSR':
        return 'cyto'
    if col == 'TP53mut':
        return 'tp53mut'
    if col in NUMERIC_FIELDS:
        return 'number'
    return None

# 注意: 'ND' (not detected) 不在此集合 — 它代表「有檢測、未偵測到」，
# 依欄位型態有不同的正規化 (見 _normalize_cell)，不是單純的缺失。
NA_STRINGS = {
    '', ' ', 'NA', 'N/A', 'n/a', 'na', 'NaN', 'nan', 'None', 'none', '.'
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
    'del5q', 'del7_7q', 'complex', 'del17_17p', 'TP53loh', 'MLL_PTD', 'FLT3', 'ASXL1',
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
# 呼叫官方 R 套件 ipssm (papaemmelab/ipssm)。
# 輸入 CSV 中的缺失值 (空白 / NA) 一律視為 NA，套件會同時計算 Best / Mean / Worst
# 三種情境: Best = 缺失視為最有利、Worst = 缺失視為最不利、
# Mean = 以族群平均值 (連續變數) / 平均突變頻率 (基因) 補值。
# 缺失欄位清單與補值模式相關資訊由 Python 端 (ipssm_pipeline.py) 另外加上。
# 套件位置: 沿用 R 預設的 library 路徑 (或環境變數 R_LIBS_USER)。

args <- commandArgs(trailingOnly=TRUE)
input_csv  <- args[1]
output_csv <- args[2]

suppressPackageStartupMessages({
  library(ipssm, warn.conflicts=FALSE, verbose=FALSE)
})

results <- IPSSMwrapper(input_csv)

# Range = Worst - Best; 與套件 IPSSMannotate 的門檻一致 (<= 1 視為可信)。
# 先 round 到小數 2 位，避免浮點誤差使剛好等於 1 的情況被判錯。
results$Range_Score <- round(results$IPSSMscore_worst - results$IPSSMscore_best, 2)
results$Confidence_Level <- ifelse(results$Range_Score <= 1, "CONFIDENT", "UNCERTAIN")

write.csv(results, file=output_csv, row.names=FALSE, na="NA")

cat("\n[OK] Results generated with Best/Mean/Worst scenarios and confidence indicators\n")
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


def _load_source(input_path, sheet=None, sheet_join=None):
    """
    讀取原始檔案 (Excel 多分頁 / CSV) 為單一 DataFrame (全字串，含來源位置欄)。
    分頁自動選擇、表頭偵測、多分頁合併等規則見 ipssm_source.py。
    回傳 (df, info)；無法明確決定來源時拋出 SourceSelectionError。
    """
    return load_table(input_path, find_column_mapping, sheet=sheet, sheet_join=sheet_join)


def _data_columns(df):
    return [c for c in df.columns if c not in META_COLUMNS]


def inspect_columns(input_path, sheet=None, sheet_join=None):
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
      source:                 來源檔案資訊 (各分頁評分、選用的分頁、表頭列、合併方式、警告)
    """
    df, source = _load_source(input_path, sheet, sheet_join)
    df = df[_data_columns(df)]

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
        'source': source,
    }


def print_inspect_report(result):
    """將 inspect_columns() 的結果印成人類可讀的預覽報告。"""
    print(f"\n{'='*60}")
    print(f"  欄位掃描預覽 (--inspect，唯讀，不寫入任何輸出檔)")
    print(f"{'='*60}")
    for line in describe_source(result['source']):
        print(f"  {line}")
    for w in result['source']['warnings']:
        print(f"  [SOURCE] {w}")
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

AUDIT_COLUMNS = ['Row', 'ID', 'Column', 'Original', 'Normalized', 'Rule', 'Source',
                 'Status', 'Confidence', 'Note']


class ValidationReport:
    """收集驗證過程中的錯誤、警告、逐欄位正規化紀錄、排除與補值資訊"""

    def __init__(self):
        self.errors = []
        self.warnings = []
        self.cell_audit = []         # 逐儲存格正規化紀錄 (dict, 欄位見 AUDIT_COLUMNS)
        self.skipped_patients = []
        self.excluded = []           # 結構化排除清單: ID / Row / Source / Reason
        self.imputed_patients = []   # (patient_id, [missing fields]) — 交給 R 補值者
        self.llm_results = []        # LLMResult 清單 (若啟用 LLM)
        self.consistency = []        # 跨欄位一致性旗標 (只警告，不排除、不改資料)
        self.source_info = None      # ipssm_source.load_table 的 info (分頁、表頭、合併)
        self.source_warnings = []    # 來源檔案層級的警告 (未選用的資料分頁、重複 ID、缺整欄…)
        self.missing_mode = DEFAULT_MISSING_MODE
        self.input_rows = 0
        self.output_rows = 0
        self.input_cols = 0
        self.output_cols = 0
        self.format_conversions = []

    def add_error(self, row, col, msg):
        self.errors.append(f"Row {row}, {col}: {msg}")

    def add_warning(self, row, col, msg):
        self.warnings.append(f"Row {row}, {col}: {msg}")

    def add_audit(self, row, patient_id, column, original, normalized, rule,
                  source='rule', status='auto_applied', confidence='', note=''):
        self.cell_audit.append({
            'Row': row, 'ID': patient_id, 'Column': column, 'Original': original,
            'Normalized': normalized, 'Rule': rule, 'Source': source,
            'Status': status, 'Confidence': confidence, 'Note': note,
        })

    def add_conversion(self, source_col, target_col):
        self.format_conversions.append(f"{source_col} -> {target_col}")

    def skip_patient(self, patient_id, reason, kind='validation', row=None):
        """kind: validation (資料錯誤) / missing (skip 模式因缺失) / uncertain (auto 模式範圍過大)"""
        self.skipped_patients.append(f"ID={patient_id}: {reason}")
        self.excluded.append({'ID': patient_id, 'Row': row, 'Source': kind, 'Reason': reason})

    def add_imputed(self, patient_id, fields):
        self.imputed_patients.append((patient_id, list(fields)))

    def add_consistency(self, row, patient_id, flags):
        for f in flags:
            self.consistency.append({'Row': row, 'ID': patient_id, **f})

    def audit_counts(self):
        """依 (Rule, Status) 統計稽核紀錄筆數。"""
        counts = {}
        for entry in self.cell_audit:
            key = (entry['Rule'], entry['Status'])
            counts[key] = counts.get(key, 0) + 1
        return counts

    def report(self):
        n_review = sum(1 for e in self.cell_audit if e['Status'] == 'needs_review')
        n_unres = sum(1 for e in self.cell_audit if e['Status'] == 'unresolved')
        lines = [
            f"\nSummary: {len(self.errors)} ERRORS, {len(self.warnings)} WARNINGS, "
            f"{len(self.cell_audit)} NORMALIZED CELLS ({n_review} need review, "
            f"{n_unres} unresolved), {len(self.skipped_patients)} SKIPPED\n"
        ]
        if self.source_info:
            lines.append("\n--- SOURCE FILE ---")
            for line in describe_source(self.source_info):
                lines.append(f"  {line}")
            for w in self.source_warnings:
                lines.append(f"  [SOURCE WARNING] {w}")
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
            lines.append(f"  [INFO] Missing-value mode: {self.missing_mode}")
            lines.append(f"  [INFO] Skipped: {len(self.skipped_patients)} patients")
            if self.missing_mode in ('impute', 'auto'):
                lines.append(f"  [INFO] With missing values (mean-scenario imputation by R): "
                             f"{len(self.imputed_patients)} patients")
        if self.skipped_patients:
            lines.append(f"\n--- SKIPPED PATIENTS ({len(self.skipped_patients)}) ---")
            for skip in self.skipped_patients[:20]:
                lines.append(f"  [SKIP] {skip}")
            if len(self.skipped_patients) > 20:
                lines.append(f"  ... and {len(self.skipped_patients) - 20} more")
        if self.imputed_patients:
            lines.append(f"\n--- PATIENTS WITH MISSING VALUES / IMPUTED ({len(self.imputed_patients)}) ---")
            for pid, fields in self.imputed_patients[:20]:
                lines.append(f"  [IMPUTE] ID={pid}: {', '.join(fields)}")
            if len(self.imputed_patients) > 20:
                lines.append(f"  ... and {len(self.imputed_patients) - 20} more")
        if self.cell_audit:
            lines.append(f"\n--- FIELD-LEVEL NORMALIZATION ({len(self.cell_audit)} cells) ---")
            for (rule, status), n in sorted(self.audit_counts().items()):
                lines.append(f"  {rule:<28} {status:<13} x{n}")
            flagged = [e for e in self.cell_audit if e['Status'] in ('needs_review', 'unresolved')]
            for e in flagged[:30]:
                lines.append(f"  [{e['Status'].upper()}] Row {e['Row']}, {e['Column']}: "
                             f"'{e['Original']}' -> '{e['Normalized']}' ({e['Rule']}, {e['Source']})")
            if len(flagged) > 30:
                lines.append(f"  ... and {len(flagged) - 30} more (see field audit file)")
        if self.consistency:
            n_conf = sum(1 for c in self.consistency if c['Severity'] == 'conflict')
            lines.append(f"\n--- CONSISTENCY CHECKS ({n_conf} conflicts, "
                         f"{len(self.consistency) - n_conf} notes; warnings only, data unchanged) ---")
            ordered = sorted(self.consistency, key=lambda c: c['Severity'] != 'conflict')
            for c in ordered[:30]:
                lines.append(f"  [{c['Severity'].upper()}] {c['Rule']} ID={c['ID']} (Row {c['Row']}): {c['Message']}")
            if len(ordered) > 30:
                lines.append(f"  ... and {len(ordered) - 30} more (see review workbook, Consistency sheet)")
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
        elif self.output_rows > 0:
            lines.append(f"\n>>> PASS WITH ISSUES: {self.output_rows} patients passed; "
                         f"{len(self.errors)} validation error(s) caused those patients to be excluded "
                         f"(see SKIPPED PATIENTS).")
        else:
            lines.append(f"\n>>> FAIL: {len(self.errors)} validation errors found.")
        return '\n'.join(lines) + '\n'


def _try_convert_cohort(input_path, report, sheet=None, sheet_join=None):
    """
    嘗試自動偵測並轉換輸入檔案的隊列格式。

    流程:
      1. 偵測隊列類型 (FJUH / HSCT / UNKNOWN)
      2. 自動對應欄位名稱到 IPSSM 標準欄位
      3. 若有 karyotype 欄位，自動解析為細胞遺傳學欄位
      4. 補齊缺失欄位為 'NA'

    回傳 (rows, fieldnames, converted: bool)；每列含來源位置欄 (META_SHEET / META_ROW)。
    來源檔案無法明確決定 (例如多個分頁都像資料) 時拋出 SourceSelectionError。
    """
    df, info = _load_source(input_path, sheet, sheet_join)
    report.source_info = info
    report.source_warnings.extend(info['warnings'])
    try:
        data_cols = _data_columns(df)
        cohort_type = detect_cohort_type(df[data_cols])
        mapping = find_column_mapping(df[data_cols])
        is_standard = len(mapping) == len(data_cols) and set(mapping.values()) == set(data_cols)

        absent = [c for c in ['ID', 'HB', 'PLT', 'BM_BLAST']
                  if c not in set(mapping.values())]
        for col in absent:
            if col == 'ID':
                report.source_warnings.append("來源檔案找不到病患 ID 欄位，病患將以檔案位置 (Row_...) 識別。")
            else:
                report.source_warnings.append(
                    f"來源檔案找不到 {col} 欄位 (整欄缺失)：所有病患的 {col} 都會是缺失值。"
                    f"若欄名不同，請用 --inspect 確認對應。")

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
            for col in data_cols:
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
        return df.to_dict('records'), list(df.columns), False


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


def _is_na_value(value):
    """判斷單一儲存格是否為缺失值 (含欄位根本不存在)。"""
    if value is None:
        return True
    text = str(value).strip()
    return text in NA_STRINGS or text == 'NA'


def missing_model_fields(row):
    """
    回傳該病患「模型輸入欄位」中缺失的欄位名稱清單 (保持固定順序)。

    - 臨床/細胞遺傳學/基因欄位: 為 NA 即缺失。
    - TP53mut: 為 NA 即缺失。
    - TP53maxvaf / TP53loh: 僅在 TP53mut == '1' 且無法判定 multi-hit
      (TP53loh 非 0/1、maxvaf 也沒超過 0.55、也沒有 del17_17p) 時才算缺失；
      TP53mut 為 0 或 '2 or more' 時兩者對結果沒有影響，不算缺失。
      (邏輯對應 R 套件 IPSSMprocess 的 TP53multi 推導)
    - SF3B1_5q / SF3B1_alpha / IPSS_M_ 由 R 套件自行推導，不列入。
    """
    missing = [f for f in CLINICAL_FIELDS + CYTOGENETIC_FIELDS
               if _is_na_value(row.get(f))]

    tp53mut = row.get('TP53mut')
    if _is_na_value(tp53mut):
        missing.append('TP53mut')
    elif str(tp53mut).strip() == '1':
        loh = row.get('TP53loh')
        loh_known = not _is_na_value(loh) and str(loh).strip() in ('0', '1')
        maxvaf = row.get('TP53maxvaf')
        try:
            vaf_high = not _is_na_value(maxvaf) and float(maxvaf) > 0.55
        except ValueError:
            vaf_high = False
        del17p = str(row.get('del17_17p', '')).strip() == '1'
        if not (loh_known or vaf_high or del17p):
            missing.append('TP53loh')
            if _is_na_value(maxvaf):
                missing.append('TP53maxvaf')

    missing += [f for f in GENE_FIELDS if _is_na_value(row.get(f))]
    return missing


def missing_groups(fields):
    """將缺失欄位歸類為 clinical / cytogenetic / tp53 / genes，回傳以 ; 連接的字串。"""
    groups = []
    if any(f in CLINICAL_FIELDS for f in fields):
        groups.append('clinical')
    if any(f in CYTOGENETIC_FIELDS for f in fields):
        groups.append('cytogenetic')
    if any(f in TP53_FIELDS for f in fields):
        groups.append('tp53')
    if any(f in GENE_FIELDS for f in fields):
        groups.append('genes')
    return ';'.join(groups) if groups else 'NONE'


# ---- Stage 1: 逐儲存格正規化 ------------------------------------------------

def _fmt_number(x):
    """數值轉字串 (去除多餘的尾數)。"""
    return format(float(x), '.10g')


def _cell(value, rule='', status='unchanged', confidence='', note=''):
    return {'value': value, 'rule': rule, 'status': status,
            'confidence': confidence, 'note': note}


def _normalize_number(col, text):
    """數值欄位: 回傳 _cell(...) 或 None (無法解析)。"""
    m = re.fullmatch(r'\s*(-?\d+(?:[.,]\d+)?)\s*([^\d\s].*)?', text)
    if not m:
        return None
    num_text, unit = m.group(1), (m.group(2) or '').strip().lower()
    comma = ',' in num_text
    try:
        num = float(num_text.replace(',', '.'))
    except ValueError:
        return None

    rule, status, conf, note = '', 'unchanged', '', ''
    if unit:
        if unit in NUMERIC_UNITS_IGNORED.get(col, set()):
            rule, status, conf = 'strip_unit', 'auto_applied', 'high'
        elif col == 'HB' and unit == 'g/l':
            num /= 10
            rule, status, conf = 'unit_g_per_l_to_g_per_dl', 'needs_review', 'medium'
            note = 'HB must be g/dL; value divided by 10'
        elif col == 'TP53maxvaf' and unit == '%':
            num /= 100
            rule, status, conf = 'percent_to_fraction', 'needs_review', 'medium'
            note = 'TP53maxvaf must be a fraction in 0-1'
        else:
            return None
    elif comma:
        rule, status, conf = 'decimal_comma', 'needs_review', 'medium'

    if col == 'TP53maxvaf' and not rule and 1 < num <= 100:
        num /= 100
        rule, status, conf = 'vaf_percent_assumed', 'needs_review', 'medium'
        note = 'value >1 assumed to be a percent; converted to fraction'

    if not rule:
        return _cell(text)
    return _cell(_fmt_number(num), rule, status, conf, note)


def _normalize_cell(col, raw):
    """
    以關鍵字/格式規則正規化單一儲存格。

    回傳 dict: value / rule / status / confidence / note
      status: unchanged | auto_applied | needs_review | unresolved
    非模型欄位 (kind=None) 原樣回傳。unresolved = 規則無法解析 (可交給 LLM)。
    """
    kind = _field_kind(col)
    text = '' if raw is None else str(raw).strip()
    if kind is None:
        return _cell(text)

    if text in NA_STRINGS or text == 'NA':
        if text == 'NA':
            return _cell('NA')
        return _cell('NA', 'na_alias', 'auto_applied', 'high')

    # ND = Not Detected (有檢測、未偵測到) — 依欄位型態處理
    if text.lower() in ND_ALIASES:
        if kind in ('binary', 'tp53mut'):
            return _cell('0', 'nd_not_detected_to_0', 'auto_applied', 'high',
                         'ND = tested, not detected -> 0')
        return _cell('NA', 'nd_to_na', 'auto_applied', 'high',
                     'ND has no numeric/category value -> missing')

    low = re.sub(r'[\s_]+', ' ', text.lower()).strip()

    if kind == 'binary':
        if text in ('0', '1'):
            return _cell(text)
        try:
            f = float(text)
            if f in (0.0, 1.0):
                return _cell(str(int(f)), 'numeric_binary', 'auto_applied', 'high')
        except ValueError:
            pass
        for words, val, conf in ((BINARY_TRUE_HIGH, '1', 'high'), (BINARY_FALSE_HIGH, '0', 'high'),
                                 (BINARY_TRUE_MEDIUM, '1', 'medium'),
                                 (BINARY_FALSE_MEDIUM, '0', 'medium')):
            if low in words:
                return _cell(val, 'binary_keyword', 'auto_applied' if conf == 'high'
                             else 'needs_review', conf)
        return _cell(text, 'unparsed', 'unresolved')

    if kind == 'cyto':
        key = re.sub(r'[\s_\-]+', ' ', low).strip()
        canonical = CYTO_LOOKUP.get(key)
        if canonical is None:
            return _cell(text, 'unparsed', 'unresolved')
        if canonical == text:
            return _cell(text)
        return _cell(canonical, 'cyto_case_normalized', 'auto_applied', 'high')

    if kind == 'tp53mut':
        if text in ('0', '1', '2 or more'):
            return _cell(text)
        if text in TP53_MULTI_ALIASES:
            return _cell('2 or more', 'tp53mut_multi_alias', 'auto_applied', 'high',
                         "R package only accepts '2 or more' for >=2 TP53 mutations")
        try:
            f = float(text)
            if f in (0.0, 1.0):
                return _cell(str(int(f)), 'numeric_binary', 'auto_applied', 'high')
            if f == 2.0:
                return _cell('2 or more', 'tp53mut_multi_alias', 'auto_applied', 'high')
        except ValueError:
            pass
        if low in BINARY_FALSE_HIGH:
            return _cell('0', 'binary_keyword', 'auto_applied', 'high')
        return _cell(text, 'unparsed', 'unresolved')

    # number
    parsed = _normalize_number(col, text)
    if parsed is None:
        return _cell(text, 'unparsed', 'unresolved')
    return parsed


def _llm_spec(col):
    """LLM 查詢用的 (kind, 允許值)。"""
    kind = _field_kind(col)
    if kind == 'binary':
        return 'binary', ['0', '1']
    if kind == 'cyto':
        return 'cyto', list(CYTO_CATEGORIES)
    if kind == 'tp53mut':
        return 'tp53mut', ['0', '1', '2 or more']
    return 'number', []


def _normalize_row(row_idx, row, report, llm_resolver=None):
    """
    正規化一列中的所有模型欄位 (就地修改 row)，並把每個「有變動或無法解析」的
    儲存格寫入 report.cell_audit。規則無法解析者，若有 llm_resolver 則交給 LLM，
    採用與否依其信心程度；一律標為 needs_review。
    """
    patient_id = row.get('ID', f'Row{row_idx}')
    for col in list(row.keys()):
        if _field_kind(col) is None:
            continue
        raw = row[col]
        result = _normalize_cell(col, raw)

        if result['status'] == 'unresolved' and llm_resolver is not None:
            kind, allowed = _llm_spec(col)
            llm = llm_resolver.resolve(col, str(raw).strip(), kind, allowed)
            if llm.accepted:
                value = llm.value
                if value != 'NA' and kind == 'number':
                    value = _fmt_number(value)
                result = _cell(value, 'llm_interpretation', 'needs_review',
                               f"{llm.confidence:.2f}", llm.reason)
                result['source'] = 'llm'
            else:
                note = llm.error or 'LLM could not interpret the text'
                result = _cell(result['value'], 'unparsed', 'unresolved', '', note)
                result['source'] = 'llm_rejected'

        row[col] = result['value']
        if result['status'] != 'unchanged':
            report.add_audit(row_idx, patient_id, col, str(raw).strip(), result['value'],
                             result['rule'], result.get('source', 'rule'), result['status'],
                             result['confidence'], result['note'])


def _validate_row(row_idx, row, report, missing_mode=DEFAULT_MISSING_MODE):
    """
    驗證單一資料列 (假設已經過 _normalize_row)，回傳 True=有效 / False=跳過。

    驗證步驟:
      1. 缺失值處理 (skip 模式: 任何模型欄位缺失即跳過)
      2. 數值範圍檢查
      3. 二元欄位 (0/1) 檢查
      4. 分類欄位 (CYTO_IPSSR, TP53mut) 檢查
    """
    patient_id = row.get('ID', f'Row{row_idx}')
    # 記錄進入本列前的錯誤數，讓有效性判斷只依「本列」是否新增錯誤，
    # 避免前面某列的錯誤導致其後所有正常病患被誤判為無效而遭丟棄。
    errors_before = len(report.errors)

    # 殘留的 NA 類字串一律標準化 (非模型欄位也一併處理，不記稽核)
    for col, value in row.items():
        if isinstance(value, str) and value in NA_STRINGS:
            row[col] = 'NA'

    # skip 模式: 任何模型輸入欄位缺失的病患一律跳過。
    # impute / auto 模式保留 NA，交給 R 套件做 Best/Mean/Worst 情境分析
    # (通過驗證後由 screen_input 記錄到 report.imputed_patients)。
    if missing_mode == 'skip':
        missing = missing_model_fields(row)
        if missing:
            report.skip_patient(
                patient_id, f"Missing value(s) [skip mode]: {', '.join(missing)}",
                kind='missing', row=row_idx)
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
    if cyto not in ['NA'] + CYTO_CATEGORIES:
        report.add_error(row_idx, 'CYTO_IPSSR', f"Invalid category: {cyto}")

    tp53 = row.get('TP53mut', 'NA')
    if isinstance(tp53, str):
        tp53 = tp53.strip()
    if tp53 not in ('NA', '0', '1', '2 or more'):
        report.add_error(row_idx, 'TP53mut', f"Invalid value: {tp53}")

    # 只依本列是否新增錯誤來判定有效性 (見上方 errors_before)。
    return len(report.errors) == errors_before


def _row_label(row, index, is_excel):
    """來源位置: Excel 為 '分頁!列號'，CSV 為檔案列號 (int)。"""
    sheet_name = row.pop(META_SHEET, None)
    raw = row.pop(META_ROW, None)
    try:
        number = int(float(raw))
    except (TypeError, ValueError):
        number = index + 2
    return f"{sheet_name}!{number}" if is_excel and sheet_name else number


def screen_input(input_path, missing_mode=DEFAULT_MISSING_MODE, llm_resolver=None,
                 sheet=None, sheet_join=None):
    """
    Stage 1 核心: 讀檔 -> 欄位對應/核型解析 -> 逐儲存格正規化 (含選用的 LLM) -> 驗證。

    回傳 (rows, report)
      rows:   通過驗證的病患，每列為 STANDARD_COLUMNS 42 欄 (缺失以 'NA' 表示)
      report: ValidationReport (含逐儲存格稽核、排除清單、補值清單、LLM 結果)
    """
    if missing_mode not in MISSING_MODES:
        raise ValueError(f"missing_mode must be one of {MISSING_MODES}, got {missing_mode!r}")

    report = ValidationReport()
    report.missing_mode = missing_mode

    rows, fieldnames, converted = _try_convert_cohort(input_path, report, sheet, sheet_join)
    if converted:
        print("  [OK] Successfully converted to standard IPSSM format\n")

    is_excel = (report.source_info or {}).get('file_type') in ('xlsx', 'xlsm')
    report.input_cols = len([f for f in fieldnames if f not in META_COLUMNS])
    report.input_rows = len(rows)
    rows = _convert_fjuh_format(rows, fieldnames, report)

    valid_rows = []
    for index, row in enumerate(rows):
        cleaned = {
            (k.strip() if isinstance(k, str) else k): (v.strip() if isinstance(v, str) else v)
            for k, v in row.items()
        }
        row_idx = _row_label(cleaned, index, is_excel)
        if _is_na_value(cleaned.get('ID')):
            new_id = f"Row_{row_idx}"
            report.add_audit(row_idx, new_id, 'ID', str(cleaned.get('ID', '')), new_id,
                             'id_from_row', status='auto_applied', confidence='high',
                             note='missing patient ID replaced by source position')
            cleaned['ID'] = new_id
        _normalize_row(row_idx, cleaned, report, llm_resolver)

        errors_before = len(report.errors)
        skipped_before = len(report.skipped_patients)
        if _validate_row(row_idx, cleaned, report, missing_mode):
            valid_rows.append(cleaned)
            report.add_consistency(row_idx, cleaned.get('ID', f'Row{row_idx}'),
                                   check_consistency(cleaned))
            still_missing = missing_model_fields(cleaned)
            if still_missing:
                report.add_imputed(cleaned.get('ID', f'Row{row_idx}'), still_missing)
        elif len(report.skipped_patients) == skipped_before:
            # 有錯誤的列不進 R 計算，但明確記錄為 SKIP(而非靜默丟棄)，
            # 且不影響其他正常病患。(已由 _validate_row 記錄 SKIP 者不重複記錄)
            new_errors = report.errors[errors_before:]
            reason = new_errors[0] if new_errors else "Validation error"
            report.skip_patient(cleaned.get('ID', f'Row{row_idx}'), reason,
                                kind='validation', row=row_idx)

    final_rows = [{col: row.get(col, 'NA') for col in STANDARD_COLUMNS} for row in valid_rows]
    id_counts = {}
    for row in final_rows:
        id_counts[row['ID']] = id_counts.get(row['ID'], 0) + 1
    dups = [pid for pid, n in id_counts.items() if n > 1]
    if dups:
        report.source_warnings.append(
            f"重複的病患 ID ({len(dups)} 個，結果中會出現同名的多筆): " + ', '.join(dups[:10])
            + (' ...' if len(dups) > 10 else ''))
    report.output_rows = len(final_rows)
    report.output_cols = len(STANDARD_COLUMNS)
    if llm_resolver is not None:
        report.llm_results = list(llm_resolver.log)
    return final_rows, report


def _write_audit_csv(path, cell_audit):
    """寫出逐欄位正規化紀錄 (機器可讀 CSV)。"""
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        writer.writerows(cell_audit)


def _write_standardized_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=STANDARD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _write_screening_log(log_path, report_text):
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write("=" * 70 + "\n")
        f.write("IPSSM Screener Validation Report\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 70 + "\n")
        f.write(report_text)


def run_screening(input_path, output_path, log_path, missing_mode=DEFAULT_MISSING_MODE,
                  llm_resolver=None, audit_path=None, sheet=None, sheet_join=None):
    """
    執行資料驗證（Stage 1），回傳是否成功。

    missing_mode:
      'skip'   - 任何模型輸入欄位缺失的病患一律跳過
      'impute' - 保留 NA，交給 R 套件做 Best/Mean/Worst 情境分析 (預設)
      'auto'   - 此階段同 impute；範圍過大者由 Stage 2 試算後排除
    audit_path: 若給定，另存逐欄位正規化紀錄 CSV。
    """
    if missing_mode not in MISSING_MODES:
        raise ValueError(f"missing_mode must be one of {MISSING_MODES}, got {missing_mode!r}")

    print(f"\n{'='*60}")
    print(f"  [階段 1] 資料正規化 & 驗證")
    print(f"{'='*60}")
    print(f"  輸入:  {input_path}")
    print(f"  輸出:  {output_path}")
    print(f"  日誌:  {log_path}")
    print(f"  缺失值模式: {missing_mode}\n")

    try:
        rows, report = screen_input(input_path, missing_mode, llm_resolver, sheet, sheet_join)
        _write_standardized_csv(output_path, rows)
        if audit_path:
            _write_audit_csv(audit_path, report.cell_audit)

        report_text = report.report()
        print(report_text)
        _write_screening_log(log_path, report_text)

        print(f"  [OK] Cleaned CSV: {output_path}")
        print(f"  [OK] 驗證日誌:    {log_path}")
        if audit_path:
            print(f"  [OK] 逐欄位紀錄:  {audit_path}")
        # 只要有任何有效輸出列即視為成功；有問題的列已被記錄為 SKIP，
        # 不再因單筆錯誤而讓整批作廢。
        return report.output_rows > 0

    except SourceSelectionError:
        raise
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


def _save_excel(r_results, output_path, validation_data=None, extra_sheets=None):
    """儲存結果到多工作表 Excel"""
    wb = Workbook()
    wb.remove(wb.active)

    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    uncertain_fill = PatternFill(start_color="FF6B6B", end_color="FF6B6B", fill_type="solid")
    uncertain_font = Font(bold=True, color="FFFFFF")

    # --- Sheet 1: Summary ---
    ws = wb.create_sheet("Summary", 0)
    ws.append(['ID', 'Reported_Score', 'Reported_Cat', 'Confidence_Level', 'Missing_Fields',
               'Consistency_Flags'])
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
    for row in r_results:
        ws.append([row.get('ID', ''), row.get('Reported_Score', 'NA'),
                   row.get('Reported_Cat', 'NA'), row.get('Confidence_Level', 'NA'),
                   row.get('Missing_Fields', ''), row.get('Consistency_Flags', '')])
        if row.get('Confidence_Level') == 'UNCERTAIN':
            for cell in ws[ws.max_row]:
                cell.fill = uncertain_fill
                cell.font = uncertain_font
    for letter, width in zip('ABCDEF', [12, 15, 16, 17, 40, 18]):
        ws.column_dimensions[letter].width = width

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
            r_cat = row.get('Reported_Cat', row.get('IPSSMcat', 'NA'))
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

    # --- 額外工作表 (例如 auto 模式排除的 UNCERTAIN 清單) ---
    for name, headers, rows in (extra_sheets or []):
        ws_x = wb.create_sheet(name)
        ws_x.append(list(headers))
        for cell in ws_x[1]:
            cell.fill = header_fill
            cell.font = header_font
        for r in rows:
            ws_x.append(list(r))
        for col in ws_x.columns:
            ws_x.column_dimensions[col[0].column_letter].width = 16

    wb.save(output_path)


def _annotate_missing_info(r_results, cleaned_csv_path):
    """
    依清理後的輸入 CSV (與 R 輸出列順序一致) 為每位病患加上:
      Missing_Fields / Missing_Groups / Used_Scenario_Analysis
      Reported_Score / Reported_Cat  (= Mean 情境的分數與風險分類，即「補平均值」的結果)
    缺失判定邏輯與 Screener 共用 (missing_model_fields)，不依賴 R。
    """
    with open(cleaned_csv_path, 'r', encoding='utf-8-sig', newline='') as f:
        input_rows = list(csv.DictReader(f))

    aligned = len(input_rows) == len(r_results)
    if not aligned:
        print(f"  [WARN] R 輸出列數 ({len(r_results)}) 與輸入列數 ({len(input_rows)}) 不一致，"
              f"無法標記缺失欄位。")

    for i, result in enumerate(r_results):
        if aligned:
            fields = missing_model_fields(input_rows[i])
            result['Missing_Fields'] = ', '.join(fields) if fields else 'NONE'
            result['Missing_Groups'] = missing_groups(fields)
            result['Key_Missing_Fields'] = (
                ', '.join(f for f in fields if f in STRONG_MISSING_FIELDS) or 'NONE')
            result['Used_Scenario_Analysis'] = 'YES' if fields else 'NO'
        else:
            result['Missing_Fields'] = result['Missing_Groups'] = 'UNKNOWN'
            result['Key_Missing_Fields'] = 'UNKNOWN'
            result['Used_Scenario_Analysis'] = 'UNKNOWN'
        result['Consistency_Flags'] = (
            flag_ids(check_consistency(input_rows[i])) if aligned else 'UNKNOWN')
        result['Reported_Score'] = result.get('IPSSMscore_mean', 'NA')
        result['Reported_Cat'] = result.get('IPSSMcat_mean', 'NA')


def _run_r_scoring(input_csv, rscript, output_csv, error_dir, quiet=False):
    """
    以官方 ipssm 套件計算 (Best/Mean/Worst + Range + Confidence)。
    成功回傳 R 輸出的 list[dict]，失敗回傳 None (並在 error_dir 寫 r_error.log)。
    """
    with tempfile.NamedTemporaryFile(mode='w', suffix='.R', delete=False) as f:
        f.write(R_SCRIPT_TEMPLATE)
        r_script_file = f.name

    try:
        cmd = [rscript, r_script_file, str(input_csv), str(output_csv)]
        r_env = os.environ.copy()
        # 只有在使用者沒設定 R_LIBS_USER、且 ~/R/library 存在時才補上，
        # 避免覆寫 RStudio / 系統預設的 library 路徑。
        user_lib = os.path.expanduser("~/R/library")
        if "R_LIBS_USER" not in r_env and os.path.isdir(user_lib):
            r_env["R_LIBS_USER"] = user_lib
        result = subprocess.run(cmd, capture_output=True, text=True, env=r_env)

        if result.stdout and not quiet:
            print(result.stdout)
        if result.returncode != 0 or not Path(output_csv).exists():
            print("  [ERROR] ERROR: R 執行失敗" if result.returncode != 0
                  else "  [ERROR] ERROR: R 輸出檔案未產生")
            if result.stderr:
                print("  [STDERR]:", result.stderr)
            with open(Path(error_dir) / "r_error.log", "w", encoding="utf-8") as f:
                f.write("[STDOUT]\n" + (result.stdout or "") + "\n\n[STDERR]\n"
                        + (result.stderr or "No stderr output recorded."))
            return None
        return _read_r_output(output_csv)
    finally:
        Path(r_script_file).unlink(missing_ok=True)


def run_translation(input_csv, rscript_path=None, validation_path=None, extra_sheets=None):
    """
    Stage 4 — 正式計算: 對「已核對的標準欄位 CSV」執行 R 計算 + 結果整合，回傳是否成功。
    extra_sheets: 額外要寫入結果活頁簿的 [(sheet 名稱, 欄位標題, 資料列)]。
    """
    print(f"\n{'='*60}")
    print(f"  [階段 4] 正式計算風險 & 信心等級標記")
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

    print("\n  執行 R IPSSMwrapper...")
    r_results = _run_r_scoring(input_path, rscript, r_output_csv, output_dir)
    if r_results is None:
        return False

    print("  處理結果中...")
    _annotate_missing_info(r_results, input_path)

    validation_data = {}
    if validation_path:
        validation_data = _read_validation_data(validation_path)

    confident = sum(1 for r in r_results if r.get('Confidence_Level') == 'CONFIDENT')
    uncertain = sum(1 for r in r_results if r.get('Confidence_Level') == 'UNCERTAIN')
    with_missing = sum(1 for r in r_results if r.get('Used_Scenario_Analysis') == 'YES')

    print(f"\n  === 信心等級統計 ===")
    print(f"  CONFIDENT (range <= 1): {confident}")
    print(f"  UNCERTAIN (range > 1):  {uncertain}")
    print(f"  含缺失值 (以 Mean 情境補值): {with_missing}")
    flagged = sum(1 for r in r_results if r.get('Consistency_Flags') not in ('NONE', 'UNKNOWN', None))
    print(f"  有跨欄位一致性旗標 (請核對 Consistency_Flags): {flagged}")
    print(f"  總計: {len(r_results)}\n")

    _save_excel(r_results, excel_output, validation_data, extra_sheets)

    print(f"  [OK] 結果已儲存: {excel_output}")
    print(f"    Sheet 'Summary':       ID + 分數/分類 + Confidence_Level + 缺失欄位")
    print(f"    Sheet 'R_Full_Output':  完整 R 計算數據")
    if validation_data:
        print(f"    Sheet 'Analysis':      與驗證數據比對")
    for name, _, rows in (extra_sheets or []):
        print(f"    Sheet '{name}': {len(rows)} 筆")
    return True


# ============================================================================
#  Stage 2 試算 / Stage 3 核對用資料 (Review)
# ============================================================================

MISSING_CHECK_COLUMNS = [
    'ID', 'Missing_Fields', 'Missing_Groups', 'Key_Missing_Fields',
    'IPSSMscore_best', 'IPSSMscore_mean', 'IPSSMscore_worst',
    'IPSSMcat_best', 'IPSSMcat_mean', 'IPSSMcat_worst',
    'Range_Score', 'Confidence_Level', 'Decision', 'Consistency_Flags',
]


def run_trial_calculation(rows, rscript, quiet=True):
    """
    Stage 2 — 試算缺失值造成的風險範圍: 對正規化後的列呼叫 R，取得
    Best/Mean/Worst 與 Range。使用暫存目錄，不會在使用者資料夾留下檔案。
    成功回傳與 rows 同序的 list[dict]，失敗回傳 None。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_csv = Path(tmp) / 'trial_input.csv'
        _write_standardized_csv(tmp_csv, rows)
        results = _run_r_scoring(tmp_csv, rscript, Path(tmp) / 'trial_output.csv',
                                 error_dir=tmp, quiet=quiet)
        if results is None:
            return None
        _annotate_missing_info(results, tmp_csv)
        return results


def _range_value(result):
    try:
        return float(result.get('Range_Score'))
    except (TypeError, ValueError):
        return float('inf')


def build_missing_check(rows, trial):
    """每位通過驗證的病患一列: 缺失欄位 + 試算結果 (若有)。Decision 由呼叫端填入。"""
    check = []
    for i, row in enumerate(rows):
        fields = missing_model_fields(row)
        entry = {c: '' for c in MISSING_CHECK_COLUMNS}
        entry.update({
            'ID': row.get('ID', ''),
            'Missing_Fields': ', '.join(fields) if fields else 'NONE',
            'Missing_Groups': missing_groups(fields),
            'Key_Missing_Fields': ', '.join(f for f in fields if f in STRONG_MISSING_FIELDS) or 'NONE',
            'Confidence_Level': 'NOT_COMPUTED',
            'Consistency_Flags': flag_ids(check_consistency(row)),
        })
        if trial is not None:
            for c in MISSING_CHECK_COLUMNS[4:12]:
                entry[c] = trial[i].get(c, '')
        check.append(entry)
    return check


def _style_sheet(ws, header_fill, header_font, widths=None):
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
    ws.freeze_panes = 'A2'
    for idx, col in enumerate(ws.columns):
        letter = col[0].column_letter
        width = (widths[idx] if widths and idx < len(widths) else 14)
        ws.column_dimensions[letter].width = width


def _save_review_workbook(path, report, included_rows, missing_check, mode, max_range,
                          trial_available, llm_info):
    """Stage 3 — 核對用活頁簿: 摘要 / 標準欄位資料 / 逐欄位紀錄 / 缺失檢核 / UNCERTAIN / 排除 / LLM。"""
    wb = Workbook()
    wb.remove(wb.active)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    review_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    bad_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

    n_review = sum(1 for e in report.cell_audit if e['Status'] == 'needs_review')
    n_unres = sum(1 for e in report.cell_audit if e['Status'] == 'unresolved')
    kinds = {}
    for ex in report.excluded:
        kinds[ex['Source']] = kinds.get(ex['Source'], 0) + 1
    n_uncertain = sum(1 for m in missing_check if m['Confidence_Level'] == 'UNCERTAIN')
    n_conflict = sum(1 for c in report.consistency if c['Severity'] == 'conflict')
    n_conflict_pts = len({c['ID'] for c in report.consistency if c['Severity'] == 'conflict'})

    # --- Summary ---
    ws = wb.create_sheet("Summary")
    ws.append(['項目', '內容'])
    src = report.source_info or {}
    src_desc = ', '.join(src.get('selected', [])) or '-'
    if src.get('join'):
        src_desc += f" (合併方式: {src['join']})"
    summary = [
        ('來源分頁', src_desc),
        ('來源檔案警告', f"{len(report.source_warnings)} 則 (見 Source 工作表)"),
        ('輸入列數', report.input_rows),
        ('缺失值模式', f"{mode}" + (f" (範圍門檻 Worst-Best <= {max_range})" if mode == 'auto' else '')),
        ('逐欄位正規化紀錄', f"{len(report.cell_audit)} 筆 (需人工確認 {n_review}、無法解析 {n_unres})"),
        ('通過驗證的病患', len(missing_check)),
        ('排除 — 資料錯誤 (validation)', kinds.get('validation', 0)),
        ('排除 — 缺失值 (skip 模式)', kinds.get('missing', 0)),
        ('排除 — 範圍過大 UNCERTAIN (auto 模式)', kinds.get('uncertain', 0)),
        ('試算結果中 UNCERTAIN (Range > 1)', n_uncertain if trial_available else '未試算 (找不到 R)'),
        ('跨欄位矛盾 (只警告，資料未更動)',
         f"{n_conflict} 項矛盾 / {n_conflict_pts} 位病患；另有 {len(report.consistency) - n_conflict} 項提示 (見 Consistency 工作表)"),
        ('進入正式計算的病患 (Standardized sheet)', len(included_rows)),
        ('LLM 輔助', llm_info or '未啟用'),
        ('下一步', '核對 Standardized / Field_Audit / Uncertain 後，確認無誤再執行正式計算 (--calc-only 或 --confirm)'),
    ]
    for item in summary:
        ws.append(list(item))
    _style_sheet(ws, header_fill, header_font, [44, 90])

    # --- Source ---
    ws = wb.create_sheet("Source")
    ws.append(['分頁', '狀態', '是否使用', '表頭列', '資料列數', '對應標準欄位數', '看起來是病患資料'])
    for sh in src.get('sheets', []):
        ws.append([sh['name'], sh['state'], 'YES' if sh['name'] in src.get('selected', []) else 'NO',
                   sh['header_row'], sh['rows'], sh['matched'], 'YES' if sh['looks_like_data'] else 'NO'])
    ws.append([])
    ws.append(['來源檔案警告'])
    for w in report.source_warnings:
        ws.append([w])
        for cell in ws[ws.max_row]:
            cell.fill = review_fill
    _style_sheet(ws, header_fill, header_font, [28, 10, 10, 8, 10, 14, 16])

    # --- Standardized ---
    ws = wb.create_sheet("Standardized")
    ws.append(STANDARD_COLUMNS)
    for row in included_rows:
        ws.append([row.get(c, 'NA') for c in STANDARD_COLUMNS])
    _style_sheet(ws, header_fill, header_font, [12] + [11] * (len(STANDARD_COLUMNS) - 1))

    # --- Field_Audit ---
    ws = wb.create_sheet("Field_Audit")
    ws.append(AUDIT_COLUMNS)
    for e in report.cell_audit:
        ws.append([e[c] for c in AUDIT_COLUMNS])
        fill = review_fill if e['Status'] == 'needs_review' else (
            bad_fill if e['Status'] == 'unresolved' else None)
        if fill:
            for cell in ws[ws.max_row]:
                cell.fill = fill
    _style_sheet(ws, header_fill, header_font, [7, 12, 14, 18, 18, 28, 12, 14, 11, 50])

    # --- Column_Summary ---
    ws = wb.create_sheet("Column_Summary")
    ws.append(['Column', 'Patients', 'Missing(NA)', 'Cells changed', 'Needs review', 'Unresolved'])
    for col in STANDARD_COLUMNS:
        if col == 'ID':
            continue
        changed = [e for e in report.cell_audit if e['Column'] == col]
        ws.append([col, len(included_rows),
                   sum(1 for r in included_rows if _is_na_value(r.get(col))),
                   len(changed),
                   sum(1 for e in changed if e['Status'] == 'needs_review'),
                   sum(1 for e in changed if e['Status'] == 'unresolved')])
    _style_sheet(ws, header_fill, header_font, [14, 10, 13, 15, 14, 12])

    # --- Missing_Check ---
    ws = wb.create_sheet("Missing_Check")
    ws.append(MISSING_CHECK_COLUMNS)
    for m in missing_check:
        ws.append([m[c] for c in MISSING_CHECK_COLUMNS])
        if m['Confidence_Level'] == 'UNCERTAIN':
            for cell in ws[ws.max_row]:
                cell.fill = bad_fill
    _style_sheet(ws, header_fill, header_font, [12, 38, 24, 22] + [12] * 6 + [11, 16, 20])

    # --- Consistency ---
    ws = wb.create_sheet("Consistency")
    ws.append(['Row', 'ID', 'Rule', 'Severity', 'Fields', 'Message'])
    for c in sorted(report.consistency, key=lambda c: c['Severity'] != 'conflict'):
        ws.append([c['Row'], c['ID'], c['Rule'], c['Severity'], c['Fields'], c['Message']])
        if c['Severity'] == 'conflict':
            for cell in ws[ws.max_row]:
                cell.fill = bad_fill
    ws.append([])
    ws.append(['規則說明 (只警告，不排除病患、不修改資料)'])
    for rule_id, (severity, text) in CONSISTENCY_RULES.items():
        ws.append(['', '', rule_id, severity, '', text])
    _style_sheet(ws, header_fill, header_font, [14, 14, 7, 10, 34, 90])

    # --- Uncertain ---
    ws = wb.create_sheet("Uncertain")
    ws.append(MISSING_CHECK_COLUMNS)
    for m in missing_check:
        if m['Confidence_Level'] == 'UNCERTAIN':
            ws.append([m[c] for c in MISSING_CHECK_COLUMNS])
    _style_sheet(ws, header_fill, header_font, [12, 38, 24, 22] + [12] * 6 + [11, 16, 20])

    # --- Excluded ---
    ws = wb.create_sheet("Excluded")
    ws.append(['ID', 'Row', 'Source', 'Reason'])
    for ex in report.excluded:
        ws.append([ex['ID'], ex['Row'], ex['Source'], ex['Reason']])
    _style_sheet(ws, header_fill, header_font, [14, 7, 13, 100])

    # --- LLM_Review ---
    if report.llm_results:
        ws = wb.create_sheet("LLM_Review")
        ws.append(['Column', 'Original text', 'LLM value', 'Confidence', 'Accepted',
                   'Reason / Error', 'Cells affected'])
        for r in report.llm_results:
            affected = sum(1 for e in report.cell_audit
                           if e['Column'] == r.column and e['Original'] == r.raw_value)
            ws.append([r.column, r.raw_value, r.value, round(r.confidence, 2),
                       'YES' if r.accepted else 'NO', r.error or r.reason, affected])
            if not r.accepted:
                for cell in ws[ws.max_row]:
                    cell.fill = bad_fill
        _style_sheet(ws, header_fill, header_font, [14, 24, 14, 11, 10, 60, 14])

    wb.save(path)


def _build_llm_info(llm_resolver):
    if llm_resolver is None:
        return None
    cfg = llm_resolver.config
    where = '遠端' if llm_resolver.is_remote else '本地'
    accepted = sum(1 for r in llm_resolver.log if r.accepted)
    return (f"{cfg.provider} / {cfg.resolved_model()} @ {cfg.resolved_endpoint()} ({where}); "
            f"查詢 {len(llm_resolver.log)} 個不同文字，採用 {accepted} 個 (信心門檻 {cfg.min_confidence})")


def run_review_pipeline(input_path, missing_mode=DEFAULT_MISSING_MODE, max_range=DEFAULT_MAX_RANGE,
                        rscript_path=None, llm_resolver=None, sheet=None, sheet_join=None):
    """
    Stage 1~3: 正規化 (逐欄位紀錄, 選用 LLM) -> 試算缺失值風險 (UNCERTAIN 清單) ->
    產生標準欄位資料與核對用活頁簿。回傳結果 dict (含各輸出路徑)，失敗回傳 None。
    正式計算 (Stage 4) 由使用者核對後另行執行 (run_translation)。
    """
    if missing_mode not in MISSING_MODES:
        raise ValueError(f"missing_mode must be one of {MISSING_MODES}, got {missing_mode!r}")
    input_path = Path(input_path)
    parent, stem = input_path.parent, input_path.stem
    paths = {
        'standardized': parent / f"{stem}_standardized.csv",
        'audit': parent / f"{stem}_field_audit.csv",
        'review': parent / f"{stem}_review.xlsx",
        'log': parent / f"{stem}_screening_log.txt",
    }

    # ---- Stage 1 ----
    print(f"\n{'='*60}")
    print(f"  [階段 1] 資料正規化 & 逐欄位紀錄")
    print(f"{'='*60}")
    print(f"  輸入: {input_path}")
    print(f"  缺失值模式: {missing_mode}"
          + (f" (Worst-Best 範圍門檻 {max_range})" if missing_mode == 'auto' else ""))
    if llm_resolver is not None:
        print(f"  LLM 輔助: {llm_resolver.config.provider} @ {llm_resolver.config.resolved_endpoint()} "
              f"({'遠端' if llm_resolver.is_remote else '本地'})")
    try:
        rows, report = screen_input(input_path, missing_mode, llm_resolver, sheet, sheet_join)
    except SourceSelectionError:
        raise
    except Exception as e:
        print(f"  [ERROR] ERROR: {e}")
        import traceback
        traceback.print_exc()
        return None
    if not rows:
        report_text = report.report()
        print(report_text)
        _write_screening_log(paths['log'], report_text)
        _write_audit_csv(paths['audit'], report.cell_audit)
        print("\n  [ERROR] 驗證後沒有任何有效資料列可供計算，請檢查日誌。")
        return None

    # ---- Stage 2 ----
    print(f"\n{'='*60}")
    print(f"  [階段 2] 試算缺失值造成的風險範圍")
    print(f"{'='*60}")
    rscript = rscript_path or _find_rscript()
    trial = None
    if rscript:
        trial = run_trial_calculation(rows, rscript)
    else:
        print("  [WARN] 找不到 Rscript，無法試算缺失值風險範圍。")
    if trial is None and missing_mode == 'auto':
        print("  [ERROR] auto 模式需要 R 試算才能判斷哪些病患範圍過大，無法繼續。")
        return None

    included_rows = rows
    if missing_mode == 'auto':
        keep = []
        for i, (row, res) in enumerate(zip(rows, trial)):
            rng = _range_value(res)
            if rng <= max_range:
                keep.append(i)
            else:
                fields = missing_model_fields(row)
                key = [f for f in fields if f in STRONG_MISSING_FIELDS]
                report.skip_patient(
                    row.get('ID', ''),
                    f"UNCERTAIN [auto mode]: Worst-Best range {rng:.2f} > {max_range}; "
                    f"missing: {', '.join(fields) or 'NONE'}"
                    + (f"; key fields: {', '.join(key)}" if key else ''),
                    kind='uncertain')
        included_rows = [rows[i] for i in keep]
        report.output_rows = len(included_rows)

    missing_check = build_missing_check(rows, trial)
    included_ids = {id(r) for r in included_rows}
    for entry, row in zip(missing_check, rows):
        entry['Decision'] = 'INCLUDE' if id(row) in included_ids else 'EXCLUDE_UNCERTAIN'

    if trial is not None:
        n_unc = sum(1 for m in missing_check if m['Confidence_Level'] == 'UNCERTAIN')
        n_missing = sum(1 for m in missing_check if m['Missing_Fields'] != 'NONE')
        print(f"  含缺失欄位的病患: {n_missing} / {len(rows)}")
        print(f"  試算 UNCERTAIN (Range > 1): {n_unc}")
        if missing_mode == 'auto':
            print(f"  auto 模式排除 (Range > {max_range}): {len(rows) - len(included_rows)}，"
                  f"進入正式計算: {len(included_rows)}")

    # ---- Stage 3 ----
    print(f"\n{'='*60}")
    print(f"  [階段 3] 產生標準欄位資料與核對檔")
    print(f"{'='*60}")
    report_text = report.report()
    print(report_text)
    _write_standardized_csv(paths['standardized'], included_rows)
    _write_audit_csv(paths['audit'], report.cell_audit)
    _write_screening_log(paths['log'], report_text)
    _save_review_workbook(paths['review'], report, included_rows, missing_check,
                          missing_mode, max_range, trial is not None,
                          _build_llm_info(llm_resolver))
    print(f"  [OK] 標準欄位資料: {paths['standardized']}")
    print(f"  [OK] 逐欄位紀錄:   {paths['audit']}")
    print(f"  [OK] 核對活頁簿:   {paths['review']}")
    print(f"  [OK] 驗證日誌:     {paths['log']}")

    uncertain_sheet = None
    if missing_mode == 'auto':
        excluded = [m for m in missing_check if m['Decision'] != 'INCLUDE']
        if excluded:
            uncertain_sheet = ('Uncertain_Excluded', MISSING_CHECK_COLUMNS,
                               [[m[c] for c in MISSING_CHECK_COLUMNS] for m in excluded])
    return {'paths': paths, 'report': report, 'rows': included_rows,
            'missing_check': missing_check, 'uncertain_sheet': uncertain_sheet}


# ============================================================================
#  主程式入口
# ============================================================================

def _build_llm_resolver(args):
    """依 CLI 參數建立 LLMResolver；未啟用回傳 None，設定錯誤時印出訊息並結束。"""
    if not args.llm:
        return None
    from ipssm_llm import LLMConfig, LLMResolver, DEFAULT_KEY_ENV
    key_env = args.llm_api_key_env or DEFAULT_KEY_ENV.get(args.llm_provider, '')
    config = LLMConfig(
        provider=args.llm_provider,
        endpoint=args.llm_endpoint or '',
        model=args.llm_model or '',
        api_key=os.environ.get(key_env, '') if key_env else '',
        min_confidence=args.llm_min_confidence,
        allow_remote=args.llm_allow_remote,
    )
    try:
        return LLMResolver(config)
    except ValueError as e:
        print(f"ERROR (LLM 設定): {e}")
        sys.exit(2)


def _add_llm_arguments(parser):
    g = parser.add_argument_group(
        'LLM 輔助檢核 (選用)',
        '只處理關鍵字規則無法解析的儲存格；僅送出「欄位名稱 + 單一儲存格原文」，不含 ID 或其他欄位。'
        '採用與否依信心程度，且一律標為「需人工確認」。')
    g.add_argument('--llm', action='store_true', help='啟用 LLM 輔助檢核')
    g.add_argument('--llm-provider', choices=('openai', 'anthropic'), default='openai',
                   help='openai = OpenAI 相容 API (本地 Ollama/LM Studio/vLLM 或雲端皆可); '
                        'anthropic = Anthropic Messages API (預設 openai)')
    g.add_argument('--llm-endpoint', help='端點 base URL，例如 http://localhost:11434/v1 '
                                          '(anthropic 預設 https://api.anthropic.com)')
    g.add_argument('--llm-model', help='模型名稱 (openai 必填；anthropic 預設 claude-haiku-5-5)')
    g.add_argument('--llm-api-key-env',
                   help='存放 API key 的環境變數名稱 (預設 OPENAI_API_KEY / ANTHROPIC_API_KEY；'
                        '本地端點可不需要)。請勿把 key 直接寫在指令列。')
    g.add_argument('--llm-min-confidence', type=float, default=0.8,
                   help='採用 LLM 結果的最低信心 (0~1，預設 0.8)')
    g.add_argument('--llm-allow-remote', action='store_true',
                   help='允許使用非本地端點 (資料會傳出本機；需確認已去識別化且符合使用條款)')


def main():
    parser = argparse.ArgumentParser(
        description='IPSSM Pipeline — 正規化 -> 試算缺失值 -> 核對 -> 正式計算風險',
        epilog='預設只做階段 1~3 並停下來讓你核對標準欄位資料；核對後加上 --confirm '
               '(或另外執行 --calc-only) 才進行正式計算。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('input_file', help='輸入檔案（CSV 或 Excel；--calc-only 時為標準欄位 CSV）')
    parser.add_argument('-v', '--validation', help='手動驗證結果 Excel 檔案（比對用）')
    parser.add_argument('--rscript', help='Rscript 執行路徑')
    parser.add_argument('--missing', choices=MISSING_MODES, default=DEFAULT_MISSING_MODE,
                        help="缺失值處理: skip = 任何模型欄位缺失的病患一律跳過; "
                             "impute = 保留 NA，由 R 套件以 Best/Mean/Worst 情境分析補平均值; "
                             "auto = 先試算，缺失造成的範圍 (Worst-Best) 超過 --max-range 者排除並列入 "
                             f"UNCERTAIN 清單，其餘補平均值計算 (預設 {DEFAULT_MISSING_MODE})")
    parser.add_argument('--max-range', type=float, default=DEFAULT_MAX_RANGE,
                        help=f'auto 模式的範圍門檻 (Worst-Best 超過此值視為 UNCERTAIN，預設 {DEFAULT_MAX_RANGE})')
    parser.add_argument('--sheet',
                        help='Excel 要讀的分頁: 名稱或序號 (1 起算)，多頁用逗號分隔 (例如 "臨床,NGS")，'
                             '或 all (所有看起來是病患資料的可見分頁)。不指定時自動選擇最像病患資料的分頁；'
                             '多頁分數相同時會要求指定')
    parser.add_argument('--sheet-join', choices=SHEET_JOINS,
                        help='多個分頁的合併方式: stack = 結構相同的分頁上下串接 (例如不同批次); '
                             'merge = 依病患 ID 合併內容不同的分頁 (例如臨床頁 + 基因頁)。'
                             '不指定時依欄位重疊程度自動判斷')
    parser.add_argument('--confirm', action='store_true',
                        help='階段 1~3 完成後，不再停下來核對，直接以標準欄位資料進行正式計算 (階段 4)')
    parser.add_argument('--calc-only', '--translate-only', dest='calc_only', action='store_true',
                        help='只執行階段 4 正式計算（輸入須為已核對的標準欄位 CSV，即 *_standardized.csv）')
    parser.add_argument('--screen-only', action='store_true',
                        help='僅執行階段 1 (正規化 + 驗證)，不試算、不產生核對活頁簿')
    parser.add_argument('--inspect', action='store_true',
                        help='唯讀掃描來源檔案標題並顯示欄位對應預覽，不執行驗證/計算、不寫任何檔案。'
                             '適合用於不確定欄名是否能自動對應的「不乾淨」來源檔案。')
    _add_llm_arguments(parser)

    args = parser.parse_args()
    input_path = Path(args.input_file)

    if not input_path.exists():
        print(f"ERROR: 輸入檔案不存在: {input_path}")
        sys.exit(1)
    if args.max_range < 0:
        print("ERROR: --max-range 不可為負數")
        sys.exit(2)

    if args.inspect:
        try:
            print_inspect_report(inspect_columns(str(input_path), args.sheet, args.sheet_join))
        except SourceSelectionError as e:
            print(f"ERROR (來源檔案): {e}")
            sys.exit(2)
        sys.exit(0)

    validation_path = Path(args.validation) if args.validation else None

    print(f"\n{'#'*60}")
    print(f"  IPSSM Pipeline")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")

    # ---- 只做階段 4 ----
    if args.calc_only:
        success = run_translation(str(input_path), args.rscript, validation_path)
        print(f"\n{'='*60}")
        print(f"  完成！{'[OK] 成功' if success else '[ERROR] 失敗'}")
        print(f"{'='*60}")
        sys.exit(0 if success else 1)

    llm_resolver = _build_llm_resolver(args)

    # ---- 只做階段 1 ----
    if args.screen_only:
        stem = input_path.parent / input_path.stem
        try:
            screen_ok = run_screening(input_path, f"{stem}_standardized.csv",
                                      f"{stem}_screening_log.txt", missing_mode=args.missing,
                                      llm_resolver=llm_resolver, audit_path=f"{stem}_field_audit.csv",
                                      sheet=args.sheet, sheet_join=args.sheet_join)
        except SourceSelectionError as e:
            print(f"\nERROR (來源檔案): {e}")
            sys.exit(2)
        if not screen_ok:
            print("\n  [ERROR] 驗證後沒有任何有效資料列可供計算，請檢查日誌。")
            sys.exit(1)
        print(f"\n{'='*60}\n  完成！（僅階段 1）\n{'='*60}")
        sys.exit(0)

    # ---- 階段 1~3 ----
    try:
        outcome = run_review_pipeline(input_path, args.missing, args.max_range, args.rscript,
                                      llm_resolver, args.sheet, args.sheet_join)
    except SourceSelectionError as e:
        print(f"\nERROR (來源檔案): {e}")
        sys.exit(2)
    if outcome is None:
        sys.exit(1)
    std_csv = outcome['paths']['standardized']

    if not args.confirm:
        print(f"\n{'='*60}")
        print(f"  階段 1~3 完成，請先核對再進行正式計算")
        print(f"{'='*60}")
        print(f"  1. 核對 {outcome['paths']['review']} (Standardized / Field_Audit / Uncertain / Excluded)")
        print(f"  2. 如需修正，直接編輯 {std_csv}")
        print(f"  3. 確認無誤後執行正式計算:")
        print(f"       python3 ipssm_pipeline.py {std_csv} --calc-only")
        print(f"     (或重新執行本指令並加上 --confirm)")
        sys.exit(0)

    # ---- 階段 4 (使用者以 --confirm 表示免核對) ----
    extra = [outcome['uncertain_sheet']] if outcome['uncertain_sheet'] else None
    translate_ok = run_translation(str(std_csv), args.rscript, validation_path, extra)

    print(f"\n{'='*60}")
    print(f"  Pipeline 完成！{'[OK] 全部成功' if translate_ok else '[ERROR] R 計算失敗'}")
    print(f"{'='*60}")
    sys.exit(0 if translate_ok else 1)


if __name__ == '__main__':
    main()
