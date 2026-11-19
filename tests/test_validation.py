"""Screener 驗證測試 — 重點在防止「單列錯誤丟失其他病患」的回歸。"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_pipeline import (
    STANDARD_COLUMNS, run_screening, missing_model_fields, missing_groups,
)

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


def test_missing_required_field_is_skipped_in_skip_mode(tmp_path):
    rows = [_make_row('good1'), _make_row('nohb', HB='NA')]
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "cleaned.csv"
    log = tmp_path / "log.txt"
    _write_csv(in_csv, rows)

    ok = run_screening(str(in_csv), str(out_csv), str(log), missing_mode='skip')
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

    ok = run_screening(str(in_csv), str(out_csv), str(log), missing_mode='skip')
    assert ok is False  # 0 有效輸出列 -> False


# ---------------------------------------------------------------------------
#  缺失值模式 (skip / impute)
# ---------------------------------------------------------------------------

def _screen(tmp_path, rows, mode):
    in_csv = tmp_path / "in.csv"
    out_csv = tmp_path / "cleaned.csv"
    log = tmp_path / "log.txt"
    _write_csv(in_csv, rows)
    ok = run_screening(str(in_csv), str(out_csv), str(log), missing_mode=mode)
    out = pd.read_csv(out_csv, dtype=str, keep_default_na=False) if out_csv.exists() else None
    return ok, out, log.read_text(encoding='utf-8')


def test_skip_mode_skips_any_missing_model_field(tmp_path):
    rows = [
        _make_row('complete'),
        _make_row('gene_na', ASXL1='NA'),        # 基因缺失
        _make_row('cyto_na', CYTO_IPSSR='NA'),   # 細胞遺傳學缺失
        _make_row('hb_blank', HB=''),            # 臨床缺失 (空白)
    ]
    ok, out, log = _screen(tmp_path, rows, 'skip')
    assert ok is True
    assert list(out['ID']) == ['complete']
    # 每位被跳過的病患只記錄一次 SKIP，且寫明缺哪些欄位
    assert log.count('ID=gene_na') == 1
    assert 'ASXL1' in log and 'CYTO_IPSSR' in log and 'HB' in log


def test_impute_mode_keeps_patients_with_missing_values(tmp_path):
    rows = [
        _make_row('complete'),
        _make_row('gene_na', ASXL1='NA'),
        _make_row('cyto_blank', CYTO_IPSSR=''),
        _make_row('hb_na', HB='NA'),             # 臨床欄位也交給 R 情境分析
    ]
    ok, out, log = _screen(tmp_path, rows, 'impute')
    assert ok is True
    assert list(out['ID']) == ['complete', 'gene_na', 'cyto_blank', 'hb_na']
    # 缺失值保留為 NA，而不是被填成 0
    assert out.loc[out['ID'] == 'gene_na', 'ASXL1'].iloc[0] == 'NA'
    assert out.loc[out['ID'] == 'cyto_blank', 'CYTO_IPSSR'].iloc[0] == 'NA'
    assert out.loc[out['ID'] == 'hb_na', 'HB'].iloc[0] == 'NA'
    assert '[IMPUTE] ID=gene_na: ASXL1' in log
    assert '[IMPUTE] ID=complete' not in log


def test_blank_and_na_are_equivalent(tmp_path):
    _, out_blank, _ = _screen(tmp_path, [_make_row('p', ASXL1='')], 'impute')
    _, out_na, _ = _screen(tmp_path, [_make_row('p', ASXL1='NA')], 'impute')
    assert out_blank.loc[0, 'ASXL1'] == out_na.loc[0, 'ASXL1'] == 'NA'


def test_invalid_missing_mode_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        _screen(tmp_path, [_make_row('p')], 'mean')


def test_tp53mut_2_is_normalized_for_r_package(tmp_path):
    """回歸: R 套件只接受 '2 or more'，傳 '2' 會讓整批計算失敗。"""
    rows = [_make_row('p1', TP53mut='2', TP53maxvaf='0.3'),
            _make_row('p2', TP53mut='>1', TP53maxvaf='0.3')]
    ok, out, _ = _screen(tmp_path, rows, 'impute')
    assert ok is True
    assert list(out['TP53mut']) == ['2 or more', '2 or more']


def test_missing_model_fields_tp53_rules():
    base = _make_row('p')
    assert missing_model_fields(base) == []                     # TP53maxvaf=NA 但 TP53mut=0 -> 不算缺失
    assert missing_model_fields({**base, 'TP53mut': '2 or more'}) == []
    # TP53mut=1 且無法判定 LOH -> 缺 TP53loh (及 maxvaf)
    got = missing_model_fields({**base, 'TP53mut': '1', 'TP53loh': 'NA', 'TP53maxvaf': 'NA'})
    assert got == ['TP53loh', 'TP53maxvaf']
    # TP53mut=1 但 maxvaf > 0.55 -> 可推論 LOH，不算缺失
    assert missing_model_fields({**base, 'TP53mut': '1', 'TP53loh': 'NA', 'TP53maxvaf': '0.8'}) == []
    # TP53mut=1 但 del17_17p=1 -> 可推論 LOH
    assert missing_model_fields({**base, 'TP53mut': '1', 'TP53loh': 'NA',
                                 'TP53maxvaf': 'NA', 'del17_17p': '1'}) == []
    # TP53mut 本身缺失
    assert missing_model_fields({**base, 'TP53mut': 'NA'}) == ['TP53mut']


def test_missing_groups():
    assert missing_groups([]) == 'NONE'
    assert missing_groups(['HB', 'ASXL1']) == 'clinical;genes'
    assert missing_groups(['CYTO_IPSSR', 'TP53loh', 'GNB1']) == 'cytogenetic;tp53;genes'
