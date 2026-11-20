#!/usr/bin/env python3
"""環境檢查 (doctor) — 這台電腦能不能跑 IPSS-M？缺什麼、怎麼補。

只使用標準函式庫：即使 pandas / openpyxl 還沒安裝也能執行 (「檢查有沒有裝」本身就是它的工作)。
網頁介面 (/api/status)、Claude skill / agent 與命令列共用同一份檢查，結果不會互相矛盾。

    python3 ipssm_env.py                 # 人類可讀的檢查報告 (全部就緒 exit 0，否則 exit 1)
    python3 ipssm_env.py --json          # 機器可讀 (給 skill / agent 解析)
    python3 ipssm_env.py --install-ipssm # 自動安裝 R 套件 ipssm (需要網路)

判斷原則
--------
* 「能不能真的載入 ipssm」才是權威判斷 (library(ipssm) 成功 = 套件與它的相依套件都在)。
* ipssm 套件本身沒有宣告最低 R 版本；本專案以 R 4.3 以上測試，所以低於 4.3 只給「警告」，
  不擋你 (避免誤擋其實可以用的環境)。
* Python 套件版本低於 requirements.txt 時視為未滿足 (資料處理依賴這些版本的行為)。
"""
import argparse
import glob
import importlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = Path.home() / 'IPSSM_Web_Workspace'
REQUIRED_PYTHON = (3, 8)
RECOMMENDED_R = (4, 3, 0)
MIN_FREE_MB = 200
INSTALL_TIMEOUT = 900

# 與 requirements.txt 一致 (tests/test_env.py 會比對，避免兩處不同步)
PY_PACKAGES = (('pandas', (2, 0, 0)), ('openpyxl', (3, 1, 0)))

R_DOWNLOAD = {
    'Windows': '到 https://cran.r-project.org/bin/windows/base/ 下載並安裝 R（4.3 以上），安裝完成後重新執行本程式。',
    'Darwin': '到 https://cran.r-project.org/bin/macosx/ 下載並安裝 R（4.3 以上），或執行 brew install r，然後重新執行本程式。',
    'Linux': '安裝 R（4.3 以上）：Debian/Ubuntu 執行 sudo apt install r-base，Fedora 執行 sudo dnf install R，然後重新執行本程式。',
}


# ---------------------------------------------------------------------------
#  小工具
# ---------------------------------------------------------------------------

def version_tuple(text):
    """'4.3.3' / '2.0.0rc1' -> (4, 3, 3)；解析不到回傳 ()。"""
    nums = re.findall(r'\d+', str(text))[:3]
    return tuple(int(n) for n in nums) if nums else ()


def fmt_version(t):
    return '.'.join(str(n) for n in t)


def _run(cmd, timeout=60, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, '', '執行逾時'
    except OSError as e:
        return 127, '', str(e)


def find_rscript():
    """尋找 Rscript (環境變數 IPSSM_RSCRIPT > PATH > 各系統常見安裝位置)。找不到回傳 None。"""
    override = os.environ.get('IPSSM_RSCRIPT')
    if override and Path(override).exists():
        return override
    for name in ('Rscript', 'Rscript.exe'):
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                     os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs')):
            candidates += sorted(glob.glob(os.path.join(base, 'R', 'R-*', 'bin', 'Rscript.exe')), reverse=True)
            candidates += sorted(glob.glob(os.path.join(base, 'R', 'R-*', 'bin', 'x64', 'Rscript.exe')), reverse=True)
    else:
        candidates += ['/Library/Frameworks/R.framework/Resources/bin/Rscript', '/opt/homebrew/bin/Rscript',
                       '/usr/local/bin/Rscript', '/usr/bin/Rscript', '/opt/R/bin/Rscript']
    return next((c for c in candidates if c and Path(c).exists()), None)


def r_subprocess_env():
    """呼叫 R 時使用的環境變數。只有在使用者沒設定 R_LIBS_USER、且 ~/R/library 存在時才補上
    (相容舊版安裝位置)，避免覆寫 RStudio / 系統預設的 library 路徑。"""
    env = os.environ.copy()
    legacy = os.path.expanduser('~/R/library')
    if 'R_LIBS_USER' not in env and os.path.isdir(legacy):
        env['R_LIBS_USER'] = legacy
    return env


def _check(id_, label, ok, detail, required=True, fix=None, warn=False, autofix=None):
    return {'id': id_, 'label': label, 'ok': bool(ok), 'required': required, 'detail': detail,
            'fix': fix, 'warn': bool(warn), 'autofix': autofix}


# ---------------------------------------------------------------------------
#  各項檢查
# ---------------------------------------------------------------------------

def check_python():
    cur = sys.version_info[:3]
    ok = cur >= REQUIRED_PYTHON
    return _check('python', 'Python', ok, f"{fmt_version(cur)}（需要 {fmt_version(REQUIRED_PYTHON)} 以上）",
                  fix=None if ok else '到 https://www.python.org/downloads/ 安裝較新的 Python（Windows 安裝時勾選 Add python.exe to PATH）。')


def check_py_package(name, minimum):
    fix = f'{sys.executable} -m pip install -r requirements.txt'
    try:
        from importlib import metadata
        installed = metadata.version(name)
    except Exception:
        return _check(f'py:{name}', f'Python 套件 {name}', False, '未安裝', fix=fix)
    try:
        importlib.import_module(name)
    except Exception as e:
        return _check(f'py:{name}', f'Python 套件 {name}', False, f'{installed} 已安裝但無法載入：{e}', fix=fix)
    ok = version_tuple(installed) >= minimum
    return _check(f'py:{name}', f'Python 套件 {name}', ok,
                  f"{installed}（需要 {fmt_version(minimum)} 以上）", fix=None if ok else fix)


def check_r(rscript):
    system = platform.system()
    if not rscript:
        return _check('r', 'R', False, '找不到 Rscript', fix=R_DOWNLOAD.get(system, R_DOWNLOAD['Linux']))
    rc, out, err = _run([rscript, '--version'])
    text = (out + err).strip()
    m = re.search(r'version\s+(\d+\.\d+\.\d+)', text)
    if rc != 0 or not m:
        return _check('r', 'R', False, f'Rscript 無法執行：{text[:120]}', fix=R_DOWNLOAD.get(system, R_DOWNLOAD['Linux']))
    ver = version_tuple(m.group(1))
    old = ver < RECOMMENDED_R
    detail = f"{m.group(1)}（{rscript}）"
    if old:
        detail += f"；ipssm 套件沒有宣告最低版本，但本專案以 R {fmt_version(RECOMMENDED_R)} 以上測試，建議升級"
    return _check('r', 'R', True, detail, warn=old,
                  fix=R_DOWNLOAD.get(system, R_DOWNLOAD['Linux']) if old else None)


# 冒煙測試: 不只檢查套件「存在」，而是用套件內附的範例資料實際算一次。
# (ipssm 在計算時才用到 car::recode 等相依套件，單純 library(ipssm) 成功不代表真的能算)
# 用結束碼區分原因，不解析錯誤文字 (R 的訊息會隨系統語言變成中文)。
IPSSM_PROBE = r"""
if (!requireNamespace("ipssm", quietly = TRUE)) quit(status = 3)
imp <- packageDescription("ipssm")$Imports
imp <- if (is.null(imp)) character(0) else trimws(strsplit(gsub("\\([^)]*\\)", "", imp), ",")[[1]])
imp <- imp[nzchar(imp)]
miss <- imp[!vapply(imp, requireNamespace, logical(1), quietly = TRUE)]
if (length(miss)) { cat(paste(miss, collapse = ",")); quit(status = 4) }
tryCatch({
  suppressPackageStartupMessages(library(ipssm))
  f <- system.file("extdata", "IPSSMexample.csv", package = "ipssm")
  invisible(capture.output(res <- suppressWarnings(suppressMessages(IPSSMwrapper(f)))))
  if (!"IPSSMscore_mean" %in% names(res) || nrow(res) < 1) stop("incomplete result")
  cat(as.character(packageVersion("ipssm")), nrow(res))
}, error = function(e) { cat(conditionMessage(e)); quit(status = 1) })
"""

IPSSM_FIX = ('按網頁上的「自動安裝」，或在這個資料夾執行 Rscript install.R（需要網路，首次約數分鐘）。'
             '離線環境可先取得 ipssm 原始碼資料夾，再設定 IPSSM_INSTALL_SOURCE=該資料夾 後執行 Rscript install.R。')


def check_ipssm(rscript):
    label = 'R 套件 ipssm（官方）'
    if not rscript:
        return _check('ipssm', label, False, '需要先安裝 R', fix='先完成上一項（安裝 R）。')
    rc, out, err = _run([rscript, '-e', IPSSM_PROBE], timeout=180, env=r_subprocess_env())
    out = out.strip()
    if rc == 0 and out:
        version, _, rows = out.partition(' ')
        return _check('ipssm', label, True, f'{version}（已用內附範例資料實際計算 {rows} 筆，正常）')
    if rc == 3:
        return _check('ipssm', label, False, '尚未安裝', fix=IPSSM_FIX, autofix='install_ipssm')
    if rc == 4:
        return _check('ipssm', label, False, f'已安裝，但缺少相依套件：{out.replace(",", "、")}',
                      fix='相依套件不完整，' + IPSSM_FIX, autofix='install_ipssm')
    tail = (out or err.strip().splitlines()[-1:] or ['未知原因'])
    tail = tail if isinstance(tail, str) else tail[0]
    return _check('ipssm', label, False, f'已安裝但無法完成範例計算：{tail[:160]}', fix=IPSSM_FIX,
                  autofix='install_ipssm')


def check_workspace(path):
    """工作資料夾是否可用。不會建立任何資料夾 (檢查不應改動這台電腦)：
    資料夾還不存在時，檢查最近的上層資料夾能不能寫入。"""
    path = Path(path)
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        if path.exists():
            with tempfile.NamedTemporaryFile(dir=path, delete=True):
                pass
        elif not os.access(probe, os.W_OK):
            raise PermissionError(f'無法在 {probe} 建立資料夾')
        free_mb = shutil.disk_usage(probe).free // (1024 * 1024)
    except OSError as e:
        return _check('workspace', '工作資料夾', False, f'{path} 無法使用：{e}',
                      fix='用 --workspace 指定一個可以寫入的資料夾。')
    low = free_mb < MIN_FREE_MB
    state = '可寫入' if path.exists() else '尚未建立，第一次使用時會自動建立'
    return _check('workspace', '工作資料夾', True, f'{path}（{state}，剩餘 {free_mb} MB）', warn=low,
                  fix='磁碟空間偏少，大型檔案可能失敗。' if low else None)


def check_browser():
    try:
        webbrowser.get()
        return _check('browser', '預設瀏覽器', True, '可自動開啟', required=False)
    except webbrowser.Error:
        return _check('browser', '預設瀏覽器', False, '找不到，程式不會自動開啟網頁', required=False,
                      fix='啟動後請手動把視窗裡顯示的網址貼到瀏覽器。')


def check_environment(workspace=None, rscript=None, include_browser=True):
    """執行全部檢查。回傳 {'ready', 'checks', 'rscript', 'platform', 'python'}。

    ready = 所有「必要」項目都通過 (警告不影響)。
    """
    rscript = rscript if rscript is not None else find_rscript()
    checks = [check_python()]
    checks += [check_py_package(n, v) for n, v in PY_PACKAGES]
    r = check_r(rscript)
    checks.append(r)
    checks.append(check_ipssm(rscript if r['ok'] else None))
    if workspace is not None:
        checks.append(check_workspace(workspace))
    if include_browser:
        checks.append(check_browser())
    return {
        'ready': all(c['ok'] for c in checks if c['required']),
        'checks': checks, 'rscript': rscript if r['ok'] else None,
        'platform': f"{platform.system()} {platform.release()}",
        'python': sys.version.split()[0],
    }


def format_report(result):
    lines = ['IPSS-M 環境檢查', '=' * 52]
    for c in result['checks']:
        mark = '✓' if c['ok'] and not c['warn'] else '!' if c['ok'] else ('✗' if c['required'] else '·')
        tag = '' if c['required'] else '（選用）'
        lines.append(f" {mark} {c['label']}{tag}：{c['detail']}")
        if c['fix'] and (not c['ok'] or c['warn']):
            lines.append(f"     → {c['fix']}")
    lines.append('=' * 52)
    lines.append('全部必要項目都滿足，可以開始使用。' if result['ready']
                 else '有必要項目尚未滿足，請依上方「→」的說明處理後再執行一次。')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
#  安裝 ipssm
# ---------------------------------------------------------------------------

def _kill_tree(proc):
    """終止程序「以及它啟動的所有子程序」。R 安裝時會再啟動 R CMD INSTALL、編譯器等，
    只殺掉最上層的 Rscript 會讓它們繼續握著輸出管線，逾時就形同虛設。"""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)       # 以 start_new_session 啟動，pid 即程序群組 id
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def install_ipssm(rscript, log=print, timeout=INSTALL_TIMEOUT, source=None):
    """執行 install.R 安裝 ipssm。逐行把輸出交給 log()。成功回傳 True。

    source: 本地 ipssm 原始碼資料夾 (離線安裝)；省略時從 GitHub 安裝。
    逾時會終止整個程序樹並回傳 False。
    """
    if not rscript:
        log('找不到 Rscript，無法安裝。')
        return False
    script = APP_DIR / 'install.R'
    if not script.exists():
        log(f'找不到安裝腳本 {script}')
        return False
    env = r_subprocess_env()
    if source:
        env['IPSSM_INSTALL_SOURCE'] = str(source)
    try:
        proc = subprocess.Popen([rscript, str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, cwd=str(APP_DIR), start_new_session=(os.name != 'nt'))
    except OSError as e:
        log(f'無法啟動 R：{e}')
        return False
    timed_out = threading.Event()

    def on_timeout():
        timed_out.set()
        _kill_tree(proc)

    timer = threading.Timer(timeout, on_timeout)
    timer.start()
    try:
        for line in proc.stdout:
            log(line.rstrip('\n'))
        proc.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        log(f'安裝逾時 ({timeout} 秒) 已中止。')
        return False
    return proc.returncode == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='IPSS-M 環境檢查：這台電腦能不能執行？缺什麼？')
    parser.add_argument('--json', action='store_true', help='輸出 JSON (給 skill / agent 解析)')
    parser.add_argument('--workspace', default=str(DEFAULT_WORKSPACE), help='要檢查的工作資料夾')
    parser.add_argument('--install-ipssm', action='store_true', help='自動安裝 R 套件 ipssm (需要網路)')
    parser.add_argument('--ipssm-source', help='離線安裝用: 本地 ipssm 原始碼資料夾')
    args = parser.parse_args(argv)

    if args.install_ipssm:
        install_ipssm(find_rscript(), source=args.ipssm_source)
        print('\n安裝完成，重新檢查：\n')
    result = check_environment(args.workspace)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(format_report(result))
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    sys.exit(main())
