"""跨欄位一致性檢查 — 只警告，不排除病患、不修改資料。"""
import os
import subprocess
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_checks import CONSISTENCY_RULES, check_consistency, flag_ids
from ipssm_pipeline import build_missing_check, screen_input, run_review_pipeline, STANDARD_COLUMNS
from test_validation import _make_row, _write_csv

CLI = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    'ipssm_pipeline.py')]


def ids(row):
    return [f['Rule'] for f in check_consistency(row)]


def test_clean_patient_has_no_flags():
    assert check_consistency(_make_row('p')) == []
    assert flag_ids([]) == 'NONE'


@pytest.mark.parametrize('overrides,expected', [
    # TP53 (對應 ipssm 套件 IPSSMprocess)
    ({'TP53mut': '0', 'TP53maxvaf': '0.8'}, ['C01']),
    ({'TP53mut': '1', 'TP53maxvaf': '0', 'TP53loh': '0'}, ['C02']),
    ({'TP53mut': '2 or more', 'TP53maxvaf': '0'}, ['C02']),
    ({'TP53mut': '0', 'TP53loh': '1'}, ['C03']),
    ({'TP53mut': '1', 'TP53loh': '0', 'TP53maxvaf': '0.7'}, ['C04']),
    ({'TP53mut': '1', 'TP53loh': '0', 'TP53maxvaf': '0.3', 'del17_17p': '1'}, ['C04']),
    # 細胞遺傳學 (IPSS-R 分組定義)
    ({'complex': '1', 'CYTO_IPSSR': 'Good'}, ['C05']),
    ({'complex': '1', 'CYTO_IPSSR': 'Intermediate'}, ['C05']),
    ({'complex': '0', 'CYTO_IPSSR': 'Very Poor'}, ['C06']),
    ({'del7_7q': '1', 'CYTO_IPSSR': 'Good'}, ['C07']),
    ({'del7_7q': '1', 'CYTO_IPSSR': 'Very Good'}, ['C07']),
    ({'del5q': '1', 'CYTO_IPSSR': 'Very Good'}, ['C08']),
    # blast
    ({'BM_BLAST': '25'}, ['C09']),
])
def test_rules_fire(overrides, expected):
    row = _make_row('p', **overrides)
    if 'TP53maxvaf' not in overrides:
        row['TP53maxvaf'] = 'NA'
    assert ids(row) == expected


@pytest.mark.parametrize('overrides', [
    {'TP53mut': '0', 'TP53maxvaf': '0'},                                   # VAF 為 0 與無突變一致
    {'TP53mut': '1', 'TP53maxvaf': '0.3', 'TP53loh': '0'},                 # 低 VAF 且無 del17p: 合理
    {'TP53mut': '1', 'TP53maxvaf': '0.7', 'TP53loh': '1'},                 # 已是 LOH=1，無覆寫
    {'complex': '1', 'CYTO_IPSSR': 'Poor'},
    {'complex': '1', 'CYTO_IPSSR': 'Very Poor'},
    {'complex': '0', 'CYTO_IPSSR': 'Poor'},                                # Poor 可由 -7 等造成，不判矛盾
    {'del7_7q': '1', 'CYTO_IPSSR': 'Intermediate'},
    {'del7_7q': '1', 'CYTO_IPSSR': 'Poor'},
    {'del5q': '1', 'CYTO_IPSSR': 'Good'},
    {'del17_17p': '1', 'TP53mut': '0'},                                    # del17p 但 TP53 野生型: 臨床上可能
    {'BM_BLAST': '20'},                                                    # 邊界: 20 不觸發
])
def test_consistent_data_does_not_fire(overrides):
    row = _make_row('p', **overrides)
    if 'TP53maxvaf' not in overrides:
        row['TP53maxvaf'] = 'NA'
    assert ids(row) == []


def test_missing_values_never_trigger_flags():
    """缺失由另一套機制處理；任一相關欄位缺失時不應產生一致性旗標。"""
    row = _make_row('p', complex='1', CYTO_IPSSR='NA', TP53mut='NA', TP53maxvaf='0.9',
                    del7_7q='NA', del5q='NA', BM_BLAST='NA')
    assert check_consistency(row) == []
    row = _make_row('p', TP53mut='0', TP53maxvaf='')
    assert check_consistency(row) == []


def test_severity_levels_match_rule_table():
    row = _make_row('p', TP53mut='0', TP53maxvaf='0.8', BM_BLAST='25')
    sev = {f['Rule']: f['Severity'] for f in check_consistency(row)}
    assert sev == {'C01': 'conflict', 'C09': 'note'}
    assert all(CONSISTENCY_RULES[r][0] == s for r, s in sev.items())


def test_multiple_flags_and_messages():
    row = _make_row('p', complex='1', CYTO_IPSSR='Good', TP53mut='0', TP53maxvaf='0.4')
    flags = check_consistency(row)
    assert [f['Rule'] for f in flags] == ['C01', 'C05']
    assert flag_ids(flags) == 'C01;C05'
    assert 'Good' in flags[1]['Message'] and 'complex' in flags[1]['Fields']


# --- 整合: 只警告，不排除、不修改 ----------------------------------------------------

def _cohort():
    return [
        _make_row('ok'),
        _make_row('vaf_conflict', TP53mut='0', TP53maxvaf='0.8', TP53loh='1'),
        _make_row('cyto_conflict', complex='1', CYTO_IPSSR='Good'),
    ]


def test_screening_warns_but_keeps_patients_and_data(tmp_path):
    in_csv = tmp_path / 'in.csv'
    _write_csv(in_csv, _cohort())
    rows, report = screen_input(str(in_csv), 'impute')
    assert [r['ID'] for r in rows] == ['ok', 'vaf_conflict', 'cyto_conflict']        # 一個都沒被排除
    assert next(r for r in rows if r['ID'] == 'vaf_conflict')['TP53maxvaf'] == '0.8'  # 資料未被修改
    assert report.excluded == []
    got = {(c['ID'], c['Rule']) for c in report.consistency}
    assert got == {('vaf_conflict', 'C01'), ('vaf_conflict', 'C03'), ('cyto_conflict', 'C05')}
    assert all(c['Row'] in (3, 4) for c in report.consistency)
    text = report.report()
    assert 'CONSISTENCY CHECKS' in text and 'warnings only' in text and 'ID=vaf_conflict' in text


def test_build_missing_check_has_flags_column():
    check = build_missing_check([_make_row('a'), _make_row('b', complex='1', CYTO_IPSSR='Good')], None)
    assert [m['Consistency_Flags'] for m in check] == ['NONE', 'C05']


def test_review_workbook_has_consistency_sheet(tmp_path):
    in_csv = tmp_path / 'cohort.csv'
    _write_csv(in_csv, _cohort())
    outcome = run_review_pipeline(str(in_csv), 'impute')
    xls = pd.ExcelFile(outcome['paths']['review'])
    assert 'Consistency' in xls.sheet_names
    cons = xls.parse('Consistency')
    body = cons[cons['ID'].isin(['vaf_conflict', 'cyto_conflict'])]
    assert set(body['Rule']) == {'C01', 'C03', 'C05'}
    assert set(body.loc[body['Rule'] == 'C03', 'Severity']) == {'note'}
    assert 'C09' in set(cons['Rule'])                       # 規則說明也列在同一頁
    summary = dict(xls.parse('Summary').values)
    assert '2 項矛盾 / 2 位病患' in summary['跨欄位矛盾 (只警告，資料未更動)']
    mc = xls.parse('Missing_Check', keep_default_na=False).set_index('ID')
    assert mc.loc['cyto_conflict', 'Consistency_Flags'] == 'C05'
    assert mc.loc['ok', 'Consistency_Flags'] == 'NONE'
    # 標準欄位資料沒被動過，病患全數保留
    std = pd.read_csv(outcome['paths']['standardized'], dtype=str, keep_default_na=False)
    assert list(std['ID']) == ['ok', 'vaf_conflict', 'cyto_conflict'] and list(std.columns) == STANDARD_COLUMNS


def test_results_workbook_recomputes_flags_from_confirmed_data(tmp_path):
    """核對後若醫師修正了資料，正式計算的旗標要反映修正後的資料。"""
    in_csv = tmp_path / 'cohort.csv'
    _write_csv(in_csv, _cohort())
    first = subprocess.run(CLI + [str(in_csv), '--missing', 'impute'], capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr

    std_csv = tmp_path / 'cohort_standardized.csv'
    df = pd.read_csv(std_csv, dtype=str, keep_default_na=False)
    df.loc[df['ID'] == 'cyto_conflict', 'CYTO_IPSSR'] = 'Poor'          # 醫師修正矛盾
    df.to_csv(std_csv, index=False)

    second = subprocess.run(CLI + [str(std_csv), '--calc-only'], capture_output=True, text=True)
    assert second.returncode == 0, second.stdout + second.stderr
    assert '有跨欄位一致性旗標' in second.stdout
    res = pd.read_excel(tmp_path / 'cohort_standardized_results.xlsx', sheet_name='Summary',
                        keep_default_na=False).set_index('ID')
    assert res.loc['cyto_conflict', 'Consistency_Flags'] == 'NONE'       # 已修正
    assert res.loc['vaf_conflict', 'Consistency_Flags'] == 'C01;C03'     # 未修正，仍提醒
    assert res.loc['ok', 'Consistency_Flags'] == 'NONE'
