"""端對端測試 (需要本機有 Rscript 與 R 套件 ipssm；否則自動略過)。

驗證缺失值的實際行為: 空白 == NA、NA != 0、TP53mut=2 不會讓整批失敗。
"""
import os
import shutil
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
