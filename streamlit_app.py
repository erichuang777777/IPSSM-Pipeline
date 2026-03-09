import streamlit as st
import pandas as pd
import requests
import io

# ==========================================
# 參數與常數定義 (與本地 pipeline 一致)
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

VALIDATION_RULES = {
    'HB': {'min': 4, 'max': 20},
    'PLT': {'min': 0, 'max': 2000},
    'BM_BLAST': {'min': 0, 'max': 30},
    'TP53maxvaf': {'min': 0, 'max': 1},
    'CYTO_IPSSR': {'values': ['Very Good', 'Good', 'Intermediate', 'Poor', 'Very Poor']},
    'TP53mut': {'values': ['0', '1', '2 or more']},
}

BINARY_FIELDS = {
    'del5q', 'del7_7q', 'complex', 'del17_17p', 'MLL_PTD', 'FLT3', 'ASXL1',
    'BCOR', 'BCORL1', 'CBL', 'CEBPA', 'DNMT3A', 'ETV6', 'EZH2', 'IDH1', 'IDH2',
    'KRAS', 'NF1', 'NPM1', 'NRAS', 'RUNX1', 'SETBP1', 'SF3B1', 'SRSF2', 'STAG2',
    'U2AF1', 'ETNK1', 'GATA2', 'GNB1', 'PHF6', 'PPM1D', 'PRPF8', 'PTPN11', 'WT1'
}

# ==========================================
# 1. 資料清理邏輯
# ==========================================
class ValidationReport:
    def __init__(self):
        self.errors = []
        self.skipped_patients = []
        self.output_rows = 0

def clean_data(raw_df):
    report = ValidationReport()
    
    # 填充缺失值為 NA 字串前，先全轉字串
    raw_df = raw_df.astype(str)
    
    # 偵測 FJUH 格式 (尾部帶有空格)
    original_cols = list(raw_df.columns)
    mapping = {}
    for col in original_cols:
        clean_col = col.strip()
        if col != clean_col:
            mapping[col] = clean_col
    if mapping:
        raw_df = raw_df.rename(columns=mapping)
    
    valid_rows = []
    
    for idx, row in raw_df.iterrows():
        row_dict = row.to_dict()
        patient_id = row_dict.get('ID', f'Row{idx+2}')
        
        # 1. 檢查必填
        missing = [f for f in REQUIRED_FIELDS if row_dict.get(f, '').strip() in NA_STRINGS or row_dict.get(f, '').strip() == '']
        if missing:
            report.skipped_patients.append(patient_id)
            continue
            
        # 2. 清理每個欄位
        cleaned_row = {}
        row_is_valid = True
        
        for col_name, value in row_dict.items():
            clean_col = col_name.strip()
            clean_val = str(value).strip()
            
            if clean_val in NA_STRINGS or pd.isna(value):
                cleaned_row[clean_col] = 'NA'
            else:
                cleaned_row[clean_col] = clean_val
                
        # 額外將一些特定要求清理：例如 TP53mut 若是 '>1' 或 '2' 改為 '2 or more'
        if cleaned_row.get('TP53mut') in ['2', '>1', '2 or more']:
            cleaned_row['TP53mut'] = '2 or more'
            
        # 若需要其他嚴格資料排除，可以寫在這裡。這邊僅做簡單清理並讓 API 處理其餘部分
        final_row = {col: cleaned_row.get(col, 'NA') for col in STANDARD_COLUMNS}
        valid_rows.append(final_row)
        
    report.output_rows = len(valid_rows)
    # 返回清理後的 DataFrame
    cleaned_df = pd.DataFrame(valid_rows)
    return cleaned_df, report

# ==========================================
# 2. 呼叫官方 API 邏輯
# ==========================================
def calculate_ipssm_via_api(cleaned_df):
    results = []
    
    progress_bar = st.progress(0)
    total_rows = len(cleaned_df)
    
    for index, row in cleaned_df.iterrows():
        row_dict = row.to_dict()
        
        # 建構 payload，過濾掉 NA 值，轉換數字
        payload = {}
        for k, v in row_dict.items():
            if k == 'ID' or v == 'NA':
                continue
            
            if k == 'CYTO_IPSSR' or k == 'TP53mut':
                payload[k] = str(v)
            else:
                # 嘗試轉為數字
                try:
                    payload[k] = float(v) if '.' in str(v) else int(float(v))
                except:
                    payload[k] = str(v)
                    
        try:
            # 發送請求給官方 API
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
                    "IPSSMscore": score_mean, 
                    "IPSSMcat": cat_mean, 
                    "IPSSMscore_best": score_best,
                    "IPSSMscore_worst": score_worst,
                    "Range_Score": round(range_score, 4),
                    "Confidence_Level": confidence,
                    "API_Status": "Success"
                })
            else:
                results.append({
                    "IPSSMscore": None, "IPSSMcat": None, 
                    "IPSSMscore_best": None, "IPSSMscore_worst": None,
                    "Range_Score": None, "Confidence_Level": None,
                    "API_Status": f"Error {response.status_code}: {response.text}"
                })
                
        except Exception as e:
            results.append({
                "IPSSMscore": None, "IPSSMcat": None, 
                "IPSSMscore_best": None, "IPSSMscore_worst": None,
                "Range_Score": None, "Confidence_Level": None,
                "API_Status": str(e)
            })
            
        progress_bar.progress((index + 1) / total_rows)
        
    res_df = pd.DataFrame(results)
    
    # 建立最終產出
    final_df = pd.concat([cleaned_df.reset_index(drop=True), res_df], axis=1)
    
    # 建立 Summary Sheet (ID + Confidence Level)
    summary_df = final_df[['ID', 'Confidence_Level']].copy()
    
    return final_df, summary_df

# ==========================================
# 3. Streamlit 網頁介面設計
# ==========================================
def main():
    st.set_page_config(page_title="IPSS-M 批次計算工具 (API版)", layout="wide", page_icon="🧬")

    st.title("🧬 IPSS-M 批次計算工具 (官方 API 版)")
    st.markdown("""
    本工具透過呼叫官方 API ([mds-risk-model.com](https://mds-risk-model.com)) 計算 IPSS-M 風險評分。
    - ✅ 支援 FJUH 格式與標準 42 欄格式
    - ✅ 自動清理缺失值 (`NA`, `N/A`, ` `) 
    - ✅ 自動標記 **CONFIDENT** / **UNCERTAIN** 信心等級
    """)

    st.warning("⚠️ 請注意：因為使用官方雲端 API，若同時上傳大量資料 (例如幾百筆)，需等待幾分鐘下載。")

    uploaded_file = st.file_uploader("📂 選擇包含資料的 Excel 檔案 (.xlsx) 或 CSV 檔案", type=["xlsx", "csv"])

    if uploaded_file is not None:
        try:
            if uploaded_file.name.endswith('.csv'):
                raw_df = pd.read_csv(uploaded_file)
            else:
                raw_df = pd.read_excel(uploaded_file)
                
            st.success(f"檔案上傳成功！共 {len(raw_df)} 筆資料。")
            
            with st.expander("👀 預覽前 3 筆原始資料"):
                st.dataframe(raw_df.head(3))
            
            if st.button("🚀 開始計算 IPSS-M", type="primary"):
                with st.spinner("正在清理資料並呼叫 API 計算中，請稍候..."):
                    
                    # 1. 執行資料清理
                    cleaned_df, report = clean_data(raw_df)
                    
                    if len(report.skipped_patients) > 0:
                        st.warning(f"跳過了 {len(report.skipped_patients)} 筆缺少 HB/PLT/BM_BLAST 的資料。")
                        
                    if len(cleaned_df) == 0:
                        st.error("清理後沒有剩餘任何有效資料可供計算！")
                        return
                    
                    # 2. 執行 API 計算
                    final_result_df, summary_df = calculate_ipssm_via_api(cleaned_df)
                    
                    st.success("🎉 計算完成！")
                    
                    st.subheader("📊 計算結果概覽")
                    
                    col1, col2, col3 = st.columns(3)
                    confident = len(summary_df[summary_df['Confidence_Level'] == 'CONFIDENT'])
                    uncertain = len(summary_df[summary_df['Confidence_Level'] == 'UNCERTAIN'])
                    total = len(summary_df)
                    
                    col1.metric("總計成功", f"{total} 筆")
                    col2.metric("CONFIDENT (可靠)", f"{confident} 筆")
                    col3.metric("UNCERTAIN (不確定)", f"{uncertain} 筆")
                    
                    st.write("預覽 5 筆摘要結果：")
                    st.dataframe(summary_df.head(5))
                    
                    # 3. 匯出 Excel
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
