"""LLM 輔助檢核測試 — 以 in-process 的假 HTTP 伺服器走真實的 urllib 傳輸。

重點: (1) 信心門檻 / 允許值檢查  (2) 遠端端點需明確同意
      (3) 隱私: 請求內容只含欄位名稱與單一儲存格原文，不含 ID 或其他欄位  (4) 快取
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ipssm_llm import LLMConfig, LLMResolver, is_local_endpoint
from ipssm_pipeline import screen_input
from test_validation import _make_row, _write_csv

# 假 LLM 的回答: cell text -> (value, confidence, reason)
ANSWERS = {
    'weakly positive': ('1', 0.92, 'weak positive means mutated'),
    'borderline': ('1', 0.50, 'unsure'),
    'maybe-ish': (None, 0.10, 'cannot interpret'),
    'gibberish-value': ('7', 0.99, 'out of allowed set'),
}


class _Handler(BaseHTTPRequestHandler):
    bodies = []
    headers_seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        _Handler.bodies.append(body)
        _Handler.headers_seen.append(dict(self.headers))
        user_text = (body['messages'][-1]['content'])
        cell = next((k for k in ANSWERS if f'"{k}"' in user_text), None)
        value, conf, reason = ANSWERS.get(cell, (None, 0.0, 'unknown'))
        reply = json.dumps({'value': value, 'confidence': conf, 'reason': reason})
        if self.path.endswith('/v1/messages'):
            payload = {'content': [{'type': 'text', 'text': reply}]}
        else:
            payload = {'choices': [{'message': {'content': reply}}]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server():
    _Handler.bodies, _Handler.headers_seen = [], []
    httpd = HTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def _resolver(server, provider='openai', **kw):
    return LLMResolver(LLMConfig(provider=provider, endpoint=server + ('/v1' if provider == 'openai' else ''),
                                 model='fake-model', **kw))


def test_is_local_endpoint():
    assert is_local_endpoint('http://localhost:11434/v1')
    assert is_local_endpoint('http://127.0.0.1:8000/v1')
    assert is_local_endpoint('http://192.168.1.20:1234/v1')
    assert not is_local_endpoint('https://api.openai.com/v1')
    assert not is_local_endpoint('https://api.anthropic.com')


def test_remote_endpoint_refused_without_explicit_allow():
    with pytest.raises(ValueError, match='not local'):
        LLMResolver(LLMConfig(provider='openai', endpoint='https://api.openai.com/v1', model='m'))
    # 明確同意後才可建立
    LLMResolver(LLMConfig(provider='openai', endpoint='https://api.openai.com/v1', model='m',
                          allow_remote=True))


def test_config_validation():
    with pytest.raises(ValueError):
        LLMResolver(LLMConfig(provider='openai', endpoint='http://localhost:1/v1', model=''))
    with pytest.raises(ValueError):
        LLMResolver(LLMConfig(provider='nope', endpoint='http://localhost:1/v1', model='m'))


def test_accepts_high_confidence_and_rejects_low_or_invalid(server):
    r = _resolver(server)
    ok = r.resolve('ASXL1', 'weakly positive', 'binary', ['0', '1'])
    assert ok.accepted and ok.value == '1' and ok.confidence == pytest.approx(0.92)

    low = r.resolve('ASXL1', 'borderline', 'binary', ['0', '1'])
    assert not low.accepted and 'min confidence' in low.error

    null = r.resolve('ASXL1', 'maybe-ish', 'binary', ['0', '1'])
    assert not null.accepted and null.value is None

    bad = r.resolve('ASXL1', 'gibberish-value', 'binary', ['0', '1'])
    assert not bad.accepted and 'allowed' in bad.error     # 即使信心很高，值不在允許集合就拒絕


def test_unreachable_endpoint_fails_closed():
    r = LLMResolver(LLMConfig(provider='openai', endpoint='http://127.0.0.1:9/v1', model='m',
                              timeout=2))
    res = r.resolve('ASXL1', 'weakly positive', 'binary', ['0', '1'])
    assert not res.accepted and 'LLM call failed' in res.error


def test_results_are_cached_per_column_and_text(server):
    r = _resolver(server)
    for _ in range(3):
        r.resolve('ASXL1', 'weakly positive', 'binary', ['0', '1'])
    r.resolve('KRAS', 'weakly positive', 'binary', ['0', '1'])
    assert len(_Handler.bodies) == 2        # (ASXL1, 文字) 一次 + (KRAS, 文字) 一次


def test_anthropic_provider_uses_messages_api(server):
    r = _resolver(server, provider='anthropic', api_key='test-key')
    res = r.resolve('ASXL1', 'weakly positive', 'binary', ['0', '1'])
    assert res.accepted and res.value == '1'
    hdr = {k.lower(): v for k, v in _Handler.headers_seen[0].items()}
    assert hdr['x-api-key'] == 'test-key' and 'anthropic-version' in hdr
    assert 'system' in _Handler.bodies[0]


def test_pipeline_applies_llm_and_flags_for_review_and_leaks_nothing(server, tmp_path):
    rows = [
        _make_row('SECRET-ID-123', ASXL1='weakly positive', HB='10.5', PLT='222'),
        _make_row('p2', KRAS='maybe-ish'),          # LLM 也無法解讀 -> 仍為資料錯誤
        _make_row('p3', RUNX1='borderline'),        # 信心不足 -> 不採用 -> 資料錯誤
    ]
    in_csv = tmp_path / 'in.csv'
    _write_csv(in_csv, rows)

    resolver = _resolver(server)
    out_rows, report = screen_input(str(in_csv), 'impute', resolver)

    # 採用 LLM 結果，且一律標為「需人工確認」、來源為 llm
    assert [r['ID'] for r in out_rows] == ['SECRET-ID-123']
    assert out_rows[0]['ASXL1'] == '1'
    entry = next(e for e in report.cell_audit if e['Column'] == 'ASXL1')
    assert entry['Source'] == 'llm' and entry['Status'] == 'needs_review'
    assert entry['Rule'] == 'llm_interpretation' and float(entry['Confidence']) == pytest.approx(0.92)

    # 被拒絕的 LLM 結果仍然留紀錄，且病患被排除
    rejected = [e for e in report.cell_audit if e['Source'] == 'llm_rejected']
    assert {e['Column'] for e in rejected} == {'KRAS', 'RUNX1'}
    assert {x['ID'] for x in report.excluded} == {'p2', 'p3'}
    assert len(report.llm_results) == 3

    # 隱私: 送出的請求只含欄位與單一儲存格原文，不含 ID 或同列其他欄位的值
    wire = json.dumps(_Handler.bodies)
    assert 'SECRET-ID-123' not in wire
    assert '10.5' not in wire and '222' not in wire
    assert 'weakly positive' in wire and 'ASXL1' in wire


def test_llm_only_called_for_unparseable_cells(server, tmp_path):
    """規則能解析的 (Positive / ND / 單位) 不應該打擾 LLM。"""
    rows = [_make_row('p1', ASXL1='Positive', RUNX1='ND', HB='9.6 g/dL', CYTO_IPSSR='very_good')]
    in_csv = tmp_path / 'in.csv'
    _write_csv(in_csv, rows)
    out_rows, _ = screen_input(str(in_csv), 'impute', _resolver(server))
    assert len(out_rows) == 1
    assert _Handler.bodies == []


# ---------------------------------------------------------------------------
#  CLI 接線 (--llm 旗標)
# ---------------------------------------------------------------------------
import subprocess

import pandas as pd

_CLI = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     'ipssm_pipeline.py')]


def _cohort(tmp_path):
    path = tmp_path / 'cohort.csv'
    _write_csv(path, [_make_row('SECRET-ID-9', ASXL1='weakly positive'), _make_row('p2')])
    return path


def test_cli_refuses_remote_endpoint_without_allow(tmp_path):
    run = subprocess.run(_CLI + [str(_cohort(tmp_path)), '--missing', 'impute', '--llm',
                                 '--llm-endpoint', 'https://api.openai.com/v1', '--llm-model', 'm'],
                         capture_output=True, text=True)
    assert run.returncode == 2
    assert '--llm-allow-remote' in run.stdout
    assert not (tmp_path / 'cohort_review.xlsx').exists()      # 一筆資料都沒送出、也沒開始處理


def test_cli_requires_model_and_endpoint(tmp_path):
    run = subprocess.run(_CLI + [str(_cohort(tmp_path)), '--llm', '--llm-endpoint',
                                 'http://localhost:9/v1'], capture_output=True, text=True)
    assert run.returncode == 2 and 'model' in run.stdout.lower()


def test_cli_end_to_end_with_local_llm(server, tmp_path):
    run = subprocess.run(_CLI + [str(_cohort(tmp_path)), '--missing', 'impute', '--llm',
                                 '--llm-endpoint', server + '/v1', '--llm-model', 'fake-model'],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr

    xls = pd.ExcelFile(tmp_path / 'cohort_review.xlsx')
    assert 'LLM_Review' in xls.sheet_names
    llm = xls.parse('LLM_Review')
    assert list(llm['Original text']) == ['weakly positive'] and llm['Accepted'].iloc[0] == 'YES'
    summary = dict(xls.parse('Summary').values)
    assert '本地' in str(summary['LLM 輔助'])
    std = pd.read_csv(tmp_path / 'cohort_standardized.csv', dtype=str)
    assert std.loc[std['ID'] == 'SECRET-ID-9', 'ASXL1'].iloc[0] == '1'
    assert 'SECRET-ID-9' not in json.dumps(_Handler.bodies)       # 沒有外洩 ID
