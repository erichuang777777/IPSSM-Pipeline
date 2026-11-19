"""跨欄位一致性檢查 — 只提出「警告」，不排除病患、不修改資料。

目的是在醫師核對標準欄位資料時，把「資料彼此矛盾」或「模型會覆寫/忽略你的輸入」的
情況指出來。是否為真正的錯誤由人判斷 (有些矛盾在臨床上可能是真的)。

嚴重程度 (Severity)
-------------------
conflict : 兩個欄位的值彼此矛盾 (或與該欄位的定義矛盾)，其中至少一個很可能填錯。
note     : 不一定是錯，但會影響結果或不影響結果，值得讓核對者知道
           (例如模型會把你填的 TP53loh=0 覆寫成 1)。

規則依據
--------
* C01-C04 : 對應 ipssm R 套件 IPSSMprocess 對 TP53 欄位的處理 (TP53loh 的推論與 TP53multi 的判定)。
* C05-C08 : IPSS-R 細胞遺傳學風險分組的定義
              Very Good = -Y、del(11q)；Good = 正常核型、del(5q)、del(12p)、del(20q)、
              含 del(5q) 的雙異常；Intermediate = del(7q)、+8、+19、i(17q)、其他單一/雙異常；
              Poor = -7、inv(3)/t(3q)/del(3q)、含 -7/del(7q) 的雙異常、複雜核型 (3 個異常)；
              Very Poor = 複雜核型 (> 3 個異常)。
            因此 complex=1 至少是 Poor、Very Poor 必為複雜核型、del(7q)/-7 至少是 Intermediate、
            del(5q) 不會是 Very Good。
            這些欄位若由核型字串自動解析產生，彼此本來就一致；矛盾通常來自人工填寫。
* C09     : IPSS-M 以 min(BM_BLAST, 20)/5 計算，blast > 20% 時模型以 20% 計。

不包含「臨床正常值」檢查 (例如 HB 多高算不合理)，因為沒有權威的 MDS 參考範圍可依據；
輸入範圍檢查 (HB 4-20 等) 另見 ipssm_pipeline.VALIDATION_RULES。
"""
from typing import Dict, List, Optional

_MISSING = {'', 'na', 'n/a', 'nan', 'none', '.'}
LOW_CYTO = ('Very Good', 'Good', 'Intermediate')
TP53_LOH_VAF_THRESHOLD = 0.55          # 與 ipssm 套件 maxvafloh 預設值一致

# Rule 說明 (供文件與報告使用): id -> (severity, 說明)
CONSISTENCY_RULES = {
    'C01': ('conflict', 'TP53mut = 0 (無突變) 但 TP53maxvaf > 0'),
    'C02': ('conflict', 'TP53mut ≥ 1 (有突變) 但 TP53maxvaf = 0'),
    'C03': ('note', 'TP53mut = 0 但 TP53loh = 1 (對結果沒有影響)'),
    'C04': ('note', 'TP53mut = 1 且 TP53loh = 0，但 VAF > 0.55 或 del17_17p = 1：模型會把 TP53loh 覆寫為 1 '
                    '(判定為 multi-hit，分數明顯變高)'),
    'C05': ('conflict', 'complex = 1 (複雜核型) 但 CYTO_IPSSR 是 Very Good / Good / Intermediate'),
    'C06': ('conflict', 'CYTO_IPSSR = Very Poor (複雜核型 >3 個異常) 但 complex = 0'),
    'C07': ('conflict', 'del7_7q = 1 但 CYTO_IPSSR 是 Very Good / Good'),
    'C08': ('conflict', 'del5q = 1 但 CYTO_IPSSR = Very Good'),
    'C09': ('note', 'BM_BLAST > 20%：模型以 20% 計算 (已超出 MDS 範圍)'),
}


def _val(row: Dict[str, str], col: str) -> Optional[str]:
    """回傳去除空白的字串；缺失回傳 None。"""
    v = row.get(col)
    if v is None:
        return None
    text = str(v).strip()
    return None if text.lower() in _MISSING else text


def _num(row: Dict[str, str], col: str) -> Optional[float]:
    v = _val(row, col)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _flag(rule_id: str, fields: List[str], detail: str) -> Dict[str, str]:
    severity, _ = CONSISTENCY_RULES[rule_id]
    return {'Rule': rule_id, 'Severity': severity, 'Fields': ', '.join(fields), 'Message': detail}


def check_consistency(row: Dict[str, str]) -> List[Dict[str, str]]:
    """
    檢查單一病患 (已正規化的標準欄位列)，回傳旗標清單。
    只在「相關欄位都有值」時才判斷；缺失值不會觸發任何旗標 (缺失由另一套機制處理)。
    """
    flags: List[Dict[str, str]] = []
    tp53mut = _val(row, 'TP53mut')
    vaf = _num(row, 'TP53maxvaf')
    loh = _val(row, 'TP53loh')
    del17p = _val(row, 'del17_17p')
    cyto = _val(row, 'CYTO_IPSSR')
    complex_k = _val(row, 'complex')
    del7 = _val(row, 'del7_7q')
    del5 = _val(row, 'del5q')
    blast = _num(row, 'BM_BLAST')

    if tp53mut == '0' and vaf is not None and vaf > 0:
        flags.append(_flag('C01', ['TP53mut', 'TP53maxvaf'],
                           f"TP53mut=0 但 TP53maxvaf={row.get('TP53maxvaf')}"))
    if tp53mut in ('1', '2 or more') and vaf is not None and vaf == 0:
        flags.append(_flag('C02', ['TP53mut', 'TP53maxvaf'],
                           f"TP53mut={tp53mut} 但 TP53maxvaf=0"))
    if tp53mut == '0' and loh == '1':
        flags.append(_flag('C03', ['TP53mut', 'TP53loh'], "TP53mut=0 但 TP53loh=1；模型在 TP53mut=0 時不使用 TP53loh"))
    if tp53mut == '1' and loh == '0':
        reasons = []
        if vaf is not None and vaf > TP53_LOH_VAF_THRESHOLD:
            reasons.append(f"TP53maxvaf={row.get('TP53maxvaf')} > {TP53_LOH_VAF_THRESHOLD}")
        if del17p == '1':
            reasons.append("del17_17p=1")
        if reasons:
            flags.append(_flag('C04', ['TP53mut', 'TP53loh', 'TP53maxvaf', 'del17_17p'],
                               f"TP53loh=0 會被模型覆寫為 1 ({'; '.join(reasons)})，TP53 判為 multi-hit"))
    if complex_k == '1' and cyto in LOW_CYTO:
        flags.append(_flag('C05', ['complex', 'CYTO_IPSSR'],
                           f"complex=1 但 CYTO_IPSSR={cyto} (複雜核型至少為 Poor)"))
    if cyto == 'Very Poor' and complex_k == '0':
        flags.append(_flag('C06', ['CYTO_IPSSR', 'complex'],
                           "CYTO_IPSSR=Very Poor 代表複雜核型 (>3 個異常) 但 complex=0"))
    if del7 == '1' and cyto in ('Very Good', 'Good'):
        flags.append(_flag('C07', ['del7_7q', 'CYTO_IPSSR'],
                           f"del7_7q=1 但 CYTO_IPSSR={cyto} (-7 / del(7q) 至少為 Intermediate)"))
    if del5 == '1' and cyto == 'Very Good':
        flags.append(_flag('C08', ['del5q', 'CYTO_IPSSR'],
                           "del5q=1 但 CYTO_IPSSR=Very Good (del(5q) 屬 Good)"))
    if blast is not None and blast > 20:
        flags.append(_flag('C09', ['BM_BLAST'], f"BM_BLAST={row.get('BM_BLAST')}%，模型以 20% 計算"))
    return flags


def flag_ids(flags: List[Dict[str, str]]) -> str:
    """旗標清單 -> 'C01;C05'，沒有旗標回傳 'NONE'。"""
    return ';'.join(f['Rule'] for f in flags) if flags else 'NONE'
