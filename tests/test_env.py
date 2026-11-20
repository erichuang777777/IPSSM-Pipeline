"""環境檢查 (ipssm_env) — 用假的 Rscript 確定性地驗證每一種失敗情境的診斷。"""
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ipssm_env as E

REPO = Path(__file__).resolve().parent.parent
posix_only = pytest.mark.skipif(os.name == 'nt', reason='假的 Rscript 是 shell 腳本')


def fake_rscript(tmp_path, mode='ok', version='4.3.3', install_lines=(), install_rc=0, sleep=0):
    """產生假的 Rscript。mode: ok / missing / deps / broken / badversion。"""
    path = tmp_path / 'Rscript'
    probe = {'ok': 'printf "1.0.0 3"; exit 0', 'missing': 'exit 3', 'deps': 'printf "car,readxl"; exit 4',
             'broken': 'printf "object not found"; exit 1'}.get(mode, 'exit 0')
    body = textwrap.dedent(f'''\
        #!/bin/sh
        if [ "$1" = "--version" ]; then
          {'echo "garbage" >&2' if mode == 'badversion' else f'echo "Rscript (R) version {version} (2024-02-29)" >&2'}
          exit 0
        fi
        if [ "$1" = "-e" ]; then {probe}; fi
        # install.R
        {'sleep ' + str(sleep) if sleep else ''}
        {''.join(f'echo "{line}"; ' for line in install_lines)}
        exit {install_rc}
        ''')
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def ids(result):
    return {c['id']: c for c in result['checks']}


# --- 與 requirements.txt 同步 --------------------------------------------------------

def test_python_requirements_match_requirements_txt():
    parsed = {}
    for line in (REPO / 'requirements.txt').read_text().splitlines():
        m = re.match(r'^([A-Za-z0-9_.-]+)\s*>=\s*([\d.]+)', line.strip())
        if m:
            parsed[m.group(1).lower()] = E.version_tuple(m.group(2))
    assert parsed == {name: ver for name, ver in E.PY_PACKAGES}


@pytest.mark.parametrize('text,expected', [('4.3.3', (4, 3, 3)), ('2.0.0rc1', (2, 0, 0)), ('3.1.5', (3, 1, 5)),
                                           ('10', (10,)), ('abc', ())])
def test_version_tuple(text, expected):
    assert E.version_tuple(text) == expected


# --- R ---------------------------------------------------------------------------------

def test_missing_r_gives_os_specific_fix():
    c = E.check_r(None)
    assert not c['ok'] and c['required'] and 'R' in c['fix']
    assert 'cran.r-project.org' in E.R_DOWNLOAD['Windows'] and 'brew' in E.R_DOWNLOAD['Darwin'] and 'apt' in E.R_DOWNLOAD['Linux']


@posix_only
def test_old_r_is_a_warning_not_a_blocker(tmp_path):
    c = E.check_r(fake_rscript(tmp_path, version='4.1.2'))
    assert c['ok'] and c['warn'] and '4.1.2' in c['detail'] and '沒有宣告最低版本' in c['detail']


@posix_only
def test_supported_r_has_no_warning(tmp_path):
    c = E.check_r(fake_rscript(tmp_path, version='4.4.1'))
    assert c['ok'] and not c['warn']


@posix_only
def test_unparseable_r_version_is_a_failure(tmp_path):
    c = E.check_r(fake_rscript(tmp_path, mode='badversion'))
    assert not c['ok'] and '無法執行' in c['detail']


# --- ipssm: 以實際計算判斷，不只是「套件存在」 ---------------------------------------------------

@posix_only
def test_ipssm_ok_reports_version_and_smoke_test(tmp_path):
    c = E.check_ipssm(fake_rscript(tmp_path, 'ok'))
    assert c['ok'] and '1.0.0' in c['detail'] and '實際計算 3 筆' in c['detail']


@posix_only
def test_ipssm_not_installed(tmp_path):
    c = E.check_ipssm(fake_rscript(tmp_path, 'missing'))
    assert not c['ok'] and c['detail'] == '尚未安裝' and c['autofix'] == 'install_ipssm' and 'install.R' in c['fix']


@posix_only
def test_ipssm_installed_but_dependencies_missing_is_not_ready(tmp_path):
    """回歸: 套件能載入、但相依套件 (car) 缺失時，計算會在最後一步失敗；檢查必須先抓到。"""
    c = E.check_ipssm(fake_rscript(tmp_path, 'deps'))
    assert not c['ok'] and 'car、readxl' in c['detail'] and c['fix'].startswith('相依套件不完整')
    assert c['autofix'] == 'install_ipssm'


@posix_only
def test_ipssm_smoke_test_failure_is_reported(tmp_path):
    c = E.check_ipssm(fake_rscript(tmp_path, 'broken'))
    assert not c['ok'] and '無法完成範例計算' in c['detail'] and 'object not found' in c['detail']


def test_ipssm_check_needs_r():
    c = E.check_ipssm(None)
    assert not c['ok'] and '先安裝 R' in c['detail']


# --- 整體判斷 ----------------------------------------------------------------------------

@posix_only
def test_ready_only_when_all_required_pass(tmp_path):
    ok = E.check_environment(None, rscript=fake_rscript(tmp_path, 'ok'), include_browser=False)
    assert ok['ready'] is True and ids(ok)['ipssm']['ok']
    bad = E.check_environment(None, rscript=fake_rscript(tmp_path, 'deps'), include_browser=False)
    assert bad['ready'] is False
    none = E.check_environment(None, rscript='', include_browser=False)
    assert none['ready'] is False and none['rscript'] is None
    assert not ids(none)['ipssm']['ok'] and '先安裝 R' in ids(none)['ipssm']['detail']


@posix_only
def test_optional_and_warning_items_do_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(E, 'check_browser', lambda: E._check('browser', '預設瀏覽器', False, '找不到', required=False))
    r = E.check_environment(None, rscript=fake_rscript(tmp_path, 'ok', version='4.0.5'))
    assert r['ready'] is True and ids(r)['r']['warn'] and not ids(r)['browser']['ok']


def test_workspace_check_creates_nothing_and_detects_unwritable(tmp_path):
    fresh = tmp_path / 'a' / 'b'
    c = E.check_workspace(fresh)
    assert c['ok'] and not fresh.exists()
    if os.name != 'nt' and os.geteuid() != 0:
        ro = tmp_path / 'ro'
        ro.mkdir()
        ro.chmod(0o500)
        assert not E.check_workspace(ro / 'x')['ok']


@posix_only
def test_report_lists_fixes_for_failures_only(tmp_path):
    r = E.check_environment(None, rscript=fake_rscript(tmp_path, 'deps'), include_browser=False)
    text = E.format_report(r)
    assert '✗ R 套件 ipssm' in text and '→ 相依套件不完整' in text and '尚未滿足' in text
    assert '→' not in E.format_report(E.check_environment(None, rscript=fake_rscript(tmp_path, 'ok'), include_browser=False)).replace('→ 全部', '')


# --- 命令列 --------------------------------------------------------------------------------

@posix_only
def test_cli_json_and_exit_codes(tmp_path):
    env = {**os.environ, 'IPSSM_RSCRIPT': fake_rscript(tmp_path, 'ok')}
    run = subprocess.run([sys.executable, str(REPO / 'ipssm_env.py'), '--json', '--workspace', str(tmp_path / 'w')],
                         capture_output=True, text=True, env=env)
    data = json.loads(run.stdout)
    assert run.returncode == 0 and data['ready'] is True and {'checks', 'ready', 'platform', 'python'} <= set(data)
    assert all({'id', 'label', 'ok', 'required', 'detail', 'fix', 'warn', 'autofix'} <= set(c) for c in data['checks'])

    env['IPSSM_RSCRIPT'] = fake_rscript(tmp_path, 'missing')
    run = subprocess.run([sys.executable, str(REPO / 'ipssm_env.py'), '--workspace', str(tmp_path / 'w')],
                         capture_output=True, text=True, env=env)
    assert run.returncode == 1 and '尚未安裝' in run.stdout


def test_doctor_runs_without_pandas_or_openpyxl():
    """環境檢查本身只能用標準函式庫，否則「沒裝 pandas」時它自己就先壞掉。"""
    src = (REPO / 'ipssm_env.py').read_text()
    imports = set(re.findall(r'^(?:import|from)\s+([A-Za-z0-9_]+)', src, flags=re.M))
    assert not ({'pandas', 'openpyxl', 'numpy', 'requests'} & imports)
    assert imports <= set(sys.stdlib_module_names)


# --- find_rscript / install ----------------------------------------------------------------

@posix_only
def test_find_rscript_prefers_override_then_path(tmp_path, monkeypatch):
    fake = fake_rscript(tmp_path)
    monkeypatch.setenv('IPSSM_RSCRIPT', fake)
    assert E.find_rscript() == fake
    monkeypatch.setenv('IPSSM_RSCRIPT', str(tmp_path / 'nope'))
    monkeypatch.setattr(E.shutil, 'which', lambda name: '/from/path/Rscript' if name == 'Rscript' else None)
    assert E.find_rscript() == '/from/path/Rscript'


@posix_only
def test_install_streams_output_and_reports_result(tmp_path):
    lines = []
    ok = E.install_ipssm(fake_rscript(tmp_path, install_lines=['[install.R] 安裝位置: /x', '[install.R] SUCCESS']), log=lines.append)
    assert ok and lines == ['[install.R] 安裝位置: /x', '[install.R] SUCCESS']
    lines.clear()
    assert not E.install_ipssm(fake_rscript(tmp_path, install_lines=['boom'], install_rc=1), log=lines.append)
    assert 'boom' in lines


@posix_only
def test_install_timeout_kills_the_process(tmp_path):
    lines = []
    ok = E.install_ipssm(fake_rscript(tmp_path, sleep=30), log=lines.append, timeout=1)
    assert not ok and any('逾時' in ln for ln in lines)


def test_install_without_r_or_script(tmp_path, monkeypatch):
    lines = []
    assert not E.install_ipssm(None, log=lines.append) and '找不到 Rscript' in lines[0]


def test_pipeline_uses_the_same_r_lookup_and_env():
    import ipssm_pipeline as P
    assert P._find_rscript() == E.find_rscript()
    assert P.r_subprocess_env is E.r_subprocess_env


# --- 真實的離線安裝 (需要 R、與一份 ipssm 原始碼；沒有就略過) -------------------------------------------

@pytest.mark.skipif(not os.environ.get('IPSSM_TEST_SOURCE') or not E.find_rscript(),
                    reason='設定 IPSSM_TEST_SOURCE=<ipssm 原始碼資料夾> 才會執行這個耗時的真實安裝測試')
def test_real_offline_install_into_a_private_library(tmp_path):
    """讓 ipssm 對 R「看不見」(換掉站台 Renviron)，驗證: 偵測缺少 -> 實際安裝 -> 偵測通過。
    注意: 以 Debian/Ubuntu 的 R 設定為前提 (R_ENVIRON 覆寫站台設定檔)。"""
    lib = tmp_path / 'lib'
    lib.mkdir()
    renv = tmp_path / 'Renviron'
    renv.write_text('R_LIBS_SITE=/usr/lib/R/site-library:/usr/lib/R/library\n')
    env = {**os.environ, 'R_ENVIRON': str(renv), 'R_LIBS_USER': str(lib)}
    rscript = E.find_rscript()

    def check():
        old = os.environ.copy()
        os.environ.update(env)
        try:
            return E.check_ipssm(rscript)
        finally:
            os.environ.clear()
            os.environ.update(old)

    assert check()['detail'] == '尚未安裝'
    old = os.environ.copy()
    os.environ.update(env)
    try:
        assert E.install_ipssm(rscript, log=lambda s: None, source=os.environ['IPSSM_TEST_SOURCE'])
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert check()['ok'], check()
    assert (lib / 'ipssm').is_dir()
