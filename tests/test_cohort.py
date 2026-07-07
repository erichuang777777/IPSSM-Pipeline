"""Cohort 轉換測試 — 重點在防止核型解析欄位被切片丟棄 (缺陷 #2)。"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_pipeline import (
    COLUMN_ALIASES, find_column_mapping, detect_cohort_type, run_screening,
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
