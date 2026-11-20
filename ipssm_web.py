#!/usr/bin/env python3
"""IPSS-M 本地網頁介面 — 給不使用終端機的一般使用者。

在自己的電腦上啟動一個只有本機能連的網頁 (http://127.0.0.1:埠號)，用瀏覽器完成：
  上傳檔案 -> 設定 -> 整理與檢查 -> 核對/修改標準欄位資料 -> 確認 -> 計算並下載結果。
所有計算仍由 ipssm_pipeline.py 與官方 R 套件完成，網頁只是外殼，不複製任何邏輯。

安全性 (資料含病患資訊，務必只留在本機)
-------------------------------------
* 只綁定 127.0.0.1，不接受來自網路上其他電腦的連線；沒有任何外部資源 (不載入 CDN/字型)。
* 啟動時產生隨機 token，只有用啟動程式開啟的網址 (含 token) 才能使用；token 存在
  SameSite=Strict 的 cookie 裡，其他網站無法代替使用者呼叫本服務 (CSRF)。
* 檢查 Host 標頭，防止 DNS rebinding；所有寫入類請求需帶自訂標頭 (跨站無法送出)。
* 工作資料夾 = --workspace (預設 ~/IPSSM_Web_Workspace)，每次上傳一個獨立資料夾，
  代號是隨機 32 碼十六進位，下載只允許固定白名單的檔名，不接受任何路徑。
* LLM 的 API key 只存在記憶體 (這次執行)，不寫入磁碟或日誌。
* 上傳前必須確認「使用條款與合規」(伺服器端強制，並記錄於結果檔的 Review_SignOff)。
"""
import argparse
import contextlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

import ipssm_env as E
import ipssm_pipeline as P
from ipssm_checks import check_consistency, flag_ids
from ipssm_llm import LLMConfig, LLMResolver
from ipssm_source import SourceSelectionError

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / 'web'
DEFAULT_WORKSPACE = E.DEFAULT_WORKSPACE
TERMS_VERSION = 1
DEFAULT_MAX_UPLOAD_MB = 100
ALLOWED_EXT = {'.xlsx', '.xlsm', '.csv', '.tsv', '.txt'}
JOB_ID_RE = re.compile(r'^[0-9a-f]{32}$')
COOKIE_NAME = 'ipssm_token'
REQUEST_HEADER = 'X-IPSSM-Request'

STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
    '/app.css': ('app.css', 'text/css; charset=utf-8'),
}

# 下載白名單: kind -> (磁碟檔名, 給使用者看的檔名後綴, MIME)
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
DOWNLOADS = {
    'results': ('data_standardized_results.xlsx', '結果', XLSX),
    'review': ('data_review.xlsx', '核對檔', XLSX),
    'standardized': ('data_standardized.csv', '標準欄位資料', 'text/csv; charset=utf-8'),
    'audit': ('data_field_audit.csv', '逐欄位紀錄', 'text/csv; charset=utf-8'),
    'log': ('data_screening_log.txt', '驗證日誌', 'text/plain; charset=utf-8'),
}

RUN_LOCK = threading.Lock()          # 同一時間只跑一個工作 (pipeline 會寫到 stdout)
RUNS = {}                            # job_id -> RunState (記憶體內)
JOB_LOCKS = {}                       # job_id -> Lock (保護 CSV/JSON 讀寫)
_ENV_CACHE = {'at': 0.0, 'value': None, 'workspace': None}
INSTALL = {'run': None}               # 目前/最近一次 ipssm 安裝的 RunState
ENV_TTL = 60


class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


# ---------------------------------------------------------------------------
#  執行狀態 / 日誌擷取
# ---------------------------------------------------------------------------

class LiveLog:
    """擷取 pipeline 的 print 輸出，供介面顯示進度。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._parts = []
        self.stage_no = 0

    def write(self, s):
        with self._lock:
            self._parts.append(s)
            for m in re.finditer(r'\[階段 (\d)\]', s):
                self.stage_no = max(self.stage_no, int(m.group(1)))
        return len(s)

    def flush(self):
        pass

    def text(self):
        with self._lock:
            return ''.join(self._parts)


class RunState:
    def __init__(self, label):
        self.label = label
        self.status = 'running'          # running | done | error
        self.error = None                # {'code', 'message', 'info'?}
        self.live = LiveLog()
        self.started = time.time()


# ---------------------------------------------------------------------------
#  工作 (Job) 儲存
# ---------------------------------------------------------------------------

class JobStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        (self.workspace / 'jobs').mkdir(parents=True, exist_ok=True)

    def dir(self, job_id):
        if not JOB_ID_RE.match(job_id or ''):
            raise ApiError(404, 'not_found', '找不到這個工作')
        d = self.workspace / 'jobs' / job_id
        if not d.is_dir():
            raise ApiError(404, 'not_found', '找不到這個工作 (資料可能已被清除)')
        return d

    def lock(self, job_id):
        return JOB_LOCKS.setdefault(job_id, threading.Lock())

    def create(self, original_name, data):
        job_id = uuid.uuid4().hex
        d = self.workspace / 'jobs' / job_id
        d.mkdir(parents=True)
        ext = Path(original_name).suffix.lower()
        (d / f'data{ext}').write_bytes(data)
        state = {
            'id': job_id, 'original_name': original_name, 'size': len(data), 'ext': ext,
            'created': datetime.now().isoformat(timespec='seconds'), 'stage': 'uploaded',
            'options': None, 'acks': {}, 'edits': [], 'trial_stale': False, 'signoff': None,
        }
        self.save_state(job_id, state)
        return state

    def input_path(self, job_id):
        state = self.load_state(job_id)
        return self.dir(job_id) / f"data{state['ext']}"

    def load_state(self, job_id):
        return json.loads((self.dir(job_id) / 'state.json').read_text(encoding='utf-8'))

    def save_state(self, job_id, state):
        path = self.workspace / 'jobs' / job_id / 'state.json'
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')

    def load_review(self, job_id):
        path = self.dir(job_id) / 'review_data.json'
        if not path.exists():
            raise ApiError(409, 'not_ready', '還沒有完成「整理與檢查」')
        return json.loads(path.read_text(encoding='utf-8'))

    def save_review(self, job_id, data):
        (self.dir(job_id) / 'review_data.json').write_text(
            json.dumps(data, ensure_ascii=False, default=str), encoding='utf-8')

    def delete(self, job_id):
        d = self.dir(job_id)
        shutil.rmtree(d)
        RUNS.pop(job_id, None)
        JOB_LOCKS.pop(job_id, None)


# ---------------------------------------------------------------------------
#  R 環境狀態
# ---------------------------------------------------------------------------

def env_status(force=False):
    """完整環境檢查 (ipssm_env.check_environment)。快取 60 秒，因為檢查 ipssm 要實際啟動 R 算一次範例。"""
    now = time.time()
    if not force and _ENV_CACHE['value'] and now - _ENV_CACHE['at'] < ENV_TTL:
        return _ENV_CACHE['value']
    value = E.check_environment(_ENV_CACHE['workspace'], include_browser=False)
    _ENV_CACHE.update(at=now, value=value)
    return value


def r_status(force=False):
    """計算所需的 R 環境是否可用 (R 與 ipssm 套件)。"""
    env = env_status(force)
    by_id = {c['id']: c for c in env['checks']}
    installed = by_id['ipssm']['ok']
    return {'rscript': env['rscript'], 'ipssm_installed': installed, 'ready': bool(env['rscript'] and installed)}


def api_install_start():
    """自動安裝 R 套件 ipssm (使用者在網頁按下按鈕才會執行)。背景執行，可輪詢進度。"""
    env = env_status(force=True)
    if not env['rscript']:
        raise ApiError(409, 'r_missing', '找不到 R，無法自動安裝。請先安裝 R。')
    if not RUN_LOCK.acquire(blocking=False):
        raise ApiError(409, 'busy', '目前有另一個工作正在執行，請稍候。')
    run = RunState('install')
    INSTALL['run'] = run

    def work():
        try:
            ok = E.install_ipssm(env['rscript'], log=lambda line: run.live.write(line + '\n'))
            run.status = 'done' if ok else 'error'
            if not ok:
                run.error = {'code': 'install_failed', 'message': _install_failure_hint(run.live.text())}
        except Exception as e:
            run.status, run.error = 'error', {'code': 'install_failed', 'message': f'安裝時發生錯誤：{e}'}
        finally:
            _ENV_CACHE['value'] = None
            RUN_LOCK.release()

    threading.Thread(target=work, daemon=True).start()
    return {'started': True}


def _install_failure_hint(log):
    """把安裝失敗的原因翻成使用者看得懂的話 (最常見的是連不到網路)。"""
    low = log.lower()
    if any(k in low for k in ('could not resolve', 'cannot open url', 'timed out', 'is not available for this version',
                              'unable to access', 'curl', 'download', 'http error', 'rate limit', 'ssl', 'proxy',
                              'connection')):
        return ('安裝失敗，最可能的原因是連不到 GitHub 或 CRAN：網路中斷、被防火牆或代理擋下，'
                '或 GitHub 暫時限流（HTTP 403，稍後再試即可）。若是離線環境，請改用「離線安裝」(見 README)。'
                '詳細原因請展開日誌。')
    if 'not writable' in low or 'permission' in low or '無法寫入' in low:
        return '安裝失敗：沒有寫入 R 套件資料夾的權限。請展開日誌查看。'
    if 'compilation failed' in low or 'configuration failed' in low or 'non-zero exit status' in low:
        return '安裝失敗：套件編譯時出錯，通常是缺少系統函式庫。請展開日誌查看缺少什麼。'
    return '安裝失敗。請展開日誌查看原因。'


def api_install_progress():
    run = INSTALL['run']
    if run is None:
        return {'status': 'idle'}
    return {'status': run.status, 'log': run.live.text()[-6000:], 'error': run.error,
            'seconds': int(time.time() - run.started)}


# ---------------------------------------------------------------------------
#  API 實作 (每個函式回傳可序列化的 dict；失敗拋 ApiError)
# ---------------------------------------------------------------------------

def _clean_filename(name):
    name = os.path.basename((name or '').replace('\\', '/')).strip()
    name = re.sub(r'[\x00-\x1f]', '', name)
    return name[:150] or 'upload'


def _inspect(store, job_id, sheet=None, sheet_join=None):
    path = store.input_path(job_id)
    try:
        res = P.inspect_columns(str(path), sheet or None, sheet_join or None)
    except SourceSelectionError as e:
        info = getattr(e, 'info', None) or {}
        return {'ok': False, 'ambiguous': bool(info), 'message': str(e),
                'sheets': info.get('sheets', []), 'source': info or None}
    except Exception as e:                         # 檔案壞掉、格式不支援等
        raise ApiError(400, 'unreadable', f'無法讀取這個檔案：{e}')
    return {
        'ok': True, 'cohort_type': res['cohort_type'], 'matched': res['matched'],
        'missing_required': res['missing_required'], 'missing_optional': res['missing_optional'],
        'unmapped': res['unmapped_input_columns'], 'warnings': res['warnings'],
        'source': res['source'], 'sheets': res['source']['sheets'],
    }


def api_status(store, force=False):
    env = env_status(force)
    installing = INSTALL['run'] is not None and INSTALL['run'].status == 'running'
    return {'r': r_status(), 'env': env, 'installing': installing, 'workspace': str(store.workspace),
            'version': 1, 'terms_version': TERMS_VERSION}


def api_upload(store, name, data, max_bytes, terms=False):
    if not terms:
        raise ApiError(400, 'terms_required', '請先勾選「使用條款與合規確認」才能上傳。')
    name = _clean_filename(name)
    if Path(name).suffix.lower() == '.xls':
        raise ApiError(400, 'legacy_xls', '舊版 .xls 格式不支援，請在 Excel 另存為 .xlsx 後再上傳。')
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        raise ApiError(400, 'bad_type', '只支援 .xlsx / .xlsm / .csv / .tsv 檔案。')
    if not data:
        raise ApiError(400, 'empty', '檔案是空的。')
    if len(data) > max_bytes:
        raise ApiError(413, 'too_large', f'檔案超過 {max_bytes // (1024 * 1024)} MB 上限。')
    state = store.create(name, data)
    state['terms_accepted_at'] = datetime.now().isoformat(timespec='seconds')
    state['terms_version'] = TERMS_VERSION
    store.save_state(state['id'], state)
    try:
        inspect = _inspect(store, state['id'])
    except ApiError:
        store.delete(state['id'])               # 讀不了的檔案不留在工作資料夾
        raise
    return {'job': state, 'inspect': inspect}


def _llm_from_options(llm):
    """依介面送來的設定建立 LLMResolver；未啟用回傳 None。設定錯誤拋 ApiError。"""
    if not llm or not llm.get('enabled'):
        return None
    config = LLMConfig(
        provider=llm.get('provider') or 'openai', endpoint=(llm.get('endpoint') or '').strip(),
        model=(llm.get('model') or '').strip(), api_key=llm.get('api_key') or '',
        min_confidence=float(llm.get('min_confidence') or 0.8),
        allow_remote=bool(llm.get('allow_remote')), timeout=30.0)
    try:
        return LLMResolver(config)
    except ValueError as e:
        raise ApiError(400, 'llm_config', str(e))


def api_llm_test(body):
    """用一段與病患無關的假文字測試 LLM 設定是否可用。"""
    resolver = _llm_from_options({**body, 'enabled': True})
    result = resolver.resolve('ASXL1', 'positive', 'binary', ['0', '1'])
    return {'ok': result.accepted, 'is_remote': resolver.is_remote, 'value': result.value,
            'confidence': result.confidence, 'error': result.error}


def _validate_options(body):
    missing = body.get('missing', P.DEFAULT_MISSING_MODE)
    if missing not in P.MISSING_MODES:
        raise ApiError(400, 'bad_option', '缺失值處理方式不正確')
    try:
        max_range = float(body.get('max_range', P.DEFAULT_MAX_RANGE))
    except (TypeError, ValueError):
        raise ApiError(400, 'bad_option', '範圍門檻必須是數字')
    if not 0 <= max_range <= 20:
        raise ApiError(400, 'bad_option', '範圍門檻必須介於 0 到 20')
    join = body.get('sheet_join') or None
    if join not in (None,) + tuple(P.SHEET_JOINS):
        raise ApiError(400, 'bad_option', '分頁合併方式不正確')
    sheet = (body.get('sheet') or '').strip() or None
    return {'missing': missing, 'max_range': max_range, 'sheet': sheet, 'sheet_join': join}


def _start_run(job_id, label, target):
    """在背景執行 target()；同一時間只允許一個工作。"""
    if not RUN_LOCK.acquire(blocking=False):
        raise ApiError(409, 'busy', '目前有另一個計算正在執行，請稍候。')
    run = RunState(label)
    RUNS[job_id] = run

    def work():
        try:
            with contextlib.redirect_stdout(run.live):
                target()
            run.status = 'done'
        except SourceSelectionError as e:
            run.status, run.error = 'error', {'code': 'source_selection', 'message': str(e),
                                              'sheets': (getattr(e, 'info', None) or {}).get('sheets', [])}
        except ApiError as e:
            run.status, run.error = 'error', {'code': e.code, 'message': e.message}
        except Exception as e:
            run.live.write('\n' + traceback.format_exc())
            run.status, run.error = 'error', {'code': 'internal', 'message': f'處理時發生錯誤：{e}'}
        finally:
            RUN_LOCK.release()

    threading.Thread(target=work, daemon=True).start()
    return run


def api_progress(store, job_id):
    store.dir(job_id)
    run = RUNS.get(job_id)
    if run is None:
        return {'status': 'idle'}
    text = run.live.text()
    return {'status': run.status, 'label': run.label, 'stage_no': run.live.stage_no,
            'error': run.error, 'log': text[-6000:], 'seconds': int(time.time() - run.started)}


def _asdict_llm(results):
    return [asdict(r) for r in results]


def _build_review_data(outcome, options, resolver):
    report = outcome['report']
    rows = outcome['rows']
    missing_check = outcome['missing_check']
    included_ids = {r['ID'] for r in rows}
    sheet = outcome['uncertain_sheet']
    kinds = {}
    for ex in report.excluded:
        kinds[ex['Source']] = kinds.get(ex['Source'], 0) + 1
    audit = [{'id': f'a{i}', **e} for i, e in enumerate(report.cell_audit)]
    consistency = [{'id': f'k{i}', **c} for i, c in enumerate(report.consistency)]
    return {
        'mode': options['missing'], 'max_range': options['max_range'],
        'trial_available': any(m['Confidence_Level'] != 'NOT_COMPUTED' for m in missing_check),
        'input_rows': report.input_rows, 'included': len(rows),
        'excluded_kinds': kinds, 'missing_check': missing_check,
        'excluded': report.excluded, 'audit': audit, 'consistency': consistency,
        'seq': len(consistency),
        'source': {'info': report.source_info, 'warnings': report.source_warnings},
        'llm': _asdict_llm(report.llm_results),
        'llm_info': P._build_llm_info(resolver),
        'uncertain_sheet': ({'name': sheet[0], 'headers': list(sheet[1]), 'rows': sheet[2]}
                            if sheet else None),
        'included_ids': sorted(included_ids),
    }


def api_start_review(store, job_id, body):
    options = _validate_options(body)
    state = store.load_state(job_id)
    resolver = _llm_from_options(body.get('llm'))
    path = str(store.input_path(job_id))
    job_dir = store.dir(job_id)

    def target():
        # 輸出檔固定放在工作資料夾 (data_*.csv / data_review.xlsx …)
        outcome = P.run_review_pipeline(path, options['missing'], options['max_range'], None,
                                        resolver, options['sheet'], options['sheet_join'])
        if outcome is None:
            raise ApiError(422, 'pipeline_failed', _last_error_line(RUNS[job_id].live.text()))
        review = _build_review_data(outcome, options, resolver)
        with store.lock(job_id):
            store.save_review(job_id, review)
            state.update(stage='reviewed', options={**options, 'llm': _public_llm(body.get('llm'))},
                         acks={}, edits=[], trial_stale=False, signoff=None)
            store.save_state(job_id, state)
            for stale in ('data_standardized_results.xlsx',):
                (job_dir / stale).unlink(missing_ok=True)

    _start_run(job_id, 'review', target)
    return {'started': True}


def _public_llm(llm):
    """寫入 state.json 的 LLM 設定 (不含 API key)。"""
    if not llm or not llm.get('enabled'):
        return None
    return {k: llm.get(k) for k in ('provider', 'endpoint', 'model', 'min_confidence', 'allow_remote')}


def _last_error_line(log):
    lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if 'ERROR' in ln or '錯誤' in ln or '沒有任何有效資料列' in ln:
            return re.sub(r'^\[ERROR\]\s*', '', ln)
    return lines[-1] if lines else '處理失敗，請查看詳細日誌。'


# ---- 核對資料 ---------------------------------------------------------------

def _read_std_rows(store, job_id):
    path = store.dir(job_id) / 'data_standardized.csv'
    if not path.exists():
        raise ApiError(409, 'not_ready', '還沒有標準欄位資料')
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df.to_dict('records')


def _write_std_rows(store, job_id, rows):
    P._write_standardized_csv(store.dir(job_id) / 'data_standardized.csv', rows)


def _todo_items(review, acks):
    """需要核對者確認的項目: 已套用但標為「需確認」的儲存格 + 一致性矛盾/提示。"""
    included = set(review['included_ids'])
    items = []
    for e in review['audit']:
        if e['Status'] == 'needs_review' and e['ID'] in included:
            items.append({'id': e['id'], 'kind': 'cell', 'patient': e['ID'], 'row': e['Row'],
                          'column': e['Column'], 'original': e['Original'], 'normalized': e['Normalized'],
                          'rule': e['Rule'], 'source': e['Source'], 'confidence': e['Confidence'],
                          'note': e['Note'], 'acked': bool(acks.get(e['id']))})
    for c in review['consistency']:
        items.append({'id': c['id'], 'kind': 'conflict' if c['Severity'] == 'conflict' else 'note',
                      'patient': c['ID'], 'row': c['Row'], 'rule': c['Rule'], 'fields': c['Fields'],
                      'message': c['Message'], 'acked': bool(acks.get(c['id']))})
    return items


def api_job(store, job_id):
    state = store.load_state(job_id)
    out = {'job': state, 'run': api_progress(store, job_id)}
    path = store.dir(job_id) / 'review_data.json'
    if path.exists() and state['stage'] in ('reviewed', 'calculated'):
        review = json.loads(path.read_text(encoding='utf-8'))
        out['review'] = _review_view(review, state)
    out['downloads'] = [k for k, (fn, _, _) in DOWNLOADS.items() if (store.dir(job_id) / fn).exists()]
    return out


def _review_view(review, state):
    items = _todo_items(review, state.get('acks', {}))
    kinds = review['excluded_kinds']
    mc = review['missing_check']
    conflicts = [i for i in items if i['kind'] == 'conflict']
    return {
        'mode': review['mode'], 'max_range': review['max_range'],
        'trial_available': review['trial_available'], 'trial_stale': state.get('trial_stale', False),
        'counts': {
            'input_rows': review['input_rows'], 'passed': len(mc), 'included': review['included'],
            'excluded_validation': kinds.get('validation', 0), 'excluded_missing': kinds.get('missing', 0),
            'excluded_uncertain': kinds.get('uncertain', 0),
            'uncertain_trial': sum(1 for m in mc if m['Confidence_Level'] == 'UNCERTAIN'),
            'with_missing': sum(1 for m in mc if m['Missing_Fields'] != 'NONE'),
            'conflicts': len(conflicts), 'conflict_patients': len({i['patient'] for i in conflicts}),
            'notes': sum(1 for i in items if i['kind'] == 'note'),
            'needs_review_cells': sum(1 for i in items if i['kind'] == 'cell'),
            'llm_applied': sum(1 for e in review['audit'] if e['Source'] == 'llm'),
        },
        'items': items,
        'missing_check': mc, 'excluded': review['excluded'],
        'source': review['source'], 'llm': review['llm'], 'llm_info': review['llm_info'],
        'edits': state.get('edits', []),
        'unresolved': [e for e in review['audit'] if e['Status'] == 'unresolved'],
    }


def api_rows(store, job_id, query):
    review = store.load_review(job_id)
    rows = _read_std_rows(store, job_id)
    q = (query.get('q', [''])[0] or '').strip().lower()
    flagged_only = query.get('flagged', ['0'])[0] == '1'
    offset = max(0, int(query.get('offset', ['0'])[0]))
    limit = min(200, max(1, int(query.get('limit', ['50'])[0])))

    cell_info = {}                                   # 病患 ID -> {欄位: 稽核資訊}
    for e in review['audit']:
        if e['Status'] in ('needs_review', 'user_edited', 'auto_applied') and e['Rule'] != 'na_alias':
            cell_info.setdefault(e['ID'], {}).setdefault(e['Column'], {
                'status': e['Status'], 'original': e['Original'], 'rule': e['Rule']})

    out = []
    for idx, row in enumerate(rows):
        missing = P.missing_model_fields(row)
        flags = check_consistency(row)
        info = cell_info.get(row['ID'], {})
        flagged = bool(missing or flags or any(v['status'] == 'needs_review' for v in info.values()))
        if q and q not in row['ID'].lower():
            continue
        if flagged_only and not flagged:
            continue
        out.append({'idx': idx, 'values': row, 'missing': missing, 'flags': [f['Rule'] for f in flags],
                    'cells': info})
    total = len(out)
    return {'total': total, 'offset': offset, 'columns': P.STANDARD_COLUMNS,
            'rows': out[offset:offset + limit]}


AUDIT_LIMIT = 5000


def api_audit(store, job_id, query):
    """逐儲存格整理紀錄。status: needs_review (進入計算的病人、需確認) / unresolved / all。"""
    review = store.load_review(job_id)
    status = query.get('status', ['needs_review'])[0]
    included = set(review['included_ids'])
    rows = review['audit']
    if status == 'needs_review':
        rows = [e for e in rows if e['Status'] in ('needs_review', 'user_edited') and e['ID'] in included]
    elif status == 'unresolved':
        rows = [e for e in rows if e['Status'] == 'unresolved']
    elif status != 'all':
        raise ApiError(400, 'bad_option', '不支援的篩選')
    return {'total': len(rows), 'rows': rows[:AUDIT_LIMIT], 'truncated': len(rows) > AUDIT_LIMIT}


def _check_cell_edit(column, value):
    """驗證手動修改的值，回傳 (正規化後的值, None) 或 (None, 錯誤訊息)。"""
    norm = P._normalize_cell(column, value)
    if norm['status'] == 'unresolved':
        return None, f'無法解析「{value}」。{_expected_hint(column)}'
    row = {c: 'NA' for c in P.STANDARD_COLUMNS}
    row['ID'] = 'edit'
    row[column] = norm['value']
    tmp = P.ValidationReport()
    P._validate_row(2, row, tmp, 'impute')
    errors = [e for e in tmp.errors if f', {column}:' in e]
    if errors:
        return None, _friendly_error(column, value, errors[0].split(': ', 1)[-1])
    return norm['value'], None


def _friendly_error(column, value, raw):
    """把驗證器的英文訊息換成使用者看得懂的中文。"""
    m = re.search(r'out of range \[([\d.]+)-([\d.]+)\]', raw)
    if m:
        return f'{column} 的值 {value} 超出允許範圍（{m.group(1)} 到 {m.group(2)}）。'
    if 'Invalid number' in raw:
        return f'「{value}」不是有效的數字。{_expected_hint(column)}'
    if 'Binary field' in raw:
        return f'{column} 只能填 0（沒有）、1（有）或 NA（未知）。'
    if 'Invalid category' in raw or 'Invalid value' in raw:
        return f'「{value}」不是這個欄位允許的值。{_expected_hint(column)}'
    return f'{raw}。{_expected_hint(column)}'


def _expected_hint(column):
    kind = P._field_kind(column)
    if kind == 'binary':
        return '請輸入 0（沒有）、1（有）或 NA（未知）。'
    if kind == 'cyto':
        return '請輸入 Very Good / Good / Intermediate / Poor / Very Poor 或 NA。'
    if kind == 'tp53mut':
        return '請輸入 0 / 1 / 2 or more 或 NA。'
    rule = P.VALIDATION_RULES.get(column)
    if rule and 'min' in rule:
        return f"請輸入 {rule['min']} 到 {rule['max']} 之間的數字，或 NA。"
    return ''


def api_edit(store, job_id, body):
    column, value = body.get('column'), str(body.get('value', '')).strip()
    idx = body.get('row')
    if column not in P.STANDARD_COLUMNS or column == 'ID':
        raise ApiError(400, 'bad_column', '這個欄位不能修改')
    with store.lock(job_id):
        state = store.load_state(job_id)
        review = store.load_review(job_id)
        rows = _read_std_rows(store, job_id)
        if not isinstance(idx, int) or not 0 <= idx < len(rows):
            raise ApiError(400, 'bad_row', '找不到這位病患')
        new_value, err = _check_cell_edit(column, value)
        if err:
            raise ApiError(422, 'invalid_value', err)
        row = rows[idx]
        old = row[column]
        if old == new_value:
            return {'changed': False, 'value': new_value}
        row[column] = new_value
        _write_std_rows(store, job_id, rows)

        pid = row['ID']
        state['edits'].append({'time': datetime.now().isoformat(timespec='seconds'), 'patient': pid,
                               'column': column, 'old': old, 'new': new_value})
        state['trial_stale'] = True
        if state['stage'] == 'calculated':               # 修改後，先前的計算結果已失效
            state['stage'] = 'reviewed'
            (store.dir(job_id) / DOWNLOADS['results'][0]).unlink(missing_ok=True)
            state['signoff'] = None
        # 該格先前的「需確認」紀錄視為已由使用者處理
        for e in review['audit']:
            if e['ID'] == pid and e['Column'] == column and e['Status'] in ('needs_review', 'unresolved'):
                e['Status'], e['Source'] = 'user_edited', 'user'
                state['acks'][e['id']] = True
        # 重新計算該病患的缺失與一致性旗標
        review['consistency'] = [c for c in review['consistency'] if c['ID'] != pid]
        for f in check_consistency(row):
            review['seq'] += 1
            review['consistency'].append({'id': f"k{review['seq']}", 'Row': '-', 'ID': pid, **f})
        included_idx = [i for i, m in enumerate(review['missing_check']) if m['Decision'] == 'INCLUDE']
        mc = review['missing_check'][included_idx[idx]]
        fields = P.missing_model_fields(row)
        mc['Missing_Fields'] = ', '.join(fields) if fields else 'NONE'
        mc['Missing_Groups'] = P.missing_groups(fields)
        mc['Key_Missing_Fields'] = ', '.join(f for f in fields if f in P.STRONG_MISSING_FIELDS) or 'NONE'
        mc['Consistency_Flags'] = flag_ids(check_consistency(row))
        store.save_review(job_id, review)
        store.save_state(job_id, state)
        return {'changed': True, 'value': new_value, 'old': old}


def api_ack(store, job_id, body):
    ids = body.get('ids') or []
    value = bool(body.get('value', True))
    with store.lock(job_id):
        state = store.load_state(job_id)
        review = store.load_review(job_id)
        valid = {e['id'] for e in review['audit']} | {c['id'] for c in review['consistency']}
        for i in ids:
            if i in valid:
                if value:
                    state['acks'][i] = True
                else:
                    state['acks'].pop(i, None)
        store.save_state(job_id, state)
    return {'acks': len(state['acks'])}


def api_retrial(store, job_id):
    """依目前 (可能已被修改的) 標準欄位資料重新試算，更新缺失檢核表。"""
    status = r_status()
    if not status['ready']:
        raise ApiError(409, 'r_missing', '找不到 R 或 ipssm 套件，無法試算。')
    rows = _read_std_rows(store, job_id)

    def target():
        trial = P.run_trial_calculation(rows, status['rscript'])
        if trial is None:
            raise ApiError(422, 'trial_failed', '試算失敗，請查看詳細日誌。')
        fresh = P.build_missing_check(rows, trial)
        with store.lock(job_id):
            review = store.load_review(job_id)
            state = store.load_state(job_id)
            included_idx = [i for i, m in enumerate(review['missing_check']) if m['Decision'] == 'INCLUDE']
            for pos, new in zip(included_idx, fresh):
                new['Decision'] = 'INCLUDE'
                review['missing_check'][pos] = new
            review['trial_available'] = True
            state['trial_stale'] = False
            store.save_review(job_id, review)
            store.save_state(job_id, state)

    _start_run(job_id, 'retrial', target)
    return {'started': True}


# ---- 確認並正式計算 -----------------------------------------------------------

def _read_results(store, job_id):
    path = store.dir(job_id) / DOWNLOADS['results'][0]
    if not path.exists():
        raise ApiError(409, 'not_ready', '還沒有計算結果')
    xls = pd.ExcelFile(path)
    full = xls.parse('R_Full_Output', dtype=str, keep_default_na=False)
    cols = ['ID', 'Reported_Score', 'Reported_Cat', 'Confidence_Level', 'IPSSMscore_best',
            'IPSSMscore_worst', 'Range_Score', 'Missing_Fields', 'Consistency_Flags']
    rows = full[[c for c in cols if c in full.columns]].to_dict('records')
    unc = []
    if 'Uncertain_Excluded' in xls.sheet_names:
        unc = xls.parse('Uncertain_Excluded', dtype=str, keep_default_na=False).to_dict('records')
    return {'rows': rows, 'uncertain_excluded': unc}


def api_confirm(store, job_id, body):
    reviewer = (body.get('reviewer') or '').strip()[:80]
    if not body.get('confirmed'):
        raise ApiError(400, 'not_confirmed', '請先勾選「我已核對」')
    if not reviewer:
        raise ApiError(400, 'no_reviewer', '請填寫核對者姓名或代號，會記錄在結果檔中。')
    status = r_status()
    if not status['ready']:
        raise ApiError(409, 'r_missing', '找不到 R 或 ipssm 套件，無法計算。請先依畫面上的說明安裝。')

    state = store.load_state(job_id)
    if not state.get('terms_accepted_at'):
        raise ApiError(400, 'terms_required', '這個工作沒有「使用條款與合規確認」紀錄，請重新上傳並勾選。')
    review = store.load_review(job_id)
    items = _todo_items(review, state.get('acks', {}))
    acked = sum(1 for i in items if i['acked'])
    std_csv = store.dir(job_id) / DOWNLOADS['standardized'][0]

    signoff = [
        ['核對者', reviewer],
        ['確認時間', datetime.now().strftime('%Y-%m-%d %H:%M:%S')],
        ['原始檔案', state['original_name']],
        ['使用條款確認', f"已確認 (v{state.get('terms_version', TERMS_VERSION)}，{state['terms_accepted_at']})：資料已去識別化；結果僅供學術研究"],
        ['缺失值處理', review['mode'] + (f" (範圍門檻 {review['max_range']})" if review['mode'] == 'auto' else '')],
        ['需核對項目', f'{len(items)} 項，其中已標記確認 {acked} 項、未標記 {len(items) - acked} 項'],
        ['手動修改', f"{len(state.get('edits', []))} 處 (見 Manual_Edits)"],
        ['LLM 輔助', review.get('llm_info') or '未啟用'],
        ['說明', '本結果僅供學術研究參考，不得用於臨床診斷或治療決策。'],
    ]
    edits_rows = [[e['time'], e['patient'], e['column'], e['old'], e['new']] for e in state.get('edits', [])]
    extra = [('Review_SignOff', ['項目', '內容'], signoff),
             ('Manual_Edits', ['時間', '病患 ID', '欄位', '原值', '新值'], edits_rows)]
    if review.get('uncertain_sheet'):
        u = review['uncertain_sheet']
        extra.insert(0, (u['name'], u['headers'], u['rows']))

    def target():
        ok = P.run_translation(str(std_csv), status['rscript'], None, extra)
        if not ok:
            raise ApiError(422, 'calc_failed', _last_error_line(RUNS[job_id].live.text()))
        with store.lock(job_id):
            st = store.load_state(job_id)
            st['stage'] = 'calculated'
            st['signoff'] = {'reviewer': reviewer, 'time': signoff[1][1]}
            store.save_state(job_id, st)

    _start_run(job_id, 'calculate', target)
    return {'started': True}


def api_results(store, job_id):
    state = store.load_state(job_id)
    return {**_read_results(store, job_id), 'signoff': state.get('signoff'),
            'original_name': state['original_name']}


def api_delete(store, job_id):
    if job_id in RUNS and RUNS[job_id].status == 'running':
        raise ApiError(409, 'busy', '這個工作還在執行中，請稍候再清除。')
    store.delete(job_id)
    return {'deleted': True}


# ---------------------------------------------------------------------------
#  HTTP 層
# ---------------------------------------------------------------------------

def make_handler(store, token, port, max_bytes, shutdown):
    allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'IPSSMWeb/1'

        def log_message(self, *args):          # 不記錄請求 (避免病患 ID 等出現在日誌)
            pass

        # -- 回應工具 --
        def _send(self, status, body, ctype='application/json; charset=utf-8', headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, default=str).encode('utf-8')
            elif isinstance(body, str):
                body = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, e):
            self._send(e.status, {'error': e.code, 'message': e.message, **e.extra})

        # -- 安全檢查 --
        def _host_ok(self):
            return (self.headers.get('Host') or '') in allowed_hosts

        def _cookie_ok(self):
            raw = self.headers.get('Cookie') or ''
            for part in raw.split(';'):
                k, _, v = part.strip().partition('=')
                if k == COOKIE_NAME and hmac.compare_digest(v, token):
                    return True
            return False

        def _guard(self, write):
            if not self._host_ok():
                raise ApiError(403, 'bad_host', '不允許的來源')
            if not self._cookie_ok():
                raise ApiError(403, 'no_token', '請使用啟動程式開啟的網址，或重新執行啟動程式。')
            if write and self.headers.get(REQUEST_HEADER) != '1':
                raise ApiError(403, 'bad_request', '不允許的請求')

        # -- 路由 --
        def do_GET(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
                if not self._host_ok():
                    raise ApiError(403, 'bad_host', '不允許的來源')

                if path == '/' and 't' in query:           # 首次開啟: 設定 cookie 後導回首頁
                    if not hmac.compare_digest(query['t'][0], token):
                        raise ApiError(403, 'bad_token', '網址已失效，請重新執行啟動程式。')
                    self._send(302, b'', headers={
                        'Location': '/',
                        'Set-Cookie': f'{COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/'})
                    return
                if path in STATIC_FILES:
                    if not self._cookie_ok():
                        raise ApiError(403, 'no_token', '請使用啟動程式開啟的網址，或重新執行啟動程式。')
                    name, ctype = STATIC_FILES[path]
                    self._send(200, (WEB_DIR / name).read_bytes(), ctype)
                    return

                self._guard(write=False)
                self._send_json(self._route_get(path, query))
            except ApiError as e:
                self._error(e)
            except FileNotFoundError:
                self._error(ApiError(404, 'not_found', '找不到'))
            except Exception as e:
                self._error(ApiError(500, 'internal', f'內部錯誤：{e}'))

        def _send_json(self, result):
            if isinstance(result, tuple):             # (bytes, ctype, headers) 供下載
                body, ctype, headers = result
                self._send(200, body, ctype, headers)
            else:
                self._send(200, result)

        def _route_get(self, path, query):
            if path == '/api/status':
                return api_status(store, force=query.get('force', ['0'])[0] == '1')
            if path == '/api/env/install':
                return api_install_progress()
            m = re.fullmatch(r'/api/jobs/([0-9a-f]{32})(/[a-z-]+(?:/[a-z]+)?)?', path)
            if not m:
                raise ApiError(404, 'not_found', '找不到')
            job_id, sub = m.group(1), m.group(2) or ''
            if sub == '':
                return api_job(store, job_id)
            if sub == '/progress':
                return api_progress(store, job_id)
            if sub == '/rows':
                return api_rows(store, job_id, query)
            if sub == '/audit':
                return api_audit(store, job_id, query)
            if sub == '/results':
                return api_results(store, job_id)
            dm = re.fullmatch(r'/download/([a-z]+)', sub)
            if dm:
                return self._download(job_id, dm.group(1))
            raise ApiError(404, 'not_found', '找不到')

        def _download(self, job_id, kind):
            if kind not in DOWNLOADS:
                raise ApiError(404, 'not_found', '找不到這個檔案')
            fname, label, mime = DOWNLOADS[kind]
            path = store.dir(job_id) / fname
            if not path.exists():
                raise ApiError(404, 'not_found', '這個檔案還沒有產生')
            stem = Path(store.load_state(job_id)['original_name']).stem
            friendly = f"{stem}_{label}{path.suffix}"
            fallback = re.sub(r'[^A-Za-z0-9._-]+', '_', f"{stem}_{kind}{path.suffix}")   # 舊瀏覽器用的 ASCII 檔名
            return (path.read_bytes(), mime, {'Content-Disposition':
                    f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{urllib.parse.quote(friendly)}"})

        def do_POST(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
                self._guard(write=True)
                length = int(self.headers.get('Content-Length') or 0)
                if path == '/api/upload':
                    if length > max_bytes:
                        raise ApiError(413, 'too_large', f'檔案超過 {max_bytes // (1024 * 1024)} MB 上限。')
                    data = self.rfile.read(length)
                    name = urllib.parse.unquote(query.get('name', [''])[0])
                    terms = query.get('terms', ['0'])[0] == '1'
                    self._send(200, api_upload(store, name, data, max_bytes, terms))
                    return
                if length > 1024 * 1024:
                    raise ApiError(413, 'too_large', '請求太大')
                raw = self.rfile.read(length) if length else b'{}'
                try:
                    body = json.loads(raw.decode('utf-8') or '{}')
                except ValueError:
                    raise ApiError(400, 'bad_json', '請求格式不正確')
                self._send(200, self._route_post(path, body))
            except ApiError as e:
                self._error(e)
            except Exception as e:
                self._error(ApiError(500, 'internal', f'內部錯誤：{e}'))

        def _route_post(self, path, body):
            if path == '/api/llm/test':
                return api_llm_test(body)
            if path == '/api/env/install':
                return api_install_start()
            if path == '/api/shutdown':
                threading.Thread(target=shutdown, daemon=True).start()
                return {'ok': True}
            m = re.fullmatch(r'/api/jobs/([0-9a-f]{32})/([a-z]+)', path)
            if not m:
                raise ApiError(404, 'not_found', '找不到')
            job_id, action = m.groups()
            store.dir(job_id)
            actions = {
                'inspect': lambda: _inspect_response(store, job_id, body),
                'review': lambda: api_start_review(store, job_id, body),
                'edit': lambda: api_edit(store, job_id, body),
                'ack': lambda: api_ack(store, job_id, body),
                'retrial': lambda: api_retrial(store, job_id),
                'confirm': lambda: api_confirm(store, job_id, body),
                'delete': lambda: api_delete(store, job_id),
            }
            if action not in actions:
                raise ApiError(404, 'not_found', '找不到')
            return actions[action]()

    return Handler


def _inspect_response(store, job_id, body):
    join = body.get('sheet_join') or None
    if join not in (None,) + tuple(P.SHEET_JOINS):
        raise ApiError(400, 'bad_option', '分頁合併方式不正確')
    return _inspect(store, job_id, (body.get('sheet') or '').strip() or None, join)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):      # 瀏覽器中途關閉連線等，不必印出
        pass


def create_server(workspace=DEFAULT_WORKSPACE, port=8765, max_upload_mb=DEFAULT_MAX_UPLOAD_MB,
                  token=None):
    """建立 (但不啟動) 伺服器。port=0 表示自動挑選；回傳 (server, token, store)。"""
    token = token or secrets.token_urlsafe(24)
    store = JobStore(workspace)
    _ENV_CACHE.update(at=0.0, value=None, workspace=str(store.workspace))
    last_error = None
    for candidate in ([port] if port == 0 else range(port, port + 20)):
        try:
            server = QuietServer(('127.0.0.1', candidate), None)
        except OSError as e:
            last_error = e
            continue
        actual = server.server_address[1]
        server.RequestHandlerClass = make_handler(store, token, actual, max_upload_mb * 1024 * 1024,
                                                  lambda: server.shutdown())
        return server, token, store
    raise SystemExit(f'找不到可用的連接埠 ({port}–{port + 19}): {last_error}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='IPSS-M 本地網頁介面 (只有這台電腦能連線)')
    parser.add_argument('--port', type=int, default=8765, help='起始連接埠 (被占用時自動往後找，預設 8765)')
    parser.add_argument('--workspace', default=str(DEFAULT_WORKSPACE),
                        help=f'工作資料夾，上傳的檔案與產生的結果都放在這裡 (預設 {DEFAULT_WORKSPACE})')
    parser.add_argument('--no-browser', action='store_true', help='不要自動開啟瀏覽器')
    parser.add_argument('--max-upload-mb', type=int, default=DEFAULT_MAX_UPLOAD_MB, help='上傳檔案大小上限')
    args = parser.parse_args(argv)

    server, token, store = create_server(args.workspace, args.port, args.max_upload_mb)
    port = server.server_address[1]
    url = f'http://127.0.0.1:{port}/?t={token}'
    print('=' * 62)
    print('  IPSS-M 本地網頁介面已啟動 (只有這台電腦能連線)')
    print('=' * 62)
    print(f'  網址:     {url}')
    print(f'  工作資料夾: {store.workspace}')
    print('  使用完畢請在網頁右上角按「結束程式」，或在此視窗按 Ctrl+C。')
    print('=' * 62)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print('\n  已結束。')


if __name__ == '__main__':
    main()
