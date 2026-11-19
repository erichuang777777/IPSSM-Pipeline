"""讀取「使用者提供的原始檔案」(Excel 多分頁 / CSV) 並轉成單一資料表。

處理原始 Excel 常見狀況
----------------------
* **多分頁**: 依「欄名能對應到幾個 IPSS-M 標準欄位」為每個分頁評分，自動選出病患資料頁。
  兩頁分數相同 (例如 2023、2024 兩批) 時不猜測，要求使用者用 --sheet 指定。
* **多分頁合併**: --sheet 指定多頁時
    - stack: 結構相同的分頁上下串接 (例如不同年度/批次)
    - merge: 內容不同的分頁依病患 ID 合併 (例如「臨床」頁 + 「NGS 基因」頁)
  未指定 --sheet-join 時依欄位重疊程度自動判斷；判斷不了就要求指定。
* **表頭不在第一列**: 掃描前 HEADER_SCAN_ROWS 列，取能對應最多標準欄位的那一列當表頭
  (常見於上方有標題、日期、單位說明列的匯出檔)。
* **隱藏分頁**: 不參與自動選擇 (可用 --sheet 明確指定)。
* **儲存格格式**: 讀取公式的計算結果 (data_only)；百分比格式的儲存格 (畫面顯示 9%、
  實際存 0.09) 會還原成 "9%"，再交給正規化規則處理，避免 blast 被讀成 0.09。
  整數型浮點數 (1.0) 輸出為 "1"；TRUE/FALSE 儲存格輸出為文字。
* **CSV 編碼**: 依序嘗試 UTF-8 (含 BOM)、cp950 (Big5，繁中 Windows Excel 另存 CSV 的預設)。

每一列都帶有來源位置 (META_SHEET / META_ROW)，供逐欄位稽核標示「哪一頁第幾列」。
"""
import csv
import datetime as _dt
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

META_SHEET = '__sheet__'
META_ROW = '__row__'
META_COLUMNS = (META_SHEET, META_ROW)

EXCEL_SUFFIXES = {'.xlsx', '.xlsm'}
TEXT_SUFFIXES = {'.csv', '.tsv', '.txt'}
CSV_ENCODINGS = ('utf-8-sig', 'cp950')
HEADER_SCAN_ROWS = 20
# 分頁至少要對應到這麼多個標準欄位，才視為「看起來是病患資料」
DATA_SHEET_MIN_MATCHES = 3
SHEET_JOINS = ('stack', 'merge')
# 自動選擇單一分頁時，該分頁必須具備的臨床欄位
CLINICAL_REQUIRED = ('HB', 'PLT', 'BM_BLAST')


class SourceSelectionError(ValueError):
    """來源檔案/分頁無法明確決定時拋出 (訊息會告訴使用者該怎麼指定)。"""


# ---------------------------------------------------------------------------
#  讀取原始格子 (grid = list[list[str]])
# ---------------------------------------------------------------------------

def _format_number(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return format(value, '.15g')
    return str(value)


def _cell_to_str(value, number_format=''):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        if '%' in (number_format or ''):
            return _format_number(value * 100) + '%'
        return _format_number(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    return str(value).strip()


def _read_excel_grids(path):
    """回傳 [{'name', 'state', 'grid'}]，依活頁簿中的分頁順序。"""
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    sheets = []
    for ws in wb.worksheets:
        grid = [[_cell_to_str(c.value, c.number_format) for c in row] for row in ws.iter_rows()]
        sheets.append({'name': ws.title, 'state': ws.sheet_state, 'grid': grid})
    return sheets


def _read_text_grid(path):
    """回傳 (grid, encoding)。"""
    delimiter = '\t' if Path(path).suffix.lower() == '.tsv' else ','
    last_error = None
    for enc in CSV_ENCODINGS:
        try:
            with open(path, 'r', encoding=enc, newline='') as f:
                grid = [[c.strip() for c in row] for row in csv.reader(f, delimiter=delimiter)]
            return grid, enc
        except UnicodeDecodeError as e:
            last_error = e
    raise SourceSelectionError(
        f"無法辨識 {Path(path).name} 的文字編碼 (試過 {', '.join(CSV_ENCODINGS)})。"
        f"請在 Excel 另存為「CSV UTF-8」或直接提供 .xlsx。({last_error})")


# ---------------------------------------------------------------------------
#  表頭偵測 / 轉成 DataFrame
# ---------------------------------------------------------------------------

def _dedupe(names):
    seen, out = {}, []
    for i, n in enumerate(names):
        n = n or f'Unnamed_{i + 1}'
        if n in seen:
            seen[n] += 1
            n = f"{n}.{seen[n]}"
        else:
            seen[n] = 0
        out.append(n)
    return out


def _detect_header(grid, map_columns):
    """回傳 (表頭列 index, 該列對應到的標準欄位數)。全部為 0 時取第一個非空列。"""
    best_idx, best_score = None, 0
    for idx, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        if not any(row):
            continue
        if best_idx is None:
            best_idx = idx
        score = len(map_columns(pd.DataFrame(columns=_dedupe(row))))
        if score > best_score:
            best_idx, best_score = idx, score
    return (best_idx if best_idx is not None else 0), best_score


def _grid_to_frame(grid, header_idx, sheet_name):
    """表頭以下的列轉成 DataFrame (全字串)，並加上來源位置欄；去除全空的列與欄。"""
    if not grid:
        return pd.DataFrame(columns=list(META_COLUMNS))
    width = max(len(r) for r in grid)
    padded = [list(r) + [''] * (width - len(r)) for r in grid]
    header = _dedupe(padded[header_idx])
    records, rownums = [], []
    for offset, row in enumerate(padded[header_idx + 1:], start=header_idx + 2):
        if any(row):
            records.append(row)
            rownums.append(offset)        # 1-based 的 Excel / 檔案列號
    df = pd.DataFrame(records, columns=header, dtype=str)
    # 去除表頭空白且整欄皆空的欄 (常見於 Excel 右側殘留格式)
    keep = [c for c in df.columns if not (c.startswith('Unnamed_') and (df[c] == '').all())]
    df = df[keep].copy()
    df[META_SHEET] = sheet_name
    df[META_ROW] = rownums
    return df


# ---------------------------------------------------------------------------
#  分頁選擇與合併
# ---------------------------------------------------------------------------

def _resolve_sheet_names(spec, sheets):
    """--sheet 字串 (名稱 / 1-based 序號 / 逗號分隔 / all) -> 分頁名稱清單。"""
    names = [s['name'] for s in sheets]
    if spec.strip().lower() == 'all':
        chosen = [s['name'] for s in sheets if s['looks_like_data'] and s['state'] == 'visible']
        if not chosen:
            raise SourceSelectionError("--sheet all: 沒有任何可見分頁看起來是病患資料。")
        return chosen
    chosen = []
    for token in [t.strip() for t in spec.split(',') if t.strip()]:
        if token in names:
            chosen.append(token)
        elif token.isdigit() and 1 <= int(token) <= len(names):
            chosen.append(names[int(token) - 1])
        else:
            raise SourceSelectionError(
                f"找不到分頁 '{token}'。可用的分頁: "
                + ', '.join(f"{i + 1}={n}" for i, n in enumerate(names)))
    if len(set(chosen)) != len(chosen):
        raise SourceSelectionError(f"--sheet 重複指定了同一個分頁: {spec}")
    return chosen


def _auto_select(sheets):
    """
    自動選擇單一分頁。只在「答案明確」時才選，否則要求使用者指定:
      * 只有一個可見分頁像病患資料 -> 選它
      * 多個分頁像病患資料 -> 只有當分數最高者具備臨床必填欄位 (HB/PLT/BM_BLAST)、
        且其他分頁的標準欄位都已包含在其中 (例如摘要頁) 時才選它；
        若其他分頁提供它沒有的欄位 (資料分散在多頁，例如臨床頁 + 基因頁) 或分數相同，
        則拋出 SourceSelectionError，請使用者用 --sheet 指定或合併。
    """
    visible = [s for s in sheets if s['state'] == 'visible' and s['rows'] > 0 and s['matched'] > 0]
    if not visible:
        raise SourceSelectionError(
            "沒有任何分頁的欄名能對應到 IPSS-M 標準欄位 (例如 HB / PLT / BM_BLAST / ID)。"
            "請用 --inspect 檢查欄名，或用 --sheet 指定分頁。")
    data_like = [s for s in visible if s['looks_like_data']]
    if len(data_like) <= 1:
        return [(data_like or sorted(visible, key=lambda s: -s['matched']))[0]['name']]

    def describe(s):
        clinical = [c for c in CLINICAL_REQUIRED if c in s['fields']]
        tag = '含 ' + '/'.join(clinical) if clinical else '無臨床欄位'
        return f"'{s['name']}' ({s['matched']} 個標準欄位, {s['rows']} 列, {tag})"

    ranked = sorted(data_like, key=lambda s: -s['matched'])
    best, others = ranked[0], ranked[1:]
    best_fields = set(best['fields'])
    has_clinical = all(c in best_fields for c in CLINICAL_REQUIRED)
    tie = others[0]['matched'] == best['matched']
    adds_fields = any(set(o['fields']) - best_fields - {'ID'} for o in others)
    if has_clinical and not tie and not adds_fields:
        return [best['name']]

    hint = ("資料可能分散在多個分頁 (例如臨床頁 + 基因頁)，請用 --sheet A,B 依 ID 合併"
            if adds_fields and not tie else
            "若是不同批次的同格式資料，請用 --sheet all 或 --sheet A,B 串接")
    raise SourceSelectionError(
        "有多個分頁看起來都是病患資料，無法自動決定: " + '; '.join(describe(s) for s in ranked)
        + f"。{hint}；或用 --sheet 指定單一分頁。")


def _infer_join(frames, map_columns):
    """依各分頁對應到的標準欄位 (不含 ID) 判斷 stack 或 merge。"""
    sets = [set(map_columns(df.drop(columns=list(META_COLUMNS))).values()) - {'ID'}
            for df in frames]
    if all(s == sets[0] for s in sets):
        return 'stack'
    overlap = set.intersection(*sets)
    if not overlap:
        return 'merge'
    raise SourceSelectionError(
        "選定的分頁欄位部分重疊 (" + ', '.join(sorted(overlap)) + ")，無法自動判斷要上下串接"
        "還是依 ID 合併。請加上 --sheet-join stack 或 --sheet-join merge。")


def _standardize_frame(df, map_columns):
    """把分頁欄名改成標準欄名 (未對應的欄位保留原名)。"""
    mapping = map_columns(df.drop(columns=list(META_COLUMNS)))
    return df.rename(columns=mapping), mapping


def _merge_frames(named_frames, map_columns, warnings):
    """依 ID 合併多個分頁 (outer join，保留第一頁的病患順序)。"""
    merged = None
    for name, df in named_frames:
        std, mapping = _standardize_frame(df, map_columns)
        if 'ID' not in std.columns:
            raise SourceSelectionError(f"分頁 '{name}' 找不到病患 ID 欄位，無法依 ID 合併。")
        std['ID'] = std['ID'].astype(str).str.strip()
        blank = std['ID'] == ''
        if blank.any():
            if merged is None:
                # 第一頁的無 ID 列仍保留 (給臨時鍵避免被當成同一人合併)，之後還原為空白
                std.loc[blank, 'ID'] = [f'__noid_{r}' for r in std.loc[blank, META_ROW]]
                warnings.append(f"合併: 分頁 '{name}' 有 {int(blank.sum())} 列沒有 ID，"
                                f"無法和其他分頁對應，只使用本頁資料。")
            else:
                std = std[~blank]
                warnings.append(f"合併: 分頁 '{name}' 有 {int(blank.sum())} 列沒有 ID，已略過。")
        dup = std['ID'][std['ID'].duplicated()].unique().tolist()
        if dup:
            raise SourceSelectionError(
                f"分頁 '{name}' 有重複的 ID，無法可靠地依 ID 合併: {', '.join(dup[:10])}")
        if merged is None:
            merged = std
            continue
        overlap = [c for c in std.columns if c in merged.columns and c not in ('ID',) + META_COLUMNS]
        right = std.drop(columns=list(META_COLUMNS))
        combined = merged.merge(right, on='ID', how='outer', suffixes=('', '__r'), indicator=True)

        only_left = combined.loc[combined['_merge'] == 'left_only', 'ID'].tolist()
        only_right = combined.loc[combined['_merge'] == 'right_only', 'ID'].tolist()
        if only_left:
            warnings.append(f"合併: {len(only_left)} 位病患在分頁 '{name}' 中找不到 (該頁欄位將為缺失): "
                            + ', '.join(only_left[:10]))
        if only_right:
            warnings.append(f"合併: {len(only_right)} 位病患只出現在分頁 '{name}': "
                            + ', '.join(only_right[:10]))
            # 只出現在右側分頁者，來源位置改標為右側分頁
            src = std.set_index('ID')
            mask = combined['_merge'] == 'right_only'
            combined.loc[mask, META_SHEET] = name
            combined.loc[mask, META_ROW] = combined.loc[mask, 'ID'].map(src[META_ROW])

        for col in overlap:
            left, right_col = combined[col].fillna(''), combined[f'{col}__r'].fillna('')
            conflict = (left != '') & (right_col != '') & (left != right_col)
            if conflict.any():
                ids = combined.loc[conflict, 'ID'].tolist()
                warnings.append(f"合併衝突: 欄位 {col} 在分頁 '{name}' 與先前分頁的值不同 "
                                f"({len(ids)} 位病患，保留先前分頁的值): {', '.join(ids[:10])}")
            combined[col] = left.where(left != '', right_col)
            combined = combined.drop(columns=[f'{col}__r'])
        merged = combined.drop(columns=['_merge'])
    merged = merged.fillna('')
    merged['ID'] = merged['ID'].where(~merged['ID'].astype(str).str.startswith('__noid_'), '')
    return merged


# ---------------------------------------------------------------------------
#  公開介面
# ---------------------------------------------------------------------------

def load_table(path, map_columns: Callable[[pd.DataFrame], Dict[str, str]],
               sheet: Optional[str] = None, sheet_join: Optional[str] = None):
    """
    讀取原始檔案並回傳 (df, info)。

    df   : 全字串 DataFrame，含 META_SHEET / META_ROW 兩個來源位置欄
    info : {'file_type', 'encoding', 'sheets': [...], 'selected': [...], 'join', 'warnings'}
    map_columns: 欄名 -> 標準欄名 的對應函式 (由 ipssm_pipeline.find_column_mapping 提供)
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if sheet_join is not None and sheet_join not in SHEET_JOINS:
        raise SourceSelectionError(f"--sheet-join 必須是 {SHEET_JOINS} 之一")

    info = {'file_type': suffix.lstrip('.'), 'encoding': None, 'sheets': [], 'selected': [],
            'join': None, 'warnings': []}

    if suffix in EXCEL_SUFFIXES:
        raw_sheets = _read_excel_grids(path)
    elif suffix == '.xls':
        raise SourceSelectionError(
            f"{path.name} 是舊版 .xls 格式，請在 Excel 另存為 .xlsx 後再執行。")
    elif suffix in TEXT_SUFFIXES:
        grid, enc = _read_text_grid(path)
        info['encoding'] = enc
        raw_sheets = [{'name': path.stem, 'state': 'visible', 'grid': grid}]
        if sheet:
            info['warnings'].append("--sheet 只適用於 Excel 檔，CSV 已忽略此參數。")
            sheet = None
    else:
        raise SourceSelectionError(
            f"不支援的檔案類型: {suffix or '(無副檔名)'}。支援 .xlsx / .xlsm / .csv / .tsv。")

    frames = {}
    for s in raw_sheets:
        header_idx, _ = _detect_header(s['grid'], map_columns)
        df = _grid_to_frame(s['grid'], header_idx, s['name'])
        fields = sorted(set(map_columns(df.drop(columns=list(META_COLUMNS))).values()))
        matched = len(fields)
        frames[s['name']] = df
        info['sheets'].append({
            'name': s['name'], 'state': s['state'], 'rows': len(df),
            'header_row': header_idx + 1, 'matched': matched, 'fields': fields,
            'looks_like_data': matched >= DATA_SHEET_MIN_MATCHES and len(df) > 0,
        })

    selected = _resolve_sheet_names(sheet, info['sheets']) if sheet else _auto_select(info['sheets'])
    info['selected'] = selected

    for s in info['sheets']:
        if s['name'] in selected:
            if s['state'] != 'visible':
                info['warnings'].append(f"分頁 '{s['name']}' 是隱藏分頁 ({s['state']})，依指定仍會讀取。")
            if s['header_row'] != 1:
                info['warnings'].append(f"分頁 '{s['name']}' 的表頭偵測在第 {s['header_row']} 列 "
                                        f"(其上方的列已略過)。")
        elif not sheet and s['looks_like_data'] and s['state'] == 'visible':
            info['warnings'].append(
                f"分頁 '{s['name']}' 也像病患資料 ({s['matched']} 個標準欄位, {s['rows']} 列) 但未被選用；"
                f"如需一起計算請用 --sheet。")

    if len(selected) == 1:
        return frames[selected[0]].reset_index(drop=True), info

    chosen = [frames[n] for n in selected]
    join = sheet_join or _infer_join(chosen, map_columns)
    info['join'] = join
    if join == 'stack':
        std = [_standardize_frame(df, map_columns)[0] for df in chosen]
        df = pd.concat(std, ignore_index=True, sort=False).fillna('')
    else:
        df = _merge_frames(list(zip(selected, chosen)), map_columns, info['warnings'])
    return df.reset_index(drop=True), info


def describe_source(info) -> List[str]:
    """把 load_table 的 info 轉成可讀的說明文字 (日誌 / 報告用)。"""
    lines = [f"檔案類型: {info['file_type']}" + (f" (編碼 {info['encoding']})" if info['encoding'] else '')]
    for s in info['sheets']:
        mark = '★ 使用' if s['name'] in info['selected'] else '  略過'
        hidden = '' if s['state'] == 'visible' else f" [{s['state']}]"
        lines.append(f"{mark} 分頁 '{s['name']}'{hidden}: 表頭第 {s['header_row']} 列, "
                     f"{s['rows']} 列資料, 對應 {s['matched']} 個標準欄位")
    if info['join']:
        lines.append(f"多分頁合併方式: {info['join']}")
    return lines
