"""Cohort 轉換測試 — 重點在防止核型解析欄位被切片丟棄 (缺陷 #2)。"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_pipeline import (
    COLUMN_ALIASES, find_column_mapping, detect_cohort_type, run_screening,
    _normalize_header, inspect_columns,
)

import pandas as pd


def _write_fjuh_csv(path, karyotypes):
    """寫出一個會被判定為 FJUH 的 CSV (含 ethnicity/diagnosis/karyotype)。"""
    fieldnames = ['Patient_ID', 'Hemoglobin', 'Platelet', 'Blast',
                  'karyotype', 'ethnicity', 'diagnosis']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, kar in enumerate(karyotypes):
            writer.writerow({
                'Patient_ID': f'p{i}', 'Hemoglobin': '10', 'Platelet': '120',
                'Blast': '4', 'karyotype': kar, 'ethnicity': 'Asian',
                'diagnosis': 'MDS',
            })


def test_detect_fjuh():
    df = pd.DataFrame(columns=['ID', 'karyotype', 'ethnicity', 'diagnosis'])
    assert detect_cohort_type(df) == 'FJUH'


def test_new_aliases_present():
    for key in ('complex', 'del17_17p', 'TP53maxvaf', 'SF3B1'):
        assert key in COLUMN_ALIASES


def test_find_column_mapping_del17p_alias():
    df = pd.DataFrame(columns=['del(17p)', 'complex'])
    mapping = find_column_mapping(df)
    assert mapping.get('del(17p)') == 'del17_17p'
    assert mapping.get('complex') == 'complex'


def test_karyotype_complex_and_del17p_survive(tmp_path):
    """轉換後 complex / del17_17p 必須帶出核型解析的結果，而非恆為 NA。"""
    in_csv = tmp_path / "fjuh.csv"
    out_csv = tmp_path / "cleaned.csv"
    log = tmp_path / "log.txt"
    _write_fjuh_csv(in_csv, [
        "46,XY,-17",                       # del17p=1, complex=0
        "47,XX,+8,del(5q),del(7q)",        # complex=1
        "46,XY",                            # 正常
    ])

    ok = run_screening(str(in_csv), str(out_csv), str(log))
    assert ok is True

    out = pd.read_csv(out_csv, dtype=str, keep_default_na=False).set_index('ID')
    # p0: -17 -> del17_17p 應為 1 (舊版會被切片成 NA)
    assert out.loc['p0', 'del17_17p'] == '1'
    # p1: 複雜核型 -> complex 應為 1
    assert out.loc['p1', 'complex'] == '1'
    assert out.loc['p1', 'del5q'] == '1'


# ---------------------------------------------------------------------------
# 正規化比對 (_normalize_header) — 括號單位/底線/大小寫變體
# ---------------------------------------------------------------------------

def test_normalize_header_strips_unit_parens():
    assert _normalize_header("Hemoglobin (g/dL)") == "HEMOGLOBIN"
    assert _normalize_header("BM Blast (%)") == "BMBLAST"
    assert _normalize_header("Platelet_Count") == _normalize_header("platelet_count")


def test_normalize_header_keeps_karyotype_paren_content():
    """del(5q)/del(7)/del(17p) 的括號內容是欄位語意的一部分，不可被當單位濾掉。"""
    assert _normalize_header("del(5q)") == "DEL5Q"
    assert _normalize_header("del(7)") == "DEL7"
    assert _normalize_header("del(17p)") == "DEL17P"


def test_no_normalization_collision_across_deletion_columns():
    """回歸測試: 正規化不能讓 del5q/del7_7q/del17_17p 的別名互相碰撞。"""
    seen = {}
    for std_col in ('del5q', 'del7_7q', 'del17_17p'):
        for alias in COLUMN_ALIASES[std_col]:
            n = _normalize_header(alias)
            assert seen.get(n, std_col) == std_col, (
                f"'{alias}' normalizes to '{n}' which collides between "
                f"{seen.get(n)} and {std_col}"
            )
            seen[n] = std_col


def test_find_column_mapping_matches_unit_annotated_headers():
    df = pd.DataFrame(columns=["Hemoglobin (g/dL)", "Platelet_count", "BM Blast (%)"])
    mapping = find_column_mapping(df)
    assert mapping == {
        "Hemoglobin (g/dL)": "HB",
        "Platelet_count": "PLT",
        "BM Blast (%)": "BM_BLAST",
    }


# ---------------------------------------------------------------------------
# 回歸測試: 欄位對應不再只在偵測到已知 cohort 類型時才嘗試 (缺陷: UNKNOWN 時
# 完全跳過對應，導致自訂醫院格式的必填欄位恆為 NA、病患被靜默跳過)
# ---------------------------------------------------------------------------

def _write_messy_unknown_cohort_csv(path):
    """無 ethnicity/diagnosis/karyotype/transplant 特徵欄，欄名帶單位/底線變體。"""
    fieldnames = ["Patient_ID", "Hemoglobin (g/dL)", "Platelet_count",
                  "BM Blast (%)", "del(5q)", "Extra_Metadata_Col"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "Patient_ID": "m1", "Hemoglobin (g/dL)": "9.6", "Platelet_count": "281",
            "BM Blast (%)": "9", "del(5q)": "1", "Extra_Metadata_Col": "whatever",
        })


def test_unknown_cohort_type_still_gets_column_mapping(tmp_path):
    in_csv = tmp_path / "messy.csv"
    out_csv = tmp_path / "cleaned.csv"
    log = tmp_path / "log.txt"
    _write_messy_unknown_cohort_csv(in_csv)

    df = pd.read_csv(in_csv, dtype=str, keep_default_na=False)
    assert detect_cohort_type(df) == "UNKNOWN"

    ok = run_screening(str(in_csv), str(out_csv), str(log))
    assert ok is True

    out = pd.read_csv(out_csv, dtype=str, keep_default_na=False).set_index("ID")
    assert out.loc["m1", "HB"] == "9.6"
    assert out.loc["m1", "PLT"] == "281"
    assert out.loc["m1", "BM_BLAST"] == "9"
    assert out.loc["m1", "del5q"] == "1"


# ---------------------------------------------------------------------------
# inspect_columns() — 唯讀欄位掃描預覽
# ---------------------------------------------------------------------------

def test_inspect_columns_reports_matched_missing_and_unmapped(tmp_path):
    in_csv = tmp_path / "messy.csv"
    _write_messy_unknown_cohort_csv(in_csv)

    result = inspect_columns(str(in_csv))
    assert result["cohort_type"] == "UNKNOWN"
    assert result["matched"]["Hemoglobin (g/dL)"] == "HB"
    assert result["matched"]["Platelet_count"] == "PLT"
    assert result["matched"]["BM Blast (%)"] == "BM_BLAST"
    assert not result["missing_required"]  # HB/PLT/BM_BLAST 都應確定匹配
    assert "Extra_Metadata_Col" in result["unmapped_input_columns"]


def test_inspect_columns_flags_missing_required_fields(tmp_path):
    in_csv = tmp_path / "incomplete.csv"
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ID", "SomeUnrelatedColumn"])
        writer.writeheader()
        writer.writerow({"ID": "x1", "SomeUnrelatedColumn": "val"})

    result = inspect_columns(str(in_csv))
    assert set(result["missing_required"]) == {"HB", "PLT", "BM_BLAST"}


def test_inspect_columns_warns_on_hb_unit_mismatch(tmp_path):
    in_csv = tmp_path / "hb_units.csv"
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ID", "HB", "PLT", "BM_BLAST"])
        writer.writeheader()
        # HB in g/L-like magnitude (should trigger the unit sanity warning)
        writer.writerow({"ID": "x1", "HB": "96", "PLT": "281", "BM_BLAST": "9"})
        writer.writerow({"ID": "x2", "HB": "150", "PLT": "23", "BM_BLAST": "16"})

    result = inspect_columns(str(in_csv))
    assert any("UNIT CHECK" in w for w in result["warnings"])
