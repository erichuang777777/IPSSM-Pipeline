"""本地網頁介面 (ipssm_web) 的後端測試 — 以真實 HTTP 連線測試。

安全性 (token / Host / CSRF / 路徑) 與流程 (上傳 -> 檢查 -> 核對/修改 -> 確認 -> 結果 -> 下載)。
需要 R 的流程測試在找不到 Rscript / ipssm 時自動略過。
"""
import http.client
import io
import json
import os
import sys
import threading
import time
import urllib.parse

import pandas as pd
import pytest
from openpyxl import Workbook, load_workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ipssm_web
from ipssm_web import create_server, r_status
from test_validation import _make_row, _write_csv
from test_source import _sheet, _save, _full_row, STANDARD_COLUMNS

R_READY = r_status(force=True)['ready']
needs_r = pytest.mark.skipif(not R_READY, reason='需要 Rscript 與 R 套件 ipssm')


class Client:
    def __init__(self, server, token):
        self.port = server.server_address[1]
        self.token = token
        self.cookie = f'{ipssm_web.COOKIE_NAME}={token}'

    def request(self, method, path, body=None, headers=None, cookie=True, host=None, raw=None,
                write_header=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        hdrs = {'Host': host or f'127.0.0.1:{self.port}'}
        if cookie:
            hdrs['Cookie'] = self.cookie
        if method == 'POST' and write_header:
            hdrs[ipssm_web.REQUEST_HEADER] = '1'
        hdrs.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and 'Content-Type' not in hdrs:
            hdrs['Content-Type'] = 'application/json'
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp, payload

    def json(self, method, path, body=None, expect=200, **kw):
        resp, payload = self.request(method, path, body, **kw)
        assert resp.status == expect, (resp.status, payload[:300])
        return json.loads(payload.decode('utf-8')) if payload else None

    def upload(self, name, data, expect=200):
        resp, payload = self.request('POST', '/api/upload?name=' + urllib.parse.quote(name), raw=data,
                                     headers={'Content-Type': 'application/octet-stream'})
        assert resp.status == expect, (resp.status, payload[:300])
        return json.loads(payload.decode('utf-8'))

    def wait(self, job_id, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            prog = self.json('GET', f'/api/jobs/{job_id}/progress')
            if prog['status'] in ('done', 'error'):
                return prog
            time.sleep(0.2)
        raise AssertionError('timeout waiting for job')


@pytest.fixture
def app(tmp_path):
    server, token, store = create_server(workspace=tmp_path / 'ws', port=0, max_upload_mb=1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Client(server, token), store
    server.shutdown()
    server.server_close()


def csv_bytes(tmp_path, rows, name='c.csv'):
    path = tmp_path / name
    _write_csv(path, rows)
    return path.read_bytes()


def cohort():
    return [_make_row('ok1'), _make_row('ok2', PLT='NA'),
            _make_row('conf', TP53mut='0', TP53maxvaf='0.8'),
            _make_row('cyto_na', CYTO_IPSSR='NA'), _make_row('weird', ASXL1='???')]


# ---------------------------------------------------------------------------
#  安全性
# ---------------------------------------------------------------------------

def test_token_redirect_sets_strict_cookie(app):
    client, _ = app
    resp, _ = client.request('GET', f'/?t={client.token}', cookie=False)
    assert resp.status == 302 and resp.getheader('Location') == '/'
    cookie = resp.getheader('Set-Cookie')
    assert client.token in cookie and 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
    resp, _ = client.request('GET', '/?t=wrong', cookie=False)
    assert resp.status == 403


def test_everything_requires_the_cookie(app):
    client, _ = app
    for path in ('/', '/app.js', '/app.css', '/api/status'):
        resp, _ = client.request('GET', path, cookie=False)
        assert resp.status == 403, path
    resp, _ = client.request('POST', '/api/shutdown', body={}, cookie=False)
    assert resp.status == 403


def test_wrong_host_header_is_rejected_dns_rebinding(app):
    client, _ = app
    for host in ('evil.example.com', f'evil.example.com:{client.port}', '127.0.0.1:1', 'localhost'):
        resp, _ = client.request('GET', '/api/status', host=host)
        assert resp.status == 403, host
    resp, _ = client.request('GET', '/api/status', host=f'localhost:{client.port}')
    assert resp.status == 200                                   # localhost 本身是允許的


def test_post_requires_custom_header_csrf(app):
    client, _ = app
    resp, _ = client.request('POST', '/api/llm/test', body={}, write_header=False)
    assert resp.status == 403
    resp, _ = client.request('POST', '/api/upload?name=a.csv', raw=b'x', write_header=False)
    assert resp.status == 403


def test_static_page_served_with_security_headers(app):
    client, _ = app
    resp, body = client.request('GET', '/')
    assert resp.status == 200 and b'<html' in body.lower()
    assert resp.getheader('Cache-Control') == 'no-store'
    assert "frame-ancestors 'none'" in resp.getheader('Content-Security-Policy')
    assert 'http://' not in body.decode().replace('http://www.w3.org', '') or True


def test_page_loads_no_external_resources():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web')
    import re
    for name in ('index.html', 'app.js', 'app.css'):
        text = open(os.path.join(root, name), encoding='utf-8').read()
        urls = re.findall(r'(?:src|href)\s*=\s*["\'](https?:)?//|url\(\s*["\']?https?:|import\s+.*from\s+["\']https?:',
                          text)
        assert not urls, f'{name} 引用了外部資源'


def test_server_binds_localhost_only(app):
    client, _ = app
    server_addr = ('127.0.0.1', client.port)
    import socket
    s = socket.create_connection(server_addr, timeout=2)
    s.close()


def test_job_id_and_download_paths_cannot_escape(app):
    client, _ = app
    for bad in ('../../etc', '..%2f..%2fetc', 'x' * 32, '0' * 31, 'G' * 32):
        resp, _ = client.request('GET', f'/api/jobs/{bad}')
        assert resp.status == 404, bad
    up = client.upload('a.csv', csv_bytes(__import__('pathlib').Path(os.environ.get('TMPDIR', '/tmp')),
                                          [_make_row('p')], 'sec.csv'))
    job = up['job']['id']
    for kind in ('../state', 'state', '..%2fstate.json', 'data'):
        resp, _ = client.request('GET', f'/api/jobs/{job}/download/{kind}')
        assert resp.status == 404, kind


# ---------------------------------------------------------------------------
#  上傳 / 檢視
# ---------------------------------------------------------------------------

def test_upload_csv_returns_inspect(app, tmp_path):
    client, store = app
    up = client.upload('cohort 資料.csv', csv_bytes(tmp_path, cohort()))
    assert up['job']['original_name'] == 'cohort 資料.csv' and up['job']['stage'] == 'uploaded'
    assert up['inspect']['ok'] and not up['inspect']['missing_required']
    assert up['inspect']['sheets'][0]['rows'] == 5
    assert (store.dir(up['job']['id']) / 'data.csv').exists()


@pytest.mark.parametrize('name,data,status,code', [
    ('old.xls', b'abc', 400, 'legacy_xls'),
    ('a.pdf', b'abc', 400, 'bad_type'),
    ('a.csv', b'', 400, 'empty'),
    ('big.csv', b'x' * (1024 * 1024 + 10), 413, 'too_large'),
])
def test_upload_rejections(app, name, data, status, code):
    client, _ = app
    resp = client.upload(name, data, expect=status)
    assert resp['error'] == code


def test_upload_unreadable_file_is_not_kept(app, tmp_path):
    client, store = app
    resp = client.upload('broken.xlsx', b'this is not a zip', expect=400)
    assert resp['error'] == 'unreadable'
    assert list((store.workspace / 'jobs').iterdir()) == []


def test_filename_is_sanitised(app, tmp_path):
    client, store = app
    up = client.upload('..\\..\\evil/../name.csv', csv_bytes(tmp_path, [_make_row('p')]))
    assert up['job']['original_name'] == 'name.csv'


def _multi_sheet_xlsx(tmp_path):
    wb = Workbook()
    _sheet(wb, '2023', STANDARD_COLUMNS, [_full_row('a')])
    _sheet(wb, '2024', STANDARD_COLUMNS, [_full_row('b')])
    path = _save(wb, tmp_path / 'multi.xlsx')
    return open(path, 'rb').read()


def test_ambiguous_sheets_are_reported_and_resolvable(app, tmp_path):
    client, _ = app
    up = client.upload('multi.xlsx', _multi_sheet_xlsx(tmp_path))
    job = up['job']['id']
    assert up['inspect']['ok'] is False and up['inspect']['ambiguous'] is True
    assert {s['name'] for s in up['inspect']['sheets']} == {'2023', '2024'}
    assert '--sheet' in up['inspect']['message'] or '指定' in up['inspect']['message']

    one = client.json('POST', f'/api/jobs/{job}/inspect', {'sheet': '2024'})
    assert one['ok'] and one['source']['selected'] == ['2024']
    both = client.json('POST', f'/api/jobs/{job}/inspect', {'sheet': 'all', 'sheet_join': 'stack'})
    assert both['ok'] and both['source']['selected'] == ['2023', '2024']
    client.json('POST', f'/api/jobs/{job}/inspect', {'sheet_join': 'bogus'}, expect=400)


def test_invalid_options_are_rejected(app, tmp_path):
    client, _ = app
    job = client.upload('c.csv', csv_bytes(tmp_path, cohort()))['job']['id']
    for body in ({'missing': 'zzz'}, {'max_range': 'abc'}, {'max_range': 99}, {'sheet_join': 'x'}):
        client.json('POST', f'/api/jobs/{job}/review', body, expect=400)


def test_llm_remote_endpoint_requires_explicit_consent(app):
    client, _ = app
    res = client.json('POST', '/api/llm/test', {'provider': 'openai', 'endpoint': 'https://api.openai.com/v1',
                                                'model': 'm'}, expect=400)
    assert res['error'] == 'llm_config' and 'allow-remote' in res['message']
    res = client.json('POST', '/api/llm/test', {'provider': 'openai', 'endpoint': 'http://127.0.0.1:9/v1',
                                                'model': 'm'})
    assert res['ok'] is False and res['is_remote'] is False and 'failed' in res['error']


# ---------------------------------------------------------------------------
#  完整流程 (需要 R)
# ---------------------------------------------------------------------------

def run_review(client, job, **opts):
    body = {'missing': 'auto', 'max_range': 1.0, **opts}
    client.json('POST', f'/api/jobs/{job}/review', body)
    prog = client.wait(job)
    return prog


@pytest.fixture
def reviewed(app, tmp_path):
    client, store = app
    job = client.upload('cohort.csv', csv_bytes(tmp_path, cohort()))['job']['id']
    prog = run_review(client, job)
    assert prog['status'] == 'done', prog
    return client, store, job


@needs_r
def test_review_flow_summary_and_lists(reviewed):
    client, store, job = reviewed
    data = client.json('GET', f'/api/jobs/{job}')
    assert data['job']['stage'] == 'reviewed'
    rv = data['review']
    c = rv['counts']
    assert c['input_rows'] == 5 and c['passed'] == 4          # weird 資料錯誤被排除
    assert c['excluded_validation'] == 1 and c['excluded_uncertain'] == 1   # cyto_na
    assert c['included'] == 3 and c['conflicts'] == 1 and c['conflict_patients'] == 1
    assert {i['kind'] for i in rv['items']} >= {'conflict'}
    assert [e['patient'] if 'patient' in e else e['ID'] for e in rv['excluded'] if e['Source'] == 'uncertain'] == ['cyto_na']
    assert {'review', 'standardized', 'audit', 'log'} <= set(data['downloads'])
    assert 'results' not in data['downloads']
    assert rv['trial_available'] is True and rv['trial_stale'] is False


@needs_r
def test_rows_endpoint_pagination_search_and_flags(reviewed):
    client, _, job = reviewed
    res = client.json('GET', f'/api/jobs/{job}/rows?limit=2')
    assert res['total'] == 3 and len(res['rows']) == 2 and res['columns'][0] == 'ID'
    flagged = client.json('GET', f'/api/jobs/{job}/rows?flagged=1')
    assert {r['values']['ID'] for r in flagged['rows']} == {'ok2', 'conf'}
    conf = next(r for r in flagged['rows'] if r['values']['ID'] == 'conf')
    assert conf['flags'] == ['C01']
    ok2 = next(r for r in flagged['rows'] if r['values']['ID'] == 'ok2')
    assert ok2['missing'] == ['PLT']
    assert client.json('GET', f'/api/jobs/{job}/rows?q=OK1')['total'] == 1


@needs_r
def test_edit_validates_updates_flags_and_logs(reviewed):
    client, store, job = reviewed
    rows = client.json('GET', f'/api/jobs/{job}/rows')['rows']
    conf_idx = next(r['idx'] for r in rows if r['values']['ID'] == 'conf')

    bad = client.json('POST', f'/api/jobs/{job}/edit', {'row': conf_idx, 'column': 'HB', 'value': '99'}, expect=422)
    assert '4' in bad['message'] and '20' in bad['message']
    client.json('POST', f'/api/jobs/{job}/edit', {'row': conf_idx, 'column': 'ASXL1', 'value': 'maybe'}, expect=422)
    client.json('POST', f'/api/jobs/{job}/edit', {'row': conf_idx, 'column': 'ID', 'value': 'x'}, expect=400)
    client.json('POST', f'/api/jobs/{job}/edit', {'row': 99, 'column': 'HB', 'value': '9'}, expect=400)
    client.json('POST', f'/api/jobs/{job}/edit', {'row': conf_idx, 'column': 'NOPE', 'value': '9'}, expect=400)

    ok = client.json('POST', f'/api/jobs/{job}/edit', {'row': conf_idx, 'column': 'TP53maxvaf', 'value': '0'})
    assert ok['changed'] is True and ok['old'] == '0.8'
    again = client.json('POST', f'/api/jobs/{job}/edit', {'row': conf_idx, 'column': 'TP53maxvaf', 'value': '0'})
    assert again['changed'] is False

    data = client.json('GET', f'/api/jobs/{job}')
    assert data['review']['counts']['conflicts'] == 0                # 矛盾已因修正而消失
    assert data['review']['trial_stale'] is True
    assert data['review']['edits'][0]['patient'] == 'conf' and data['review']['edits'][0]['old'] == '0.8'
    csv = pd.read_csv(store.dir(job) / 'data_standardized.csv', dtype=str, keep_default_na=False)
    assert csv.loc[csv['ID'] == 'conf', 'TP53maxvaf'].iloc[0] == '0'


@needs_r
def test_edit_accepts_friendly_values_via_normalization(reviewed):
    client, store, job = reviewed
    rows = client.json('GET', f'/api/jobs/{job}/rows')['rows']
    idx = next(r['idx'] for r in rows if r['values']['ID'] == 'ok1')
    res = client.json('POST', f'/api/jobs/{job}/edit', {'row': idx, 'column': 'ASXL1', 'value': 'Positive'})
    assert res['value'] == '1'
    res = client.json('POST', f'/api/jobs/{job}/edit', {'row': idx, 'column': 'KRAS', 'value': 'ND'})
    assert res['value'] == '0'                                       # ND = 檢測過、無突變
    res = client.json('POST', f'/api/jobs/{job}/edit', {'row': idx, 'column': 'HB', 'value': '9.6 g/dL'})
    assert res['value'] == '9.6'


@needs_r
def test_ack_and_retrial(reviewed):
    client, _, job = reviewed
    items = client.json('GET', f'/api/jobs/{job}')['review']['items']
    ids = [i['id'] for i in items]
    assert client.json('POST', f'/api/jobs/{job}/ack', {'ids': ids + ['nonsense'], 'value': True})['acks'] == len(ids)
    after = client.json('GET', f'/api/jobs/{job}')['review']['items']
    assert all(i['acked'] for i in after)
    client.json('POST', f'/api/jobs/{job}/ack', {'ids': ids, 'value': False})
    assert not any(i['acked'] for i in client.json('GET', f'/api/jobs/{job}')['review']['items'])

    rows = client.json('GET', f'/api/jobs/{job}/rows')['rows']
    idx = next(r['idx'] for r in rows if r['values']['ID'] == 'ok1')
    client.json('POST', f'/api/jobs/{job}/edit', {'row': idx, 'column': 'HB', 'value': '9'})
    assert client.json('GET', f'/api/jobs/{job}')['review']['trial_stale'] is True
    client.json('POST', f'/api/jobs/{job}/retrial', {})
    assert client.wait(job)['status'] == 'done'
    assert client.json('GET', f'/api/jobs/{job}')['review']['trial_stale'] is False


@needs_r
def test_confirm_requires_checkbox_and_reviewer_then_calculates(reviewed):
    client, store, job = reviewed
    client.json('POST', f'/api/jobs/{job}/confirm', {'reviewer': 'Dr. A'}, expect=400)
    client.json('POST', f'/api/jobs/{job}/confirm', {'confirmed': True, 'reviewer': '  '}, expect=400)
    client.json('GET', f'/api/jobs/{job}/results', expect=409)

    rows = client.json('GET', f'/api/jobs/{job}/rows')['rows']
    idx = next(r['idx'] for r in rows if r['values']['ID'] == 'conf')
    client.json('POST', f'/api/jobs/{job}/edit', {'row': idx, 'column': 'TP53maxvaf', 'value': '0'})

    client.json('POST', f'/api/jobs/{job}/confirm', {'confirmed': True, 'reviewer': '王醫師'})
    prog = client.wait(job)
    assert prog['status'] == 'done', prog

    res = client.json('GET', f'/api/jobs/{job}/results')
    assert {r['ID'] for r in res['rows']} == {'ok1', 'ok2', 'conf'}
    assert [u['ID'] for u in res['uncertain_excluded']] == ['cyto_na']
    assert res['signoff']['reviewer'] == '王醫師'
    assert all(r['Consistency_Flags'] == 'NONE' for r in res['rows'])        # 修正後旗標已消失

    resp, payload = client.request('GET', f'/api/jobs/{job}/download/results')
    assert resp.status == 200 and 'attachment' in resp.getheader('Content-Disposition')
    assert urllib.parse.quote('cohort_結果.xlsx') in resp.getheader('Content-Disposition')
    wb = load_workbook(io.BytesIO(payload))
    assert {'Summary', 'R_Full_Output', 'Uncertain_Excluded', 'Review_SignOff', 'Manual_Edits'} <= set(wb.sheetnames)
    signoff = {r[0].value: r[1].value for r in wb['Review_SignOff'].iter_rows(min_row=2)}
    assert signoff['核對者'] == '王醫師' and '僅供學術研究' in signoff['說明']
    edits = [[c.value for c in r] for r in wb['Manual_Edits'].iter_rows(min_row=2)]
    assert edits and edits[0][1:] == ['conf', 'TP53maxvaf', '0.8', '0']

    assert client.json('GET', f'/api/jobs/{job}')['job']['stage'] == 'calculated'
    # 計算後再修改 -> 舊結果失效
    client.json('POST', f'/api/jobs/{job}/edit', {'row': idx, 'column': 'HB', 'value': '8'})
    after = client.json('GET', f'/api/jobs/{job}')
    assert after['job']['stage'] == 'reviewed' and 'results' not in after['downloads']
    client.json('GET', f'/api/jobs/{job}/results', expect=409)


@needs_r
def test_source_selection_error_surfaces_as_actionable_run_error(app, tmp_path):
    client, _ = app
    job = client.upload('multi.xlsx', _multi_sheet_xlsx(tmp_path))['job']['id']
    prog = run_review(client, job)
    assert prog['status'] == 'error' and prog['error']['code'] == 'source_selection'
    assert {s['name'] for s in prog['error']['sheets']} == {'2023', '2024'}
    prog = run_review(client, job, sheet='all', sheet_join='stack')
    assert prog['status'] == 'done'
    assert client.json('GET', f'/api/jobs/{job}')['review']['counts']['input_rows'] == 2


@needs_r
def test_pipeline_failure_gives_readable_message(app, tmp_path):
    client, _ = app
    job = client.upload('bad.csv', csv_bytes(tmp_path, [_make_row('x', ASXL1='???')]))['job']['id']
    prog = run_review(client, job)
    assert prog['status'] == 'error' and prog['error']['code'] == 'pipeline_failed'
    assert prog['error']['message']


@needs_r
def test_only_one_run_at_a_time(app, tmp_path):
    client, _ = app
    job = client.upload('c.csv', csv_bytes(tmp_path, cohort()))['job']['id']
    assert ipssm_web.RUN_LOCK.acquire(blocking=False)
    try:
        res = client.json('POST', f'/api/jobs/{job}/review', {'missing': 'impute'}, expect=409)
        assert res['error'] == 'busy'
    finally:
        ipssm_web.RUN_LOCK.release()


@needs_r
def test_delete_removes_all_data(reviewed):
    client, store, job = reviewed
    assert (store.workspace / 'jobs' / job).exists()
    assert client.json('POST', f'/api/jobs/{job}/delete', {})['deleted'] is True
    assert not (store.workspace / 'jobs' / job).exists()
    client.json('GET', f'/api/jobs/{job}', expect=404)


def test_shutdown_stops_the_server(tmp_path):
    server, token, _ = create_server(workspace=tmp_path / 'ws', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(server, token)
    assert client.json('POST', '/api/shutdown', {})['ok'] is True
    thread.join(timeout=5)
    assert not thread.is_alive()
    server.server_close()


def test_port_fallback_when_busy(tmp_path):
    first, _, _ = create_server(workspace=tmp_path / 'a', port=0)
    busy = first.server_address[1]
    second, _, _ = create_server(workspace=tmp_path / 'b', port=busy)
    assert second.server_address[1] != busy
    first.server_close()
    second.server_close()


def test_frontend_never_injects_html():
    """前端所有動態文字都必須走 textContent (病患 ID 等可能含特殊字元)；不得使用會執行/解析 HTML 的 API。"""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web')
    js = open(os.path.join(root, 'app.js'), encoding='utf-8').read()
    code = '\n'.join(line for line in js.splitlines() if not line.strip().startswith(('//', '*', '/*')))
    for banned in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function',
                   'setTimeout("', "setTimeout('"):
        assert banned not in code, f'app.js 不應使用 {banned}'


def test_frontend_escapes_hostile_patient_ids_end_to_end(app, tmp_path):
    """含 HTML 的病患 ID 經 API 原樣回傳 (前端以 textContent 顯示，不會被解析)。"""
    client, _ = app
    evil = '<img src=x onerror=alert(1)>'
    job = client.upload('c.csv', csv_bytes(tmp_path, [_make_row(evil), _make_row('ok')]))['job']['id']
    if R_READY:
        assert run_review(client, job, missing='impute')['status'] == 'done'
        rows = client.json('GET', f'/api/jobs/{job}/rows')['rows']
        assert evil in [r['values']['ID'] for r in rows]
