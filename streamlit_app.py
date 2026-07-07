import streamlit as st
import pandas as pd
import requests
import io
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None

# 載入現有的 pipeline 函數與共用常數 (單一資料來源，避免重複定義失同步)
try:
    from ipssm_pipeline import (
        run_screening, run_translation, _find_rscript,
        clean_dataframe, STANDARD_COLUMNS, REQUIRED_FIELDS, NA_STRINGS,
    )
    HAS_PIPELINE = True
except ImportError:
    HAS_PIPELINE = False
    # 後備定義 (僅在找不到 ipssm_pipeline.py 時使用)
    STANDARD_COLUMNS = [
        'ID', 'HB', 'PLT', 'BM_BLAST', 'del5q', 'del7_7q', 'complex', 'CYTO_IPSSR',
        'del17_17p', 'TP53mut', 'TP53maxvaf', 'TP53loh', 'MLL_PTD', 'FLT3', 'ASXL1',
        'BCOR', 'BCORL1', 'CBL', 'CEBPA', 'DNMT3A', 'ETV6', 'EZH2', 'IDH1', 'IDH2',
        'KRAS', 'NF1', 'NPM1', 'NRAS', 'RUNX1', 'SETBP1', 'SF3B1', 'SRSF2', 'STAG2',
        'U2AF1', 'ETNK1', 'GATA2', 'GNB1', 'PHF6', 'PPM1D', 'PRPF8', 'PTPN11', 'WT1'
    ]
    REQUIRED_FIELDS = {'HB', 'PLT', 'BM_BLAST'}
    NA_STRINGS = {'', ' ', 'NA', 'N/A', 'n/a', 'na', 'NaN', 'nan', 'None', 'none', '.', 'ND', 'nd'}
    clean_dataframe = None

# ==========================================
# API 引擎設定
# ==========================================
API_URL = "https://api.mds-risk-model.com/ipssm"
API_TIMEOUT = 15
API_MAX_RETRIES = 3
API_MAX_WORKERS = 8


def clean_data_for_api(raw_df):
    """清理上傳資料供 API 引擎使用 (重用 pipeline 的 clean_dataframe)。"""
    if clean_dataframe is not None:
        return clean_dataframe(raw_df, collapse_tp53=True)

    # 後備: ipssm_pipeline 不可用時的最小清理
    class _Report:
        def __init__(self):
            self.skipped_patients = []
            self.output_rows = 0

    report = _Report()
    df = raw_df.astype(str).rename(columns=lambda c: c.strip())
    valid_rows = []
    for idx, row in df.iterrows():
        row_dict = row.to_dict()
        if any(str(row_dict.get(f, '')).strip() in NA_STRINGS for f in REQUIRED_FIELDS):
            report.skipped_patients.append(str(row_dict.get('ID', f'Row{idx + 2}')))
            continue
        cleaned = {k: ('NA' if str(v).strip() in NA_STRINGS else str(v).strip())
                   for k, v in row_dict.items()}
        if cleaned.get('TP53mut') in ('2', '>1', '2 or more'):
            cleaned['TP53mut'] = '2 or more'
        valid_rows.append({c: cleaned.get(c, 'NA') for c in STANDARD_COLUMNS})
    report.output_rows = len(valid_rows)
    return pd.DataFrame(valid_rows, columns=STANDARD_COLUMNS), report


def _build_api_session():
    """建立具連線重用與指數退避重試的 requests.Session。"""
    session = requests.Session()
    if Retry is not None:
        retry = Retry(
            total=API_MAX_RETRIES,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(['POST']),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=API_MAX_WORKERS)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


def _row_to_payload(row_dict):
    """將一列清理後資料轉為 API payload (略過 ID 與 NA 欄位)。"""
    payload = {}
    for k, v in row_dict.items():
        if k == 'ID' or v == 'NA':
            continue
        if k in ('CYTO_IPSSR', 'TP53mut'):
            payload[k] = str(v)
        else:
            try:
                payload[k] = float(v) if '.' in str(v) else int(float(v))
            except (ValueError, TypeError):
                payload[k] = str(v)
    return payload


def _call_api_for_row(session, row_dict):
    """對單列呼叫 API，回傳結果 dict。網路/逾時錯誤回傳可讀訊息。"""
    empty = {
        "IPSSMscore": None, "IPSSMcat": None, "IPSSMscore_best": None,
        "IPSSMscore_worst": None, "Range_Score": None, "Confidence_Level": None,
    }
    try:
        response = session.post(API_URL, json=_row_to_payload(row_dict), timeout=API_TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            score_best = data['ipssm']['best']['riskScore']
            score_worst = data['ipssm']['worst']['riskScore']
            range_score = score_worst - score_best
            return {
                "IPSSMscore": data['ipssm']['means']['riskScore'],
                "IPSSMcat": data['ipssm']['means']['riskCat'],
                "IPSSMscore_best": score_best, "IPSSMscore_worst": score_worst,
                "Range_Score": round(range_score, 4),
                "Confidence_Level": "CONFIDENT" if range_score < 1.0 else "UNCERTAIN",
                "API_Status": "Success",
            }
        err_msg = response.text
        if "CYTO_IPSSR" in err_msg:
            err_msg = "缺少必填的 CYTO_IPSSR (細胞遺傳學)。官方 API 不支援此欄位空白。"
        return {**empty, "API_Status": f"Error {response.status_code}: {err_msg}"}
    except requests.exceptions.Timeout:
        return {**empty, "API_Status": f"逾時 (>{API_TIMEOUT}s)，已重試 {API_MAX_RETRIES} 次仍失敗"}
    except requests.exceptions.RequestException as e:
        return {**empty, "API_Status": f"連線錯誤: {e}"}
    except (KeyError, ValueError) as e:
        return {**empty, "API_Status": f"回應格式錯誤: {e}"}


def calculate_ipssm_via_api(cleaned_df):
    """以執行緒池併發呼叫 API，保留原始列順序。"""
    cleaned_df = cleaned_df.reset_index(drop=True)
    row_dicts = cleaned_df.to_dict('records')
    total_rows = len(row_dicts)
    results = [None] * total_rows
    progress_bar = st.progress(0)
    done = 0

    session = _build_api_session()
    try:
        with ThreadPoolExecutor(max_workers=min(API_MAX_WORKERS, max(total_rows, 1))) as executor:
            future_to_idx = {
                executor.submit(_call_api_for_row, session, rd): i
                for i, rd in enumerate(row_dicts)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                results[idx] = future.result()
                done += 1
                progress_bar.progress(done / total_rows)
    finally:
        session.close()

    res_df = pd.DataFrame(results)
    final_df = pd.concat([cleaned_df, res_df], axis=1)
    summary_df = final_df[['ID', 'Confidence_Level', 'API_Status']].copy()
    return final_df, summary_df

# ==========================================
# 3. Streamlit 網頁介面設計
# ==========================================
def main():
    st.set_page_config(page_title="IPSS-M 批次計算工具", layout="wide", page_icon="🧬")

    st.title("🧬 IPSS-M 批次計算工具 (雙引擎版)")
    st.caption("👨‍⚕️ Developed by: **erichuang777777** | ⚙️ Powered by: MSKCC IPSS-M Engine")

    st.markdown("""
    本工具提供兩種計算 IPSS-M 風險評分的引擎：
    1. **R 模型引擎 (支援遺失資料)**：使用官方 R 套件，支援情境分析 (Scenario Analysis)，能處理 **缺少細胞遺傳學 (CYTO_IPSSR)** 的資料！
    2. **Web API 引擎 (輕量極速)**：使用官方 REST API，但 **嚴格要求** 必須提供 `CYTO_IPSSR`，若空白會直接報錯 `Error 400`。
    """)

    engine = st.radio("⚙️ 選擇計算引擎", ["1️⃣ R 模型引擎 (推薦，支援所有的資料缺失處理)", "2️⃣ 官方 Web API (速度快，但 CYTO_IPSSR 不可空白)"])

    @st.cache_resource(show_spinner="⏳ 正在初始化雲端 R 執行環境與 IPSSM 核心套件 (⚠️ 伺服器初次部署時約需花費 3~5 分鐘安裝，請耐心等待...)")
    def setup_r_environment():
        rscript = "Rscript" if os.name == "posix" else _find_rscript()
        if not rscript:
            return False
        try:
            # 設定使用者可寫的 R 函式庫路徑
            user_r_lib = os.path.expanduser("~/R/library")
            os.makedirs(user_r_lib, exist_ok=True)
            env = os.environ.copy()
            env["R_LIBS_USER"] = user_r_lib

            # 檢查 ipssm 是否已安裝 (包含雲端與本地路徑)
            check_script = """
            lib_candidates <- c(
              path.expand("~/R/library"),
              path.expand("~/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Local/R/win-library/4.5"),
              path.expand("~/AppData/Local/R/win-library/4.5"),
              "C:/Users/user/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Local/R/win-library/4.5",
              "C:/Users/user/AppData/Local/R/win-library/4.5"
            )
            for (lib_path in lib_candidates) {
              if (dir.exists(lib_path)) .libPaths(c(lib_path, .libPaths()))
            }
            if (!require('ipssm', quietly=TRUE)) quit(status=1)
            """
            check_cmd = [rscript, "-e", check_script]
            if subprocess.run(check_cmd, env=env, capture_output=True).returncode == 0:
                return True

            # 若未安裝，呼叫 install.R 安裝腳本
            install_r_path = os.path.join(os.path.dirname(__file__), "install.R")
            result = subprocess.run([rscript, install_r_path], capture_output=True, text=True, env=env, timeout=600)
            if result.returncode != 0:
                st.toast(f"R 套件安裝失敗：\n{result.stderr}", icon="❌")
                return False
            return True
        except Exception as e:
            st.toast(f"R 環境初始化異常：{e}", icon="❌")
            return False

    st.markdown("---")
    st.subheader("⚠️ 法律與合規確認")
    st.info("根據 MSKCC 官方 IPSS-M 使用條款規定，您必須同意以下事項才能執行計算：")
    agree_terms = st.checkbox("✅ 我同意接受 IPSS-M 官方使用條款，並確認：(1) 資料已去識別化且不含病患個資 (PHI)；(2) 計算結果僅供「學術研究」使用，絕不直接應用於臨床診斷、治療或醫療報告。")
    st.markdown("---")

    uploaded_file = st.file_uploader("📂 選擇包含資料的 Excel 檔案 (.xlsx) 或 CSV 檔案", type=["xlsx", "csv"])

    if uploaded_file is not None:
        file_ext = ".csv" if uploaded_file.name.endswith('.csv') else ".xlsx"
        
        try:
            if file_ext == '.csv':
                raw_df = pd.read_csv(uploaded_file)
            else:
                raw_df = pd.read_excel(uploaded_file)
                
            st.success(f"檔案上傳成功！共 {len(raw_df)} 筆資料。")
            with st.expander("👀 預覽前 3 筆原始資料 (為保護隱私已隱藏 ID 欄位)"):
                preview_df = raw_df.copy()
                if 'ID' in preview_df.columns:
                    preview_df = preview_df.drop(columns=['ID'])
                st.dataframe(preview_df.head(3))
            
            if not agree_terms:
                st.warning("請先勾選上方的「法律與合規確認」同意書，按鈕才會解鎖。")
            
            if st.button("🚀 開始計算 IPSS-M", type="primary", disabled=not agree_terms):
                # ==========================================
                # Engine 1: 呼叫本地/雲端 R 引擎 (ipssm_pipeline.py)
                # ==========================================
                if "1️⃣" in engine:
                    if not HAS_PIPELINE:
                        st.error("找不到 ipssm_pipeline.py，無法使用 R 引擎！")
                        return
                    
                    with st.spinner("正在執行資料驗證與 R 語言計算... (這可能需要一些時間)"):
                        # 將上傳的檔案存入 Temp
                        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_in:
                            tmp_in.write(uploaded_file.getbuffer())
                            tmp_in_path = tmp_in.name
                            
                        base_name = os.path.splitext(tmp_in_path)[0]
                        cleaned_csv = base_name + "_cleaned.csv"
                        log_path = base_name + "_screening_log.txt"
                        
                        # run_translation 以 cleaned_csv 為輸入，檔案名自帶 _cleaned 
                        # 因此最後輸出會多一層 _cleaned，例如：_cleaned_results.xlsx
                        excel_output = base_name + "_cleaned_results.xlsx"
                        
                        # 0. 確認 R 環境
                        setup_success = setup_r_environment()
                        if not setup_success:
                            st.error("R 環境或核心套件安裝失敗！無法執行 R 引擎。")
                            return

                        # 1. 執行 Screening
                        success_screen = run_screening(tmp_in_path, cleaned_csv, log_path)
                        if not success_screen:
                            st.error("資料驗證失敗！請檢查檔案格式。")
                            if os.path.exists(log_path):
                                with open(log_path, "r", encoding="utf-8") as f:
                                    st.text_area("驗證錯誤日誌", f.read())
                            return
                            
                        # 2. 尋找 Rscript (針對雲端與本地)
                        st.info("資料驗證完成！正在呼叫 R 模型計算 (IPSSMwrapper)...")
                        # 判定是否在 Streamlit Cloud (Linux)
                        rscript_path = "Rscript" if os.name == "posix" else _find_rscript()
                        if not rscript_path:
                            st.error("找不到 Rscript！這代表伺服器/本地尚未安裝 R 語言。")
                            return
                            
                        success_r = run_translation(cleaned_csv, rscript_path)
                        
                        if success_r and os.path.exists(excel_output):
                            st.success("🎉 R 引擎計算完成！情境分析與信心等級皆已產生。")
                            
                            # 讀取 Summary 提供預覽
                            summary_df = pd.read_excel(excel_output, sheet_name='Summary')
                            col1, col2, col3 = st.columns(3)
                            confident = len(summary_df[summary_df['Confidence_Level'] == 'CONFIDENT'])
                            uncertain = len(summary_df[summary_df['Confidence_Level'] == 'UNCERTAIN'])
                            
                            col1.metric("總計成功", f"{len(summary_df)} 筆")
                            col2.metric("CONFIDENT (可靠)", f"{confident} 筆")
                            col3.metric("UNCERTAIN (不確定)", f"{uncertain} 筆")
                            
                            preview_out_1 = summary_df.copy()
                            if 'ID' in preview_out_1.columns:
                                preview_out_1 = preview_out_1.drop(columns=['ID'])
                            st.dataframe(preview_out_1.head(5))
                            
                            # 供下載
                            with open(excel_output, "rb") as f:
                                st.download_button(
                                    label="📥 下載完整計算結果 (Excel, 包含 R_Full_Output)",
                                    data=f,
                                    file_name="IPSSM_R_Engine_Results.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                )
                        else:
                            err_msg = "未知錯誤"
                            err_log = os.path.join(os.path.dirname(excel_output), "r_error.log")
                            if os.path.exists(err_log):
                                with open(err_log, "r", encoding="utf-8") as f:
                                    err_msg = f.read()
                            st.error(f"R 計算失敗！可能是資料型態錯誤，或是 R 套件尚未安裝完畢。")
                            with st.expander("查看 R 引擎錯誤日誌 (給開發者)"):
                                st.code(err_msg, language="r")
                            
                # ==========================================
                # Engine 2: 呼叫 API
                # ==========================================
                else:
                    with st.spinner("正在清理資料並呼叫 API 計算中，請稍候..."):
                        cleaned_df, report = clean_data_for_api(raw_df)
                        
                        if len(report.skipped_patients) > 0:
                            st.warning(f"跳過了 {len(report.skipped_patients)} 筆缺少 HB/PLT/BM_BLAST 的資料。")
                            
                        if len(cleaned_df) == 0:
                            st.error("清理後沒有剩餘任何有效資料可供計算！")
                            return
                        
                        final_result_df, summary_df = calculate_ipssm_via_api(cleaned_df)
                        
                        st.success("🎉 API 計算完成！")
                        
                        # 檢查是否有 CYTO_IPSSR 的錯誤
                        err_count = summary_df['API_Status'].str.contains("CYTO_IPSSR").sum()
                        if err_count > 0:
                            st.error(f"❌ 警告：有 {err_count} 筆資料因為缺少 `CYTO_IPSSR`，遭官方 API 拒絕計算 (Error 400)。若要計算這類資料，強烈建議使用『R 模型引擎』！")
                        
                        st.write("預覽 5 筆摘要結果 (為保護隱私已隱藏 ID 欄位)：")
                        preview_out_2 = summary_df.copy()
                        if 'ID' in preview_out_2.columns:
                            preview_out_2 = preview_out_2.drop(columns=['ID'])
                        st.dataframe(preview_out_2.head(5))
                        
                        output = io.BytesIO()
                        with pd.ExcelWriter(output, engine='openpyxl') as writer:
                            summary_df.to_excel(writer, index=False, sheet_name='Summary')
                            final_result_df.to_excel(writer, index=False, sheet_name='R_Full_Output')
                        processed_data = output.getvalue()
                        
                        st.download_button(
                            label="📥 下載完整計算結果 (Excel)",
                            data=processed_data,
                            file_name="IPSSM_API_Calculated_Results.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                        
        except Exception as e:
            st.error(f"檔案讀取或處理時發生錯誤: {e}")

if __name__ == '__main__':
    main()
