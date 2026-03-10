#!/usr/bin/env python
"""
Universal Cohort Converter
Converts any cohort format (FJUH, HSCT, etc.) to standard IPSSM format.
Handles missing columns, different column orders, and name variations.
"""

import pandas as pd
import argparse
from pathlib import Path
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)-8s | %(message)s'
)
logger = logging.getLogger(__name__)

# Standard IPSSM columns (42 columns)
STANDARD_IPSSM_COLUMNS = [
    'ID', 'HB', 'PLT', 'BM_BLAST', 'del5q', 'del7_7q', 'TP53loh', 'TP53mut',
    'BCOR', 'BCORL1', 'CEBPA', 'ETNK1', 'GATA2', 'GNB1', 'IDH1', 'NF1',
    'PHF6', 'PPM1D', 'PRPF8', 'PTPN11', 'SETBP1', 'STAG2', 'WT1',
    'FLT3', 'MLL_PTD', 'SF3B1_5q', 'NPM1', 'RUNX1', 'NRAS', 'ETV6', 'IDH2',
    'CBL', 'EZH2', 'U2AF1', 'SRSF2', 'DNMT3A', 'ASXL1', 'KRAS', 'SF3B1_alpha',
    'CYTO_IPSSR', 'IPSS_M_'
]

# Column name variations and aliases
COLUMN_ALIASES = {
    'ID': ['ID', 'Patient_ID', 'PatientID', 'patient_id', 'PID'],
    'HB': ['HB', 'Hemoglobin', 'hemoglobin', 'Hb'],
    'PLT': ['PLT', 'Platelet', 'platelets', 'platelet_count'],
    'BM_BLAST': ['BM_BLAST', 'Blast', 'BM Blast', 'bone marrow blast', 'BM_Blast'],
    'del5q': ['del5q', 'del(5q)', 'DEL5Q', 'Deletion 5q'],
    'del7_7q': ['del7_7q', 'del(7)', 'del(7q)', 'DEL7', 'Deletion 7'],
    'TP53loh': ['TP53loh', 'TP53 LOH', 'TP53_LOH', 'TP53-LOH'],
    'TP53mut': ['TP53mut', 'TP53', 'TP53_Mutation', 'TP53 mutation'],
    'BCOR': ['BCOR'],
    'BCORL1': ['BCORL1'],
    'CEBPA': ['CEBPA'],
    'ETNK1': ['ETNK1'],
    'GATA2': ['GATA2'],
    'GNB1': ['GNB1'],
    'IDH1': ['IDH1'],
    'NF1': ['NF1'],
    'PHF6': ['PHF6'],
    'PPM1D': ['PPM1D'],
    'PRPF8': ['PRPF8'],
    'PTPN11': ['PTPN11'],
    'SETBP1': ['SETBP1'],
    'STAG2': ['STAG2'],
    'WT1': ['WT1'],
    'FLT3': ['FLT3', 'FLT3-ITD', 'FLT3_ITD'],
    'MLL_PTD': ['MLL_PTD', 'MLL-PTD', 'MLL PTD'],
    'SF3B1_5q': ['SF3B1_5q', 'SF3B1 5q'],
    'NPM1': ['NPM1'],
    'RUNX1': ['RUNX1'],
    'NRAS': ['NRAS'],
    'ETV6': ['ETV6'],
    'IDH2': ['IDH2'],
    'CBL': ['CBL'],
    'EZH2': ['EZH2'],
    'U2AF1': ['U2AF1'],
    'SRSF2': ['SRSF2'],
    'DNMT3A': ['DNMT3A'],
    'ASXL1': ['ASXL1'],
    'KRAS': ['KRAS'],
    'SF3B1_alpha': ['SF3B1_alpha', 'SF3B1-alpha'],
    'CYTO_IPSSR': ['CYTO_IPSSR', 'CYTO_IPSS-R', 'CYTO IPSS-R', 'Cytogenetic IPSS-R'],
    'IPSS_M_': ['IPSS_M_', 'IPSS_M', 'IPSSM', 'IPSS-M']
}

def detect_cohort_type(df):
    """Detect cohort type based on column names"""
    columns_lower = [str(col).lower().strip() for col in df.columns]
    
    fjuh_markers = ['ethnicity', 'diagnosis', 'karyotype']
    hsct_markers = ['transplant', 'graft', 'donor', 'conditioning']
    
    fjuh_count = sum(1 for marker in fjuh_markers if any(marker in col for col in columns_lower))
    hsct_count = sum(1 for marker in hsct_markers if any(marker in col for col in columns_lower))
    
    if hsct_count > fjuh_count:
        return "HSCT"
    elif fjuh_count > 0:
        return "FJUH"
    else:
        return "UNKNOWN"

def find_column_mapping(input_df):
    """Find mapping from input columns to standard IPSSM columns"""
    mapping = {}
    used_cols = set()
    
    for std_col, aliases in COLUMN_ALIASES.items():
        for input_col in input_df.columns:
            if input_col in used_cols:
                continue
            
            col_clean = str(input_col).strip()
            col_upper = col_clean.upper()
            
            # Exact match (case-insensitive)
            for alias in aliases:
                if col_upper == alias.upper():
                    mapping[input_col] = std_col
                    used_cols.add(input_col)
                    break
            
            if input_col in used_cols:
                break
    
    return mapping

def scan_cohort(file_path):
    """Scan and analyze cohort file"""
    logger.info(f"Scanning: {file_path}")
    
    df = pd.read_excel(file_path) if str(file_path).endswith('.xlsx') else pd.read_csv(file_path)
    
    cohort_type = detect_cohort_type(df)
    logger.info(f"Detected cohort type: {cohort_type}")
    
    logger.info(f"\n=== COLUMN ANALYSIS ===")
    logger.info(f"Total columns: {len(df.columns)}")
    logger.info(f"Total rows: {len(df)}")
    
    logger.info(f"\nInput columns (in order):")
    for i, col in enumerate(df.columns, 1):
        logger.info(f"  {i:2d}. {col}")
    
    # Find mapping
    mapping = find_column_mapping(df)
    
    logger.info(f"\n=== COLUMN MAPPING ===")
    logger.info(f"Matched columns: {len(mapping)}/{len(df.columns)}")
    
    for input_col, std_col in mapping.items():
        logger.info(f"  ✓ {input_col:25s} → {std_col}")
    
    # Unmatched columns
    unmatched = set(df.columns) - set(mapping.keys())
    if unmatched:
        logger.info(f"\nUnmatched columns ({len(unmatched)}):")
        for col in unmatched:
            logger.info(f"  ! {col}")
    
    # Check for missing IPSSM columns
    mapped_std_cols = set(mapping.values())
    missing = set(STANDARD_IPSSM_COLUMNS) - mapped_std_cols
    
    if missing:
        logger.info(f"\nMissing IPSSM columns ({len(missing)}):")
        for col in sorted(missing):
            logger.info(f"  - {col}")
    
    # Data quality
    logger.info(f"\n=== DATA QUALITY ===")
    for col in df.columns:
        null_count = df[col].isnull().sum()
        if null_count > 0:
            pct = (null_count / len(df)) * 100
            logger.info(f"  {col:25s} : {null_count:3d} missing ({pct:5.1f}%)")
    
    return df, mapping, cohort_type

def convert_cohort(input_file, output_file=None):
    """Convert cohort to standard IPSSM format"""
    df, mapping, cohort_type = scan_cohort(input_file)
    
    # Rename columns
    df_converted = df[list(mapping.keys())].rename(columns=mapping)
    
    # Add missing columns with NA
    missing_cols = set(STANDARD_IPSSM_COLUMNS) - set(df_converted.columns)
    for col in missing_cols:
        df_converted[col] = 'NA'
    
    # Reorder to standard format
    df_converted = df_converted[STANDARD_IPSSM_COLUMNS]
    
    # Save
    if output_file is None:
        input_path = Path(input_file)
        output_file = input_path.parent / f"{input_path.stem}_converted.csv"
    
    df_converted.to_csv(output_file, index=False)
    logger.info(f"\n✓ Converted file saved: {output_file}")
    logger.info(f"  Rows: {len(df_converted)}, Columns: {len(df_converted.columns)}")
    
    return df_converted

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Universal Cohort Converter to IPSSM format')
    parser.add_argument('input_file', help='Input cohort file (Excel or CSV)')
    parser.add_argument('-o', '--output', help='Output CSV file')
    parser.add_argument('--scan-only', action='store_true', help='Only scan, do not convert')
    
    args = parser.parse_args()
    
    if args.scan_only:
        scan_cohort(args.input_file)
    else:
        convert_cohort(args.input_file, args.output)
