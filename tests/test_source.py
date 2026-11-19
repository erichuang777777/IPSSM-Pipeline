"""原始檔案讀取測試 — Excel 多分頁 / 表頭不在第一列 / 隱藏分頁 / 百分比格式 / 合併 / CSV 編碼。"""
import os
import subprocess
import sys

import pandas as pd
import pytest
from openpyxl import Workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_pipeline import STANDARD_COLUMNS, inspect_columns, screen_input
from ipssm_source import SourceSelectionError

CLI = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    'ipssm_pipeline.py')]

CLIN_COLS = ['Patient_ID', 'Hemoglobin (g/dL)', 'Platelet_count', 'BM Blast (%)', 'CYTO_IPSSR',
             'del5q', 'del7_7q', 'del17_17p', 'complex', 'TP53mut', 'TP53maxvaf', 'TP53loh']
GENES = [c for c in STANDARD_COLUMNS if c not in
         ('ID', 'HB', 'PLT', 'BM_BLAST', 'CYTO_IPSSR', 'del5q', 'del7_7q', 'del17_17p',
          'complex', 'TP53mut', 'TP53maxvaf', 'TP53loh')]


def _clin(pid, hb=10, plt=120, blast=5, cyto='Good'):
    return [pid, hb, plt, blast, cyto, 0, 0, 0, 0, 0, None, 0]


def _full_row(pid, **kw):
    row = {c: 0 for c in STANDARD_COLUMNS}
    row.update(ID=pid, HB=10, PLT=120, BM_BLAST=5, CYTO_IPSSR='Good', TP53maxvaf=None)
    row.update(kw)
    return [row[c] for c in STANDARD_COLUMNS]


def _sheet(wb, title, header, rows, title_rows=(), hidden=False):
    ws = wb.create_sheet(title)
    for t in title_rows:
        ws.append(t)
    ws.append(header)
    for r in rows:
        ws.append(r)
    if hidden:
        ws.sheet_state = 'hidden'
    return ws


def _save(wb, path):
    wb.remove(wb['Sheet']) if 'Sheet' in wb.sheetnames else None
    wb.save(path)
    return str(path)


# ---------------------------------------------------------------------------

def test_auto_selects_data_sheet_after_notes_and_detects_header(tmp_path):
    wb = Workbook()
    _sheet(wb, '說明', ['本檔案為 MDS 世代資料'], [['匯出日期', '2026-01-01']])
    _sheet(wb, 'Clinical', CLIN_COLS + GENES,
           [_clin('A1') + [0] * len(GENES), _clin('A2', hb=8.5) + [0] * len(GENES)],
           title_rows=[['XX 醫院 MDS 病患清單'], ['單位: HB g/dL'], []])
    path = _save(wb, tmp_path / 'raw.xlsx')

    rows, report = screen_input(path, 'impute')
    assert [r['ID'] for r in rows] == ['A1', 'A2']
    assert rows[1]['HB'] == '8.5'
    info = report.source_info
    assert info['selected'] == ['Clinical']
    clinical = next(s for s in info['sheets'] if s['name'] == 'Clinical')
    assert clinical['header_row'] == 4                       # 上方 3 列標題/空白被略過
    assert any('第 4 列' in w for w in report.source_warnings)


def test_row_labels_point_to_sheet_and_excel_row(tmp_path):
    wb = Workbook()
    _sheet(wb, 'Data', STANDARD_COLUMNS, [_full_row('p1'), _full_row('p2', ASXL1='ND')],
           title_rows=[['title']])
    path = _save(wb, tmp_path / 'raw.xlsx')
    _, report = screen_input(path, 'impute')
    entry = next(e for e in report.cell_audit if e['Column'] == 'ASXL1')
    assert entry['Row'] == 'Data!4'                          # 標題1列 + 表頭1列 + 第2筆資料


def test_tie_between_data_sheets_requires_explicit_choice(tmp_path):
    wb = Workbook()
    _sheet(wb, '2023', STANDARD_COLUMNS, [_full_row('a')])
    _sheet(wb, '2024', STANDARD_COLUMNS, [_full_row('b')])
    path = _save(wb, tmp_path / 'raw.xlsx')
    with pytest.raises(SourceSelectionError, match='--sheet'):
        screen_input(path, 'impute')

    rows, _ = screen_input(path, 'impute', sheet='2024')
    assert [r['ID'] for r in rows] == ['b']
    rows, _ = screen_input(path, 'impute', sheet='1')          # 1-based 序號
    assert [r['ID'] for r in rows] == ['a']
    with pytest.raises(SourceSelectionError, match='可用的分頁'):
        screen_input(path, 'impute', sheet='2025')


def test_stack_batches_with_sheet_all(tmp_path):
    wb = Workbook()
    _sheet(wb, '2023', STANDARD_COLUMNS, [_full_row('a'), _full_row('b')])
    _sheet(wb, '2024', STANDARD_COLUMNS, [_full_row('c')])
    _sheet(wb, 'notes', ['comment'], [['ignore me']])
    path = _save(wb, tmp_path / 'raw.xlsx')
    rows, report = screen_input(path, 'impute', sheet='all')
    assert [r['ID'] for r in rows] == ['a', 'b', 'c']
    assert report.source_info['join'] == 'stack'
    assert report.source_info['selected'] == ['2023', '2024']


def test_merge_clinical_and_ngs_sheets_by_id(tmp_path):
    wb = Workbook()
    _sheet(wb, 'Clinical', CLIN_COLS, [_clin('P1'), _clin('P2', hb=7.1), _clin('P3')])
    _sheet(wb, 'NGS', ['Patient_ID'] + GENES,
           [['P2'] + [1 if g == 'ASXL1' else 0 for g in GENES],
            ['P1'] + [0] * len(GENES)])
    path = _save(wb, tmp_path / 'raw.xlsx')
    rows, report = screen_input(path, 'impute', sheet='Clinical,NGS')
    by_id = {r['ID']: r for r in rows}
    assert report.source_info['join'] == 'merge'
    assert list(by_id) == ['P1', 'P2', 'P3']                 # 保留第一頁順序
    assert by_id['P2']['HB'] == '7.1' and by_id['P2']['ASXL1'] == '1'
    assert by_id['P1']['ASXL1'] == '0'
    assert by_id['P3']['ASXL1'] == 'NA'                      # NGS 頁沒有 P3 -> 基因缺失
    assert any('P3' in w and '找不到' in w for w in report.source_warnings)


def test_merge_reports_conflicting_values(tmp_path):
    wb = Workbook()
    _sheet(wb, 'A', ['ID', 'HB', 'PLT', 'BM_BLAST'], [['x', 10, 100, 5]])
    _sheet(wb, 'B', ['ID', 'HB', 'ASXL1', 'KRAS'], [['x', 9, 1, 0]])
    path = _save(wb, tmp_path / 'raw.xlsx')
    rows, report = screen_input(path, 'impute', sheet='A,B', sheet_join='merge')
    assert rows[0]['HB'] == '10'                             # 保留先前分頁的值
    assert any('合併衝突' in w and 'HB' in w for w in report.source_warnings)


def test_partial_overlap_requires_sheet_join(tmp_path):
    wb = Workbook()
    _sheet(wb, 'A', ['ID', 'HB', 'PLT', 'BM_BLAST'], [['x', 10, 100, 5]])
    _sheet(wb, 'B', ['ID', 'HB', 'ASXL1'], [['x', 10, 1]])
    path = _save(wb, tmp_path / 'raw.xlsx')
    with pytest.raises(SourceSelectionError, match='--sheet-join'):
        screen_input(path, 'impute', sheet='A,B')


def test_merge_rejects_duplicate_ids(tmp_path):
    wb = Workbook()
    _sheet(wb, 'A', ['ID', 'HB', 'PLT', 'BM_BLAST'], [['x', 10, 100, 5], ['x', 9, 90, 4]])
    _sheet(wb, 'B', ['ID', 'ASXL1', 'KRAS'], [['x', 1, 0]])
    path = _save(wb, tmp_path / 'raw.xlsx')
    with pytest.raises(SourceSelectionError, match='重複的 ID'):
        screen_input(path, 'impute', sheet='A,B')


def test_hidden_sheet_is_not_auto_selected_but_can_be_requested(tmp_path):
    wb = Workbook()
    _sheet(wb, 'old_backup', STANDARD_COLUMNS + ['extra'], [_full_row('old') + ['x']], hidden=True)
    _sheet(wb, 'Data', ['ID', 'HB', 'PLT', 'BM_BLAST', 'CYTO_IPSSR'], [['new', 10, 100, 5, 'Good']])
    path = _save(wb, tmp_path / 'raw.xlsx')
    rows, _ = screen_input(path, 'impute')
    assert [r['ID'] for r in rows] == ['new']                # 隱藏頁分數較高但不自動選
    rows, report = screen_input(path, 'impute', sheet='old_backup')
    assert [r['ID'] for r in rows] == ['old']
    assert any('隱藏分頁' in w for w in report.source_warnings)


def test_excel_cell_types_percent_formulas_and_integers(tmp_path):
    wb = Workbook()
    ws = _sheet(wb, 'Data', STANDARD_COLUMNS, [_full_row('p1')])
    col = {c: i + 1 for i, c in enumerate(STANDARD_COLUMNS)}
    ws.cell(2, col['BM_BLAST'], 0.09).number_format = '0%'        # 畫面顯示 9%
    ws.cell(2, col['TP53mut'], 1)
    ws.cell(2, col['TP53maxvaf'], 0.85).number_format = '0%'      # 畫面顯示 85%
    ws.cell(2, col['TP53loh'], 1)
    ws.cell(2, col['ASXL1'], True)                               # TRUE 儲存格
    ws.cell(2, col['HB'], 9.0)                                   # 整數型浮點
    path = _save(wb, tmp_path / 'raw.xlsx')

    rows, report = screen_input(path, 'impute')
    r = rows[0]
    assert r['BM_BLAST'] == '9'                                  # 不會被讀成 0.09
    assert r['TP53maxvaf'] == '0.85'
    assert r['ASXL1'] == '1' and r['HB'] == '9'
    rules = {e['Column']: e['Rule'] for e in report.cell_audit}
    assert rules['BM_BLAST'] == 'strip_unit' and rules['TP53maxvaf'] == 'percent_to_fraction'


def test_missing_whole_column_and_duplicate_ids_are_warned(tmp_path):
    wb = Workbook()
    _sheet(wb, 'Data', ['ID', 'PLT', 'BM_BLAST', 'CYTO_IPSSR'],
           [['dup', 100, 5, 'Good'], ['dup', 90, 4, 'Good'], ['', 80, 3, 'Good']])
    path = _save(wb, tmp_path / 'raw.xlsx')
    rows, report = screen_input(path, 'impute')
    warn = ' '.join(report.source_warnings)
    assert '找不到 HB 欄位' in warn
    assert '重複的病患 ID' in warn and 'dup' in warn
    assert rows[2]['ID'] == 'Row_Data!4'                         # 沒有 ID -> 以來源位置識別
    assert all(r['HB'] == 'NA' for r in rows)


def test_csv_in_big5_cp950_with_title_rows(tmp_path):
    path = tmp_path / 'big5.csv'
    lines = ['輔大 MDS 資料,,,', 'ID,HB,PLT,BM_BLAST', '病患甲,9.6,281,9', '病患乙,8,50,12']
    path.write_bytes('\r\n'.join(lines).encode('cp950'))
    rows, report = screen_input(str(path), 'impute')
    assert [r['ID'] for r in rows] == ['病患甲', '病患乙']
    assert report.source_info['encoding'] == 'cp950'
    assert report.source_info['sheets'][0]['header_row'] == 2


def test_legacy_xls_and_unknown_types_give_clear_errors(tmp_path):
    xls = tmp_path / 'old.xls'
    xls.write_bytes(b'not really')
    with pytest.raises(SourceSelectionError, match='.xlsx'):
        screen_input(str(xls), 'impute')
    other = tmp_path / 'data.json'
    other.write_text('{}')
    with pytest.raises(SourceSelectionError, match='不支援'):
        screen_input(str(other), 'impute')


def test_inspect_lists_all_sheets(tmp_path):
    wb = Workbook()
    _sheet(wb, 'notes', ['x'], [['y']])
    _sheet(wb, 'Data', STANDARD_COLUMNS, [_full_row('p1')])
    path = _save(wb, tmp_path / 'raw.xlsx')
    result = inspect_columns(path)
    assert [s['name'] for s in result['source']['sheets']] == ['notes', 'Data']
    assert result['source']['selected'] == ['Data']

    run = subprocess.run(CLI + [path, '--inspect'], capture_output=True, text=True)
    assert run.returncode == 0 and "★ 使用 分頁 'Data'" in run.stdout


def test_cli_reports_ambiguous_sheets_with_exit_code_2(tmp_path):
    wb = Workbook()
    _sheet(wb, '2023', STANDARD_COLUMNS, [_full_row('a')])
    _sheet(wb, '2024', STANDARD_COLUMNS, [_full_row('b')])
    path = _save(wb, tmp_path / 'raw.xlsx')
    run = subprocess.run(CLI + [path, '--screen-only'], capture_output=True, text=True)
    assert run.returncode == 2 and '--sheet' in run.stdout
    run = subprocess.run(CLI + [path, '--screen-only', '--sheet', 'all'], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout
    std = pd.read_csv(tmp_path / 'raw_standardized.csv', dtype=str)
    assert list(std['ID']) == ['a', 'b']


def test_review_workbook_has_source_sheet(tmp_path):
    from ipssm_pipeline import run_review_pipeline
    wb = Workbook()
    _sheet(wb, 'Clinical', CLIN_COLS, [_clin('P1'), _clin('P2')], title_rows=[['title']])
    _sheet(wb, 'NGS', ['Patient_ID'] + GENES, [['P1'] + [0] * len(GENES)])
    path = _save(wb, tmp_path / 'raw.xlsx')
    outcome = run_review_pipeline(path, 'impute', sheet='Clinical,NGS')
    xls = pd.ExcelFile(outcome['paths']['review'])
    assert 'Source' in xls.sheet_names
    src = xls.parse('Source')
    used = src[src['是否使用'] == 'YES']['分頁'].tolist()
    assert used == ['Clinical', 'NGS']
    summary = dict(xls.parse('Summary').values)
    assert 'Clinical, NGS' in summary['來源分頁'] and 'merge' in summary['來源分頁']


def test_split_dataset_is_not_auto_resolved_to_the_gene_sheet(tmp_path):
    """回歸: 基因頁對到的欄位較多，但沒有臨床欄位，不得被自動選為唯一資料頁。"""
    wb = Workbook()
    _sheet(wb, 'Clinical', CLIN_COLS, [_clin('P1'), _clin('P2')])
    _sheet(wb, 'NGS', ['Patient_ID'] + GENES, [['P1'] + [0] * len(GENES)])
    path = _save(wb, tmp_path / 'raw.xlsx')
    with pytest.raises(SourceSelectionError, match='依 ID 合併') as exc:
        screen_input(path, 'impute')
    assert "無臨床欄位" in str(exc.value) and "含 HB/PLT/BM_BLAST" in str(exc.value)


def test_summary_subset_sheet_does_not_block_auto_selection(tmp_path):
    """另一頁只是完整資料頁欄位的子集 (例如摘要)，仍可自動選完整的那頁並提示。"""
    wb = Workbook()
    _sheet(wb, 'Data', STANDARD_COLUMNS, [_full_row('p1'), _full_row('p2')])
    _sheet(wb, 'Summary', ['ID', 'HB', 'PLT', 'BM_BLAST'], [['p1', 10, 120, 5]])
    path = _save(wb, tmp_path / 'raw.xlsx')
    rows, report = screen_input(path, 'impute')
    assert [r['ID'] for r in rows] == ['p1', 'p2']
    assert any("'Summary' 也像病患資料" in w for w in report.source_warnings)
