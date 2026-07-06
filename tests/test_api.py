"""API 引擎測試 — 併發、順序保留、錯誤處理 (mock streamlit 與 HTTP session)。"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest


@pytest.fixture(scope="module")
def app():
    """注入假的 streamlit 模組後匯入 streamlit_app。"""
    fake_st = types.ModuleType("streamlit")

    class _Progress:
        def progress(self, *_args, **_kwargs):
            return None

    fake_st.progress = lambda *_a, **_k: _Progress()
    sys.modules["streamlit"] = fake_st
    import streamlit_app
    return streamlit_app


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200 if "CYTO_IPSSR" in payload else 400
        self.text = "" if self.status_code == 200 else "missing CYTO_IPSSR field"

    def json(self):
        # 以 HB 值當作 riskScore，方便回頭驗證結果對齊到正確的列
        hb = self._payload["HB"]
        return {"ipssm": {
            "means": {"riskScore": hb, "riskCat": "Low"},
            "best": {"riskScore": hb - 0.2},
            "worst": {"riskScore": hb + 0.2},
        }}


class _FakeSession:
    def post(self, url, json=None, timeout=None):
        return _FakeResponse(json)

    def close(self):
        pass


def _df(n):
    rows = []
    for i in range(n):
        rows.append({"ID": f"p{i}", "HB": float(i + 1), "PLT": 100, "BM_BLAST": 3,
                     "CYTO_IPSSR": "Good", "TP53mut": "NA"})
    return pd.DataFrame(rows)


def test_api_preserves_order(app, monkeypatch):
    monkeypatch.setattr(app, "_build_api_session", lambda: _FakeSession())
    df = _df(20)
    final_df, summary_df = app.calculate_ipssm_via_api(df)

    assert list(final_df["ID"]) == [f"p{i}" for i in range(20)]
    # riskScore == HB == i+1 → 證明第 i 列結果對齊第 i 列輸入 (併發下未錯位)
    for i in range(20):
        assert final_df.loc[i, "IPSSMscore"] == float(i + 1)
        assert final_df.loc[i, "API_Status"] == "Success"


def test_api_missing_cyto_reports_error(app, monkeypatch):
    monkeypatch.setattr(app, "_build_api_session", lambda: _FakeSession())
    df = _df(1)
    df.loc[0, "CYTO_IPSSR"] = "NA"   # payload 不含 CYTO_IPSSR → 假 API 回 400
    final_df, _ = app.calculate_ipssm_via_api(df)
    status = final_df.loc[0, "API_Status"]
    assert "CYTO_IPSSR" in status


def test_row_to_payload_skips_na_and_id(app):
    payload = app._row_to_payload({"ID": "x", "HB": "10", "PLT": "NA", "CYTO_IPSSR": "Good"})
    assert "ID" not in payload
    assert "PLT" not in payload
    assert payload["HB"] == 10
    assert payload["CYTO_IPSSR"] == "Good"
