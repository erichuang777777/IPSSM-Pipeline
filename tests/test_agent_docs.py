"""skill / agent 文件與實際程式的一致性 — 文件寫的指令、旗標、檔案、輸出格式必須真的存在。"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / '.claude' / 'skills' / 'ipssm-web' / 'SKILL.md'
AGENT = REPO / '.claude' / 'agents' / 'ipssm-web-guide.md'
CLI_SKILL = REPO / '.claude' / 'skills' / 'ipssm' / 'SKILL.md'

VALID_TOOLS = {'Bash', 'Read', 'Glob', 'Grep', 'Edit', 'Write', 'WebFetch', 'WebSearch', 'NotebookEdit', 'Task', 'Agent'}


def frontmatter(path):
    """極簡 YAML frontmatter 解析 (只支援 key: value)。"""
    text = path.read_text(encoding='utf-8')
    m = re.match(r'^---\n(.*?)\n---\n', text, flags=re.S)
    assert m, f'{path.name} 缺少 frontmatter'
    data = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(':')
        data[k.strip()] = v.strip()
    return data, text[m.end():]


# --- 結構 ----------------------------------------------------------------------------

def test_skill_frontmatter():
    fm, body = frontmatter(SKILL)
    assert fm['name'] == SKILL.parent.name == 'ipssm-web'
    assert 20 < len(fm['description']) <= 1024
    assert '網頁' in fm['description'] and '環境' in fm['description']
    assert body.lstrip().startswith('# ')


def test_agent_frontmatter_and_least_privilege():
    fm, body = frontmatter(AGENT)
    assert fm['name'] == AGENT.stem == 'ipssm-web-guide'
    assert 20 < len(fm['description'])
    tools = {t.strip() for t in fm['tools'].split(',')}
    assert tools <= VALID_TOOLS, tools - VALID_TOOLS
    # 診斷員不得有寫入能力 (避免動到病患資料或程式碼)，也不應能再派出其他 agent
    assert not (tools & {'Edit', 'Write', 'NotebookEdit', 'Task', 'Agent'}), tools
    assert {'Bash', 'Read'} <= tools


def test_agent_states_it_cannot_talk_to_the_user():
    _, body = frontmatter(AGENT)
    assert '不能' in body and '直接問使用者' in body
    for key in ('allow_pip', 'allow_install_ipssm', '需要使用者做的事', '需要使用者決定的事'):
        assert key in body


# --- 隱私與安全規則必須寫在文件裡 (被誤刪就會失敗) -------------------------------------------------

@pytest.mark.parametrize('path', [SKILL, AGENT])
def test_privacy_and_safety_rules_present(path):
    text = path.read_text(encoding='utf-8')
    for phrase in ('病患', '隱私' if path == SKILL else '不讀', '學術研究', '127.0.0.1' if path == SKILL else '本機'):
        assert phrase in text, f'{path.name} 缺少「{phrase}」'
    assert 'Review_SignOff' in SKILL.read_text(encoding='utf-8')
    assert '不要替他勾' in SKILL.read_text(encoding='utf-8')          # 不得替使用者勾選核對確認


# --- 引用的檔案 / 旗標必須存在 -----------------------------------------------------------------

# 執行期才會產生的檔案 (不在 repo 內)，文件裡提到它們是正常的
RUNTIME_FILES = {'r_error.log', 'data_screening_log.txt', '_screening_log.txt'}


def referenced_paths(text):
    """文件中用反引號標示、看起來像 repo 內檔案的名稱 (排除含萬用字元的樣式，例如 *_screening_log.txt)。"""
    found = set()
    for token in re.findall(r'`([^`\n]+)`', text):
        if '*' in token or '<' in token:
            continue
        for cand in re.findall(r'(?:\.claude/[\w./-]+|[\w-]+\.(?:py|R|bat|sh|command|md|txt))', token):
            found.add(cand)
    return found - RUNTIME_FILES


@pytest.mark.parametrize('path', [SKILL, AGENT])
def test_every_referenced_file_exists(path):
    refs = referenced_paths(path.read_text(encoding='utf-8'))
    assert {'ipssm_env.py', 'ipssm_web.py'} <= refs                     # 抽取邏輯本身有效
    missing = sorted(r for r in refs if not (REPO / r).exists())
    assert not missing, f'{path.name} 引用了不存在的檔案: {missing}'


def test_documented_flags_exist_in_the_programs():
    skill = SKILL.read_text(encoding='utf-8') + AGENT.read_text(encoding='utf-8')
    env_help = subprocess.run([sys.executable, str(REPO / 'ipssm_env.py'), '--help'], capture_output=True, text=True).stdout
    web_help = subprocess.run([sys.executable, str(REPO / 'ipssm_web.py'), '--help'], capture_output=True, text=True).stdout
    for flag in ('--json', '--install-ipssm', '--ipssm-source'):
        assert flag in skill and flag in env_help, flag
    for flag in ('--workspace', '--port', '--no-browser'):
        assert flag in skill and flag in web_help, flag
    # 文件中提到的每個 --旗標，都必須存在於兩支程式其中之一
    for flag in set(re.findall(r'`(--[a-z][a-z-]+)', skill)):
        assert flag in env_help + web_help, f'文件提到不存在的旗標 {flag}'


def test_environment_json_has_the_fields_the_docs_promise():
    env = {**os.environ}
    run = subprocess.run([sys.executable, str(REPO / 'ipssm_env.py'), '--json'], capture_output=True, text=True, env=env)
    data = json.loads(run.stdout)
    assert {'ready', 'checks', 'platform', 'python', 'rscript'} <= set(data)
    for c in data['checks']:
        assert {'id', 'label', 'ok', 'required', 'detail', 'warn', 'fix', 'autofix'} <= set(c)
    ids = {c['id'] for c in data['checks']}
    text = SKILL.read_text(encoding='utf-8')
    for doc_id in ('python', 'py:pandas', 'py:openpyxl', 'r', 'ipssm', 'workspace', 'browser'):
        assert f'`{doc_id}`' in text, f'skill 沒有說明 id={doc_id}'
    assert {'python', 'py:pandas', 'py:openpyxl', 'r', 'ipssm'} <= ids


def test_three_ipssm_failure_messages_are_explained_in_the_docs():
    """doctor 會輸出的三種 ipssm 失敗說法，skill 與 agent 都必須解釋，否則引導者會被不認得的訊息難住。"""
    import ipssm_env as E
    produced = {'尚未安裝', '缺少相依套件', '無法完成範例計算'}
    src = (REPO / 'ipssm_env.py').read_text(encoding='utf-8')
    assert all(p in src for p in produced)
    for text in (SKILL.read_text(encoding='utf-8'), AGENT.read_text(encoding='utf-8')):
        assert all(p in text for p in produced)


# --- skill 教的「取出網址」正規表示式必須真的能用 -------------------------------------------------------

def test_url_regex_in_skill_matches_real_server_output(tmp_path):
    skill = SKILL.read_text(encoding='utf-8')
    m = re.search(r"`(http://127\\\.0\\\.0\\\.1:\\d\+/\\\?t=\[\\w-\]\+)`", skill)
    assert m, 'skill 裡找不到網址正規表示式'
    pattern = m.group(1)
    assert pattern in AGENT.read_text(encoding='utf-8')               # agent 與 skill 使用同一個正規表示式

    proc = subprocess.Popen([sys.executable, str(REPO / 'ipssm_web.py'), '--no-browser', '--port', '0',
                             '--workspace', str(tmp_path / 'ws')], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        url, deadline = None, time.time() + 20
        while time.time() < deadline and url is None:
            line = proc.stdout.readline()
            hit = re.search(pattern, line)
            url = hit.group(0) if hit else None
        assert url, '無法從程式輸出擷取網址'
        # 取出的網址真的能用: 帶權杖開啟 -> 導回首頁並設定 cookie
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(url, timeout=5)
        except urllib.error.HTTPError as e:
            assert e.code == 302 and 'ipssm_token=' in e.headers.get('Set-Cookie', '')
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# --- 與既有 CLI skill 互相參照 ---------------------------------------------------------------------

def test_cli_skill_points_to_the_web_guide_and_env_check():
    text = CLI_SKILL.read_text(encoding='utf-8')
    assert 'ipssm-web' in text and 'ipssm_env.py' in text
