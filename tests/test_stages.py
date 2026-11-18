"""Stage 1 正規化規則 / 逐欄位稽核 / 缺失檢核 (純 Python，不需 R)。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_pipeline import (
    STANDARD_COLUMNS, STRONG_MISSING_FIELDS, _normalize_cell, build_missing_check, screen_input,
    run_screening,
)
from test_validation import _make_row, _write_csv


# --- ND = Not Detected (有檢測、沒突變) --------------------------------------------

@pytest.mark.parametrize('col', ['ASXL1', 'FLT3', 'MLL_PTD', 'del5q', 'del17_17p', 'complex',
                                 'TP53loh', 'SF3B1', 'TP53mut'])
@pytest.mark.parametrize('nd', ['ND', 'nd', 'Not Detected', 'n.d.'])
def test_nd_means_tested_negative_for_binary_and_tp53mut(col, nd):
    r = _normalize_cell(col, nd)
    assert r['value'] == '0' and r['status'] == 'auto_applied'


@pytest.mark.parametrize('col', ['HB', 'PLT', 'BM_BLAST', 'TP53maxvaf', 'CYTO_IPSSR'])
def test_nd_in_numeric_or_category_fields_is_missing(col):
    r = _normalize_cell(col, 'ND')
    assert r['value'] == 'NA' and r['rule'] == 'nd_to_na'


# --- 關鍵字 / 格式規則 -------------------------------------------------------------

@pytest.mark.parametrize('col,raw,value,status', [
    ('ASXL1', 'Positive', '1', 'auto_applied'),
    ('ASXL1', 'neg', '0', 'auto_applied'),
    ('ASXL1', 'wild-type', '0', 'auto_applied'),
    ('ASXL1', '1.0', '1', 'auto_applied'),
    ('ASXL1', '+', '1', 'needs_review'),
    ('ASXL1', '-', '0', 'needs_review'),
    ('ASXL1', '', 'NA', 'auto_applied'),
    ('ASXL1', 'N/A', 'NA', 'auto_applied'),
    ('CYTO_IPSSR', 'very_good', 'Very Good', 'auto_applied'),
    ('CYTO_IPSSR', 'POOR', 'Poor', 'auto_applied'),
    ('TP53mut', '2', '2 or more', 'auto_applied'),
    ('TP53mut', '>1', '2 or more', 'auto_applied'),
    ('HB', '9.6 g/dL', '9.6', 'auto_applied'),
    ('HB', '96 g/L', '9.6', 'needs_review'),
    ('HB', '9,6', '9.6', 'needs_review'),
    ('PLT', '281 K/uL', '281', 'auto_applied'),
    ('BM_BLAST', '9%', '9', 'auto_applied'),
    ('TP53maxvaf', '85%', '0.85', 'needs_review'),
    ('TP53maxvaf', '85', '0.85', 'needs_review'),
])
def test_keyword_rules(col, raw, value, status):
    r = _normalize_cell(col, raw)
    assert (r['value'], r['status']) == (value, status)


@pytest.mark.parametrize('col,raw', [('ASXL1', 'maybe-ish'), ('CYTO_IPSSR', 'Fair'),
                                     ('TP53mut', 'several'), ('HB', 'abc'), ('PLT', '12 apples')])
def test_unparseable_cells_are_unresolved(col, raw):
    r = _normalize_cell(col, raw)
    assert r['status'] == 'unresolved' and r['value'] == raw


def test_unchanged_values_produce_no_audit_noise():
    for col, raw in [('ASXL1', '0'), ('ASXL1', '1'), ('ASXL1', 'NA'), ('HB', '9.6'),
                     ('TP53maxvaf', '0.4'), ('CYTO_IPSSR', 'Good'), ('TP53mut', '1')]:
        assert _normalize_cell(col, raw)['status'] == 'unchanged'


def test_non_model_columns_pass_through():
    assert _normalize_cell('ID', ' p1 ')['value'] == 'p1'
    assert _normalize_cell('Whatever', 'ND')['status'] == 'unchanged'


# --- 逐欄位稽核紀錄 ----------------------------------------------------------------

def test_field_audit_records_every_change(tmp_path):
    rows = [_make_row('p1', ASXL1='ND', RUNX1='Positive', HB='96 g/L', KRAS='???', CYTO_IPSSR='')]
    in_csv = tmp_path / 'in.csv'
    _write_csv(in_csv, rows)
    out_rows, report = screen_input(str(in_csv), 'impute')
    audit = {e['Column']: e for e in report.cell_audit}

    assert audit['ASXL1']['Original'] == 'ND' and audit['ASXL1']['Normalized'] == '0'
    assert audit['ASXL1']['Rule'] == 'nd_not_detected_to_0'
    assert audit['RUNX1']['Rule'] == 'binary_keyword' and audit['RUNX1']['Status'] == 'auto_applied'
    assert audit['HB']['Status'] == 'needs_review' and audit['HB']['Normalized'] == '9.6'
    assert audit['KRAS']['Status'] == 'unresolved'
    assert audit['CYTO_IPSSR']['Rule'] == 'na_alias'
    # 每筆紀錄都帶有定位資訊
    for e in report.cell_audit:
        assert e['ID'] == 'p1' and e['Row'] == 2 and e['Source'] == 'rule'
    # 無法解析的儲存格 -> 該病患被排除，並留下原因
    assert out_rows == [] and report.excluded[0]['Source'] == 'validation'


def test_audit_csv_written(tmp_path):
    rows = [_make_row('p1', ASXL1='ND'), _make_row('p2')]
    in_csv, out_csv = tmp_path / 'in.csv', tmp_path / 'std.csv'
    audit_csv, log = tmp_path / 'audit.csv', tmp_path / 'log.txt'
    _write_csv(in_csv, rows)
    assert run_screening(str(in_csv), str(out_csv), str(log), audit_path=str(audit_csv))
    text = audit_csv.read_text(encoding='utf-8-sig')
    assert text.splitlines()[0] == 'Row,ID,Column,Original,Normalized,Rule,Source,Status,Confidence,Note'
    assert 'nd_not_detected_to_0' in text


def test_nd_gene_is_not_counted_as_missing(tmp_path):
    """ND 是「有檢測」，轉成 0 後不算缺失，不應讓病患被標為補值。"""
    in_csv = tmp_path / 'in.csv'
    _write_csv(in_csv, [_make_row('p1', ASXL1='ND', TP53mut='ND')])
    rows, report = screen_input(str(in_csv), 'skip')
    assert [r['ID'] for r in rows] == ['p1']
    assert rows[0]['ASXL1'] == '0' and rows[0]['TP53mut'] == '0'
    assert report.skipped_patients == []


def test_tp53loh_is_validated_as_binary(tmp_path):
    in_csv = tmp_path / 'in.csv'
    _write_csv(in_csv, [_make_row('p1', TP53loh='2')])
    rows, report = screen_input(str(in_csv), 'impute')
    assert rows == [] and any('TP53loh' in e for e in report.errors)


# --- 缺失檢核 (不需 R) --------------------------------------------------------------

def test_build_missing_check_without_trial():
    rows = [_make_row('a'), _make_row('b', HB='NA', ASXL1='NA')]
    check = build_missing_check(rows, None)
    assert check[0]['Missing_Fields'] == 'NONE'
    assert check[1]['Missing_Fields'] == 'HB, ASXL1'
    assert check[1]['Missing_Groups'] == 'clinical;genes'
    assert check[1]['Key_Missing_Fields'] == 'HB'
    assert check[1]['Confidence_Level'] == 'NOT_COMPUTED'


def test_strong_missing_fields_are_model_fields():
    assert STRONG_MISSING_FIELDS <= set(STANDARD_COLUMNS)
