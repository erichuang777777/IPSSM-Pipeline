import streamlit as st
import pandas as pd
import requests
import io
import os
import subprocess
import tempfile
import time

# 載入現有的 pipeline 函數
try:
    from ipssm_pipeline import run_screening, run_translation, _find_rscript
    HAS_PIPELINE = True
except ImportError:
    HAS_PIPELINE = False

# ==========================================
# 參數與常數定義 (用於 API 模式)
# ==========================================
STANDARD_COLUMNS = [
    'ID', 'HB', 'PLT', 'BM_BLAST', 'del5q', 'del7_7q', 'complex', 'CYTO_IPSSR',
    'del17_17p', 'TP53mut', 'TP53maxvaf', 'TP53loh', 'MLL_PTD', 'FLT3', 'ASXL1',
    'BCOR', 'BCORL1', 'CBL', 'CEBPA', 'DNMT3A', 'ETV6', 'EZH2', 'IDH1', 'IDH2',
    'KRAS', 'NF1', 'NPM1', 'NRAS', 'RUNX1', 'SETBP1', 'SF3B1', 'SRSF2', 'STAG2',
    'U2AF1', 'ETNK1', 'GATA2', 'GNB1', 'PHF6', 'PPM1D', 'PRPF8', 'PTPN11', 'WT1'
]

REQUIRED_FIELDS = {'HB', 'PLT', 'BM_BLAST'}
NA_STRINGS = {'', ' ', 'NA', 'N/A', 'n/a', 'na', 'NaN', 'nan', 'None', 'none', '.', 'ND', 'nd'}

class ValidationReport:
    def __init__(self):
        self.errors = []
        self.skipped_patients = []
        self.output_rows = 0

def clean_data_for_api(raw_df):
    report = ValidationReport()
    raw_df = raw_df.astype(str)
    
    # 偵測並修復欄名尾部空格
    mapping = {col: col.strip() for col in raw_df.columns if col != col.strip()}
    if mapping:
        raw_df = raw_df.rename(columns=mapping)
    
    valid_rows = []
    for idx, row in raw_df.iterrows():
        row_dict = row.to_dict()
        patient_id = row_dict.get('ID', f'Row{idx+2}')
        
        missing = [f for f in REQUIRED_FIELDS if row_dict.get(f, '').strip() in NA_STRINGS or row_dict.get(f, '').strip() == '']
        if missing:
            report.skipped_patients.append(patient_id)
            continue
            
        cleaned_row = {}
        for col_name, value in row_dict.items():
            clean_col = col_name.strip()
            clean_val = str(value).strip()
            cleaned_row[clean_col] = 'NA' if clean_val in NA_STRINGS or pd.isna(value) else clean_val
                
        if cleaned_row.get('TP53mut') in ['2', '>1', '2 or more']:
            cleaned_row['TP53mut'] = '2 or more'
            
        final_row = {col: cleaned_row.get(col, 'NA') for col in STANDARD_COLUMNS}
        valid_rows.append(final_row)
        
    report.output_rows = len(valid_rows)
    return pd.DataFrame(valid_rows), report

def calculate_ipssm_via_api(cleaned_df):
    results = []
    progress_bar = st.progress(0)
    total_rows = len(cleaned_df)
    
    for index, row in cleaned_df.iterrows():
        row_dict = row.to_dict()
        payload = {}
        for k, v in row_dict.items():
            if k == 'ID' or v == 'NA':
                continue
            if k == 'CYTO_IPSSR' or k == 'TP53mut':
                payload[k] = str(v)
            else:
                try: payload[k] = float(v) if '.' in str(v) else int(float(v))
                except: payload[k] = str(v)
                    
        try:
            response = requests.post("https://api.mds-risk-model.com/ipssm", json=payload, timeout=15)
            if response.status_code == 200:
                data = response.json()
                score_mean = data['ipssm']['means']['riskScore']
                cat_mean = data['ipssm']['means']['riskCat']
                score_best = data['ipssm']['best']['riskScore']
                score_worst = data['ipssm']['worst']['riskScore']
                range_score = score_worst - score_best
                confidence = "CONFIDENT" if range_score < 1.0 else "UNCERTAIN"
                
                results.append({
                    "IPSSMscore": score_mean, "IPSSMcat": cat_mean, 
                    "IPSSMscore_best": score_best, "IPSSMscore_worst": score_worst,
                    "Range_Score": round(range_score, 4), "Confidence_Level": confidence,
                    "API_Status": "Success"
                })
            else:
                # 若為細胞遺傳學錯誤，特製錯誤訊息
                err_msg = response.text
                if "CYTO_IPSSR" in err_msg:
                    err_msg = "缺少必填的 CYTO_IPSSR (細胞遺傳學)。官方 API 不支援此欄位空白。"
                results.append({
                    "IPSSMscore": None, "IPSSMcat": None, "IPSSMscore_best": None, "IPSSMscore_worst": None,
                    "Range_Score": None, "Confidence_Level": None, "API_Status": f"Error {response.status_code}: {err_msg}"
                })
        except Exception as e:
            results.append({
                "IPSSMscore": None, "IPSSMcat": None, "IPSSMscore_best": None, "IPSSMscore_worst": None,
                "Range_Score": None, "Confidence_Level": None, "API_Status": str(e)
            })
            
        progress_bar.progress((index + 1) / total_rows)
        
    res_df = pd.DataFrame(results)
    final_df = pd.concat([cleaned_df.reset_index(drop=True), res_df], axis=1)
    summary_df = final_df[['ID', 'Confidence_Level', 'API_Status']].copy()
    
    return final_df, summary_df

# ==========================================
# 3. Streamlit 網頁介面設計
# ==========================================
def main():
    st.set_page_config(page_title="IPSS-M 批次計算工具", layout="wide", page_icon="🧬")

    st.title("🧬 IPSS-M 批次計算工具 (雙引擎版)")
    st.caption("👨‍⚕️ Developed by: **輔仁大學附設醫院 (Fu Jen Catholic University Hospital, FJUH) 團隊** | ⚙️ Powered by: MSKCC IPSS-M Engine")

    st.markdown("""
    本工具提供兩種計算 IPSS-M 風險評分的引擎：
    1. **R 模型引擎 (支援遺失資料)**：使用官方 R 套件，支援情境分析 (Scenario Analysis)，能處理 **缺少細胞遺傳學 (CYTO_IPSSR)** 的資料！
    2. **Web API 引擎 (輕量極速)**：使用官方 REST API，但 **嚴格要求** 必須提供 `CYTO_IPSSR`，若空白會直接報錯 `Error 400`。
    """)

    engine = st.radio("⚙️ 選擇計算引擎", ["1️⃣ R 模型引擎 (推薦，支援所有的資料缺失處理)", "2️⃣ 官方 Web API (速度快，但 CYTO_IPSSR 不可空白)"])

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
            with st.expander("👀 預覽前 3 筆原始資料"):
                st.dataframe(raw_df.head(3))
            
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
                        excel_output = base_name + "_results.xlsx"
                        
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
                            
                            st.dataframe(summary_df.head(5))
                            
                            # 供下載
                            with open(excel_output, "rb") as f:
                                st.download_button(
                                    label="📥 下載完整計算結果 (Excel, 包含 R_Full_Output)",
                                    data=f,
                                    file_name="IPSSM_R_Engine_Results.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                )
                        else:
                            st.error("R 計算失敗！可能是資料型態錯誤，或是 R 套件尚未安裝完畢。")
                            
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
                        
                        st.write("預覽 5 筆摘要結果：")
                        st.dataframe(summary_df.head(5))
                        
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
