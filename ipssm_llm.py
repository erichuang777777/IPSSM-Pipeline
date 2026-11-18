"""LLM 輔助檢核 (選用) — 只處理「關鍵字規則無法解析」的儲存格。

設計原則
--------
* **只做解讀，不做補值**：LLM 只能把一段「已存在的原始文字」對應到允許值
  (例如 "weakly positive" -> 1)。它不得憑空推測缺失值；無法解讀時必須回傳 null。
* **最小揭露 (privacy)**：只送出「欄位名稱 + 該欄位允許值 + 單一儲存格原文」，
  絕不送病患 ID 或同一列的其他欄位。相同 (欄位, 原文) 只查詢一次 (快取)。
* **遠端端點需明確同意**：本專案的使用條款要求資料已去識別化且不得外傳。
  只有 localhost / 私有網段端點可直接使用；其他端點必須明確 allow_remote=True。
* **一律附信心程度**：LLM 必須回傳 0~1 的 confidence；低於門檻者不採用，
  採用者在審閱活頁簿中一律標為「需人工確認」。

支援的端點 (provider)
--------------------
* ``openai``    — OpenAI 相容的 Chat Completions API。涵蓋本地 (Ollama、LM Studio、
                  vLLM、llama.cpp server …) 與雲端 (OpenAI、OpenRouter …)。
                  endpoint 填 base URL，例如 ``http://localhost:11434/v1``。
* ``anthropic`` — Anthropic Messages API (預設 ``https://api.anthropic.com``)。

僅使用標準函式庫 (urllib)，不增加依賴。
"""
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

PROVIDERS = ('openai', 'anthropic')
DEFAULT_ENDPOINTS = {'anthropic': 'https://api.anthropic.com'}
DEFAULT_MODELS = {'anthropic': 'claude-haiku-5-5'}
DEFAULT_KEY_ENV = {'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY'}
ANTHROPIC_VERSION = '2023-06-01'

# 給 LLM 的欄位語意說明 (只描述欄位，不含任何病患資料)
FIELD_DESCRIPTIONS = {
    'binary': ("a binary clinical/genetic flag for an MDS patient: 1 = present / mutated / "
               "positive, 0 = absent / wild-type / negative (tested and not detected). "
               "Use \"NA\" only if the text explicitly says the status is unknown / not tested."),
    'cyto': ("IPSS-R cytogenetic risk category. Allowed: Very Good, Good, Intermediate, "
             "Poor, Very Poor. Use \"NA\" only if the text says unknown / not available."),
    'tp53mut': ("number of TP53 mutations. Allowed: \"0\", \"1\", \"2 or more\"."),
    'number': ("a single numeric value (plain number, no unit). Hemoglobin is in g/dL, "
               "platelets in 10^9/L, bone-marrow blasts in percent, TP53 max VAF as a "
               "fraction between 0 and 1."),
}


@dataclass
class LLMConfig:
    provider: str = 'openai'
    endpoint: str = ''
    model: str = ''
    api_key: str = ''
    timeout: float = 30.0
    min_confidence: float = 0.8
    allow_remote: bool = False

    def resolved_endpoint(self) -> str:
        return (self.endpoint or DEFAULT_ENDPOINTS.get(self.provider, '')).rstrip('/')

    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, '')


@dataclass
class LLMResult:
    column: str
    raw_value: str
    value: Optional[str]          # None = LLM 判定無法解讀
    confidence: float
    reason: str = ''
    accepted: bool = False        # 是否達信心門檻且通過允許值檢查
    error: str = ''


def is_local_endpoint(url: str) -> bool:
    """localhost / 迴圈位址 / 私有網段 視為本地端點。"""
    host = urllib.parse.urlparse(url).hostname or ''
    if host in ('localhost', 'host.docker.internal'):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def _http_post_json(url: str, headers: Dict[str, str], payload: dict, timeout: float) -> dict:
    """送出 JSON POST 並回傳解析後的 JSON。獨立成函式以便測試時替換。"""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', **headers}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def _parse_model_json(text: str) -> dict:
    match = _JSON_RE.search(text or '')
    if not match:
        raise ValueError(f"no JSON object in LLM reply: {text[:80]!r}")
    return json.loads(match.group(0))


class LLMResolver:
    """把規則無法解析的儲存格原文，對應到允許值並給出信心程度。"""

    def __init__(self, config: LLMConfig):
        if config.provider not in PROVIDERS:
            raise ValueError(f"llm provider must be one of {PROVIDERS}, got {config.provider!r}")
        endpoint = config.resolved_endpoint()
        if not endpoint:
            raise ValueError("LLM endpoint is required (e.g. http://localhost:11434/v1)")
        if not config.resolved_model():
            raise ValueError("LLM model name is required")
        if not is_local_endpoint(endpoint) and not config.allow_remote:
            raise ValueError(
                f"LLM endpoint {endpoint} is not local. Sending cell text to a remote "
                f"service must be explicitly allowed (--llm-allow-remote). Make sure the "
                f"data is de-identified and permitted by the IPSS-M terms of use.")
        self.config = config
        self._cache: Dict[Tuple[str, str], LLMResult] = {}
        self.log: List[LLMResult] = []     # 每個唯一 (欄位, 原文) 的查詢結果
        self.is_remote = not is_local_endpoint(endpoint)

    # ---- prompt ---------------------------------------------------------
    @staticmethod
    def _system_prompt() -> str:
        return ("You normalize ONE cell of a clinical spreadsheet for the IPSS-M MDS risk "
                "model. Interpret only the text you are given; never guess a value that "
                "the text does not support, and never invent missing data. "
                "Reply with ONLY a JSON object: "
                '{"value": <one allowed value, "NA", or null>, "confidence": <0..1>, '
                '"reason": <short>}. Use null when the text cannot be interpreted.')

    @staticmethod
    def _user_prompt(column: str, raw_value: str, kind: str, allowed: List[str]) -> str:
        return (f"Field: {column}\nMeaning: {FIELD_DESCRIPTIONS[kind]}\n"
                f"Allowed values: {json.dumps(allowed)}\n"
                f"Cell text (verbatim): {json.dumps(raw_value, ensure_ascii=False)}")

    # ---- transport ------------------------------------------------------
    def _complete(self, system: str, user: str) -> str:
        cfg = self.config
        base = cfg.resolved_endpoint()
        if cfg.provider == 'anthropic':
            headers = {'anthropic-version': ANTHROPIC_VERSION}
            if cfg.api_key:
                headers['x-api-key'] = cfg.api_key
            data = _http_post_json(f"{base}/v1/messages", headers, {
                'model': cfg.resolved_model(), 'max_tokens': 300, 'temperature': 0,
                'system': system, 'messages': [{'role': 'user', 'content': user}],
            }, cfg.timeout)
            return ''.join(b.get('text', '') for b in data.get('content', []))
        headers = {'Authorization': f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        data = _http_post_json(f"{base}/chat/completions", headers, {
            'model': cfg.resolved_model(), 'temperature': 0,
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': user}],
        }, cfg.timeout)
        return data['choices'][0]['message']['content']

    # ---- public ---------------------------------------------------------
    def resolve(self, column: str, raw_value: str, kind: str,
                allowed: List[str]) -> LLMResult:
        """解讀單一儲存格。結果依 (欄位, 原文) 快取。"""
        key = (column, raw_value)
        if key in self._cache:
            return self._cache[key]

        result = LLMResult(column=column, raw_value=raw_value, value=None, confidence=0.0)
        try:
            reply = _parse_model_json(self._complete(
                self._system_prompt(), self._user_prompt(column, raw_value, kind, allowed)))
            value = reply.get('value')
            result.value = None if value is None else str(value).strip()
            result.confidence = max(0.0, min(1.0, float(reply.get('confidence', 0.0))))
            result.reason = str(reply.get('reason', ''))[:200]
            result.accepted = self._accept(result, kind, allowed)
            if result.value is not None and not result.accepted and not result.error:
                result.error = (f"below min confidence {self.config.min_confidence}"
                                if result.confidence < self.config.min_confidence
                                else "value not in allowed set")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError,
                IndexError, TypeError) as exc:
            result.error = f"LLM call failed: {exc}"
        self._cache[key] = result
        self.log.append(result)
        return result

    def _accept(self, result: LLMResult, kind: str, allowed: List[str]) -> bool:
        if result.value is None or result.confidence < self.config.min_confidence:
            return False
        if result.value == 'NA':
            return True
        if kind == 'number':
            try:
                float(result.value)
                return True
            except ValueError:
                return False
        return result.value in allowed
