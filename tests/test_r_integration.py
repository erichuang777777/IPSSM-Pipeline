"""端對端測試 (需要本機有 Rscript 與 R 套件 ipssm；否則自動略過)。

驗證缺失值的實際行為: 空白 == NA、NA != 0、TP53mut=2 不會讓整批失敗。
"""
import os
import subprocess
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_pipeline import run_screening, run_translation, _find_rscript
from test_validation import _make_row, _write_csv


def _r_ready():
    rscript = _find_rscript()
    if not rscript:
        return False
    r = subprocess.run([rscript, '-e', 'if(!requireNamespace("ipssm",quietly=TRUE)) quit(status=1)'],
                       capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _r_ready(), reason="需要 Rscript 與 R 套件 ipssm")


@pytest.fixture
def results(tmp_path):
    rows = [
        _make_row('complete'),
        _make_row('cyto_blank', CYTO_IPSSR=''),
        _make_row('cyto_na', CYTO_IPSSR='NA'),
        _make_row('asxl1_na', ASXL1='NA'),
        _make_row('asxl1_zero', ASXL1='0'),
        _make_row('hb_na', HB='NA'),
        _make_row('tp53_two', TP53mut='2', TP53maxvaf='0.3'),
    ]
    in_csv = tmp_path / "in.csv"
    cleaned = tmp_path / "in_cleaned.csv"
    log = tmp_path / "in_screening_log.txt"
    _write_csv(in_csv, rows)
    assert run_screening(str(in_csv), str(cleaned), str(log), missing_mode='impute')
    assert run_translation(str(cleaned))   # TP53mut='2' 不得讓整批失敗
    df = pd.read_excel(tmp_path / "in_cleaned_results.xlsx", sheet_name='R_Full_Output')
    return df.set_index('ID')


def test_blank_equals_na(results):
    cols = ['IPSSMscore_best', 'IPSSMscore_mean', 'IPSSMscore_worst']
    assert results.loc['cyto_blank', cols].tolist() == results.loc['cyto_na', cols].tolist()


def test_na_is_not_zero(results):
    na, zero = results.loc['asxl1_na'], results.loc['asxl1_zero']
    assert na['IPSSMscore_mean'] > zero['IPSSMscore_mean']     # 補平均突變頻率 (0.252)，不是 0
    assert na['IPSSMscore_worst'] > zero['IPSSMscore_worst']
    assert zero['Range_Score'] == 0                             # 填 0 = 檢測過無突變 -> 無不確定性


def test_missing_info_columns(results):
    assert results.loc['complete', 'Used_Scenario_Analysis'] == 'NO'
    assert results.loc['complete', 'Confidence_Level'] == 'CONFIDENT'
    assert results.loc['cyto_na', 'Used_Scenario_Analysis'] == 'YES'
    assert results.loc['cyto_na', 'Missing_Fields'] == 'CYTO_IPSSR'
    assert results.loc['cyto_na', 'Missing_Groups'] == 'cytogenetic'
    assert results.loc['asxl1_na', 'Missing_Groups'] == 'genes'     # 基因缺失也要被標記
    assert results.loc['hb_na', 'Missing_Groups'] == 'clinical'


def test_reported_score_is_mean_scenario(results):
    assert (results['Reported_Score'] == results['IPSSMscore_mean']).all()
    assert (results['Reported_Cat'] == results['IPSSMcat_mean']).all()


def test_uncertain_when_range_exceeds_one(results):
    assert results.loc['cyto_na', 'Confidence_Level'] == 'UNCERTAIN'
    assert results.loc['hb_na', 'Confidence_Level'] == 'UNCERTAIN'


# ---------------------------------------------------------------------------
#  Stage 2~4: 試算 / auto 模式 / 核對檔 / 正式計算
# ---------------------------------------------------------------------------
import subprocess as _sp

from ipssm_pipeline import (
    STANDARD_COLUMNS, STRONG_MISSING_FIELDS, GENE_FIELDS, run_review_pipeline,
    run_trial_calculation, _find_rscript as _rs,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = [sys.executable, os.path.join(REPO, 'ipssm_pipeline.py')]


def test_strong_missing_fields_table_matches_r():
    """以 R 套件實測: 單一欄位缺失時，只有 STRONG_MISSING_FIELDS 會讓範圍超過 1。"""
    variants = {}
    for col in ['HB', 'PLT', 'BM_BLAST', 'del5q', 'del7_7q', 'del17_17p', 'complex',
                'CYTO_IPSSR', 'TP53mut'] + GENE_FIELDS:
        variants[col] = _make_row(f'na_{col}', **{col: 'NA'})
    # TP53maxvaf / TP53loh 只有在 TP53mut=1 且無法判定時才有影響
    variants['TP53loh'] = _make_row('na_TP53loh', TP53mut='1', TP53loh='NA', TP53maxvaf='NA')

    rows = list(variants.values())
    trial = run_trial_calculation(rows, _rs())
    assert trial is not None and len(trial) == len(rows)

    uncertain = {col for col, res in zip(variants, trial) if float(res['Range_Score']) > 1}
    expected = STRONG_MISSING_FIELDS - {'TP53maxvaf'}   # maxvaf 單獨缺失不構成缺失 (見 missing_model_fields)
    assert uncertain == expected, (uncertain ^ expected)

    # 數值與文件中的表一致 (單欄缺失造成的範圍)
    rng = {col: float(res['Range_Score']) for col, res in zip(variants, trial)}
    assert rng['HB'] == pytest.approx(3.95, abs=0.02)
    assert rng['BM_BLAST'] == pytest.approx(2.03, abs=0.02)
    assert rng['CYTO_IPSSR'] == pytest.approx(1.66, abs=0.02)
    assert rng['PLT'] == pytest.approx(0.80, abs=0.02)


def _mixed_cohort():
    return [
        _make_row('clean'),
        _make_row('nd_gene', ASXL1='ND'),                    # ND -> 0，不算缺失
        _make_row('gene_na', ASXL1='NA', DNMT3A=''),         # 範圍小 -> 補平均
        _make_row('plt_na', PLT=''),                         # 0.80 -> 補平均
        _make_row('cyto_na', CYTO_IPSSR=''),                 # 1.66 -> UNCERTAIN
        _make_row('hb_na', HB='NA'),                         # 3.95 -> UNCERTAIN
        _make_row('bad_value', ASXL1='???'),                 # 資料錯誤
    ]


def test_auto_mode_excludes_uncertain_and_writes_review_files(tmp_path):
    in_csv = tmp_path / 'cohort.csv'
    _write_csv(in_csv, _mixed_cohort())
    outcome = run_review_pipeline(str(in_csv), 'auto', 1.0)
    assert outcome is not None

    paths = outcome['paths']
    for key in ('standardized', 'audit', 'review', 'log'):
        assert paths[key].exists(), key

    std = pd.read_csv(paths['standardized'], dtype=str, keep_default_na=False)
    assert list(std['ID']) == ['clean', 'nd_gene', 'gene_na', 'plt_na']
    assert list(std.columns) == STANDARD_COLUMNS

    xls = pd.ExcelFile(paths['review'])
    assert {'Summary', 'Standardized', 'Field_Audit', 'Column_Summary', 'Missing_Check',
            'Uncertain', 'Excluded'} <= set(xls.sheet_names)

    check = xls.parse('Missing_Check', keep_default_na=False).set_index('ID')
    assert check.loc['cyto_na', 'Decision'] == 'EXCLUDE_UNCERTAIN'
    assert check.loc['hb_na', 'Decision'] == 'EXCLUDE_UNCERTAIN'
    assert check.loc['gene_na', 'Decision'] == 'INCLUDE'
    assert check.loc['nd_gene', 'Missing_Fields'] == 'NONE'
    assert check.loc['hb_na', 'Key_Missing_Fields'] == 'HB'

    unc = xls.parse('Uncertain')
    assert set(unc['ID']) == {'cyto_na', 'hb_na'}
    exc = xls.parse('Excluded').set_index('ID')
    assert exc.loc['bad_value', 'Source'] == 'validation'
    assert exc.loc['cyto_na', 'Source'] == 'uncertain' and exc.loc['hb_na', 'Source'] == 'uncertain'
    assert 'ND' in set(xls.parse('Field_Audit')['Original'])


def test_max_range_threshold_is_adjustable(tmp_path):
    in_csv = tmp_path / 'cohort.csv'
    _write_csv(in_csv, _mixed_cohort())
    strict = run_review_pipeline(str(in_csv), 'auto', 0.5)      # PLT 缺失 (0.80) 也會被排除
    assert 'plt_na' not in [r['ID'] for r in strict['rows']]
    loose = run_review_pipeline(str(in_csv), 'auto', 5.0)       # HB 缺失 (3.95) 也可接受
    assert {'cyto_na', 'hb_na'} <= {r['ID'] for r in loose['rows']}


def test_impute_mode_keeps_everyone_but_flags_uncertain(tmp_path):
    in_csv = tmp_path / 'cohort.csv'
    _write_csv(in_csv, _mixed_cohort())
    outcome = run_review_pipeline(str(in_csv), 'impute')
    ids = [r['ID'] for r in outcome['rows']]
    assert ids == ['clean', 'nd_gene', 'gene_na', 'plt_na', 'cyto_na', 'hb_na']
    flags = {m['ID']: m['Confidence_Level'] for m in outcome['missing_check']}
    assert flags['cyto_na'] == flags['hb_na'] == 'UNCERTAIN' and flags['gene_na'] == 'CONFIDENT'
    assert outcome['uncertain_sheet'] is None


def test_default_cli_stops_for_review_then_calc_only_finishes(tmp_path):
    in_csv = tmp_path / 'cohort.csv'
    _write_csv(in_csv, _mixed_cohort())

    first = _sp.run(CLI + [str(in_csv), '--missing', 'auto'], capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    std_csv = tmp_path / 'cohort_standardized.csv'
    assert std_csv.exists() and (tmp_path / 'cohort_review.xlsx').exists()
    # 預設不會自動做正式計算
    assert not (tmp_path / 'cohort_standardized_results.xlsx').exists()
    assert '--calc-only' in first.stdout

    second = _sp.run(CLI + [str(std_csv), '--calc-only'], capture_output=True, text=True)
    assert second.returncode == 0, second.stdout + second.stderr
    res = pd.read_excel(tmp_path / 'cohort_standardized_results.xlsx', sheet_name='Summary')
    assert list(res['ID']) == ['clean', 'nd_gene', 'gene_na', 'plt_na']
    assert (res['Confidence_Level'] == 'CONFIDENT').all()


def test_confirm_flag_runs_all_stages_and_keeps_uncertain_interval(tmp_path):
    in_csv = tmp_path / 'cohort.csv'
    _write_csv(in_csv, _mixed_cohort())
    run = _sp.run(CLI + [str(in_csv), '--missing', 'auto', '--confirm'], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr

    xls = pd.ExcelFile(tmp_path / 'cohort_standardized_results.xlsx')
    assert 'Uncertain_Excluded' in xls.sheet_names
    unc = xls.parse('Uncertain_Excluded').set_index('ID')
    # 被排除的 UNCERTAIN 病患仍保留 Best/Mean/Worst 區間供參考
    assert set(unc.index) == {'cyto_na', 'hb_na'}
    assert unc.loc['hb_na', 'IPSSMscore_worst'] > unc.loc['hb_na', 'IPSSMscore_best']
    assert list(xls.parse('Summary')['ID']) == ['clean', 'nd_gene', 'gene_na', 'plt_na']


def test_skip_mode_review_has_no_uncertain_inclusion(tmp_path):
    in_csv = tmp_path / 'cohort.csv'
    _write_csv(in_csv, _mixed_cohort())
    outcome = run_review_pipeline(str(in_csv), 'skip')
    assert [r['ID'] for r in outcome['rows']] == ['clean', 'nd_gene']     # 任何缺失都跳過
    kinds = {x['ID']: x['Source'] for x in outcome['report'].excluded}
    assert kinds['gene_na'] == kinds['plt_na'] == kinds['cyto_na'] == 'missing'
    assert kinds['bad_value'] == 'validation'


def test_auto_mode_without_r_fails_clearly(tmp_path, monkeypatch):
    import ipssm_pipeline
    monkeypatch.setattr(ipssm_pipeline, '_find_rscript', lambda: None)
    in_csv = tmp_path / 'cohort.csv'
    _write_csv(in_csv, _mixed_cohort())
    assert run_review_pipeline(str(in_csv), 'auto') is None
