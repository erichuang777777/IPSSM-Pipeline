"""Screener 驗證測試 — 重點在防止「單列錯誤丟失其他病患」的回歸。"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_pipeline import STANDARD_COLUMNS, run_screening, clean_dataframe

import pandas as pd


def _make_row(pid, **overrides):
    """產生一筆預設有效的病患資料 (dict of str)。"""
    row = {col: 'NA' for col in STANDARD_COLUMNS}
    row['ID'] = pid
    row['HB'] = '10'
    row['PLT'] = '100'
    row['BM_BLAST'] = '5'
    # 所有二元基因欄位預設為 0
    for col in STANDARD_COLUMNS:
        if col not in ('ID', 'HB', 'PLT', 'BM_BLAST', 'CYTO_IPSSR', 'TP53maxvaf'):
            row[col] = '0'
    row['CYTO_IPSSR'] = 'Good'
    row.update(overrides)
    return row


def _write_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=STANDARD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def test_error_row_does_not_drop_valid_rows(tmp_path):
    """回歸測試: 一筆壞列不得導致其後正常病患被丟棄 (缺陷 #1)。"""
    rows = [
        _make_row('good1'),
        _make_row('bad1', del5q='5'),   # 非法二元值 -> error -> 應被 SKIP
        _make_row('good2'),
        _make_row('good3'),
    ]
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "cleaned.csv"
    log = tmp_path / "log.txt"
    _write_csv(in_csv, rows)

    ok = run_screening(str(in_csv), str(out_csv), str(log))
    assert ok is True

    out = pd.read_csv(out_csv, dtype=str)
    ids = set(out['ID'])
    assert {'good1', 'good2', 'good3'} <= ids, f"正常病患被丟棄了! 只剩 {ids}"
    assert 'bad1' not in ids
    assert len(out) == 3


def test_missing_required_field_is_skipped(tmp_path):
    rows = [_make_row('good1'), _make_row('nohb', HB='NA')]
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "cleaned.csv"
    log = tmp_path / "log.txt"
    _write_csv(in_csv, rows)

    ok = run_screening(str(in_csv), str(out_csv), str(log))
    assert ok is True
    out = pd.read_csv(out_csv, dtype=str)
    assert list(out['ID']) == ['good1']


def test_nd_in_cyto_becomes_na(tmp_path):
    rows = [_make_row('p1', CYTO_IPSSR='ND')]
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "cleaned.csv"
    log = tmp_path / "log.txt"
    _write_csv(in_csv, rows)

    run_screening(str(in_csv), str(out_csv), str(log))
    out = pd.read_csv(out_csv, dtype=str, keep_default_na=False)
    assert out.loc[0, 'CYTO_IPSSR'] == 'NA'


def test_all_rows_invalid_returns_false(tmp_path):
    rows = [_make_row('nohb', HB='NA'), _make_row('noplt', PLT='NA')]
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "cleaned.csv"
    log = tmp_path / "log.txt"
    _write_csv(in_csv, rows)

    ok = run_screening(str(in_csv), str(out_csv), str(log))
    assert ok is False  # 0 有效輸出列 -> False


def test_clean_dataframe_reusable():
    df = pd.DataFrame([
        _make_row('a'),
        _make_row('b', HB='NA'),   # 缺必填 -> skip
    ])
    cleaned, report = clean_dataframe(df, collapse_tp53=True)
    assert report.output_rows == 1
    assert len(report.skipped_patients) == 1
    assert list(cleaned.columns) == STANDARD_COLUMNS


def test_clean_dataframe_collapses_tp53():
    df = pd.DataFrame([_make_row('a', TP53mut='2')])
    cleaned, _ = clean_dataframe(df, collapse_tp53=True)
    assert cleaned.loc[0, 'TP53mut'] == '2 or more'
