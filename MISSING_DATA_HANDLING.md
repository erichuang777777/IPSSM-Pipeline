# IPSS-M Missing Data Handling & Uncertainty Calculation

## Overview

When patient data contains missing values (e.g., untested genes, missing cytogenetic results), IPSS-M doesn't fail but instead uses **Scenario Analysis** to compute three possible risk scores. This approach is the core strength of IPSS-M's robustness.

## Three Scenario Calculation

When data is missing, IPSS-M calculates three risk score scenarios:

### 1. Best Scenario (最佳情況)
- Assumes all missing negative factors (disease-causing gene mutations) are **negative (0)**
- Continuous variables (e.g., Hemoglobin) are set to optimal population levels
- **Represents the minimum possible risk**

### 2. Worst Scenario (最差情況)
- Assumes all missing negative factors are **positive (1)**
- Represents the maximum possible risk
- **Represents the maximum possible risk**

### 3. Mean Scenario (平均/預期情況)
- **This is the most important metric**
- NOT simply (Best + Worst) / 2
- Uses **weighted probability** based on population prevalence (`meanValues`)
- For each missing factor, the system uses the population-level probability of that feature

## Example: Using meanValues for Missing Genes

In the R function parameters:

```r
meanValues = c(
  HB1 = 9.87,           # If Hb is missing, use 9.87 as estimate
  TP53multi = 0.071,    # If TP53 untested, assume 7.1% probability of multi-mutation
  IDH2 = 0.0429,        # If IDH2 untested, assume 4.29% probability of mutation
  FLT3 = 0.0108,        # If FLT3 untested, assume 1.08% probability of mutation
  ...
)
```

When you don't test TP53, the system doesn't guess randomly—it uses real population statistics: ~7.1% of MDS patients have TP53 multi-mutations.

## Determining Uncertainty: The Range-Max Rule

This is where **range.max** parameter becomes crucial:

### Calculation Formula

```
ΔRisk = Worst Score - Best Score
```

### Decision Logic

| Condition | Result | Interpretation |
|-----------|--------|-----------------|
| ΔRisk ≤ range.max (default 1.0) | **CONFIDENT** | Insufficient uncertainty; Mean Scenario is reliable |
| ΔRisk > range.max (default 1.0) | **UNCERTAIN** | Too much missing data; results are ambiguous |

### What This Means

- **ΔRisk ≤ 1**: The impact of missing data is minor. Even in worst-case scenarios, the risk category doesn't shift significantly. → Use Mean Scenario result confidently

- **ΔRisk > 1**: Critical data is missing. Depending on unknown values, the patient could shift across multiple risk categories. → Mark as UNCERTAIN; may require follow-up testing

## IPSSMannotate Function Logic

The R function `IPSSMannotate()` performs this consolidation:

```
IF no missing data:
    CERTAIN: Best = Mean = Worst (result is definite)

ELSE IF ΔRisk ≤ 1:
    CONFIDENT: Output Mean Scenario score & category
    (system is confident enough to report a single result)

ELSE (ΔRisk > 1):
    UNCERTAIN: Output includes score range; category may be ambiguous
    (insufficient data to provide reliable single classification)
```

## Real-World Example

### Patient ID: 3113041
- Missing: CYTO_IPSSR, some gene mutations
- Best Score: -0.50 (Low risk)
- Mean Score: 0.00 (Moderate Low)
- Worst Score: 0.49 (Moderate Low)
- **Range: 0.99** (< 1.0) → **CONFIDENT**
- Result: Mean Scenario is reported reliably

### Patient ID: 2786746  
- Missing: Multiple cytogenetic values + key genes
- Best Score: -0.80
- Mean Score: 0.25
- Worst Score: 1.58
- **Range: 2.38** (> 1.0) → **UNCERTAIN**
- Result: Cannot confidently report single category; may need additional testing

## Parameters Explained

In the `IPSSMwrapper()` function:

### betaValues
- **Definition**: Regression coefficients (weights) for each risk factor
- **Usage**: Used to calculate the linear combination of risk scores
- **Example**: `TP53multi = 1.18` means TP53 multi-mutation carries 1.18 units of risk weight

### meanValues  
- **Definition**: Population-level prevalence for each factor
- **Usage**: Used to fill in missing data with realistic estimates
- **Example**: `TP53multi = 0.071` = 7.1% of population has TP53 multi-mutations
- **Critical for**: Computing the Mean Scenario when data is unavailable

### bestValues & worstValues
- **Definition**: Optimal and worst-case values for each factor
- **Usage**: Define the boundaries of the Best and Worst scenarios
- **Example**: `BLAST5 = 0` (best) to `4` (worst)

### range.max
- **Definition**: Threshold for acceptable uncertainty range
- **Default**: 1.0
- **Usage**: If (Worst - Best) ≤ range.max, report Mean result as CONFIDENT
- **Interpretation**: Allows clinician to decide what level of uncertainty is acceptable

## Clinical Implications

### When You See CONFIDENT ✅
- Missing data is minimal or not critical
- The result is reliable for clinical decision-making
- You can report the single risk score/category

### When You See UNCERTAIN ⚠️
- Multiple important data points are missing
- The result range spans multiple risk categories
- **Action**: Consider:
  1. Follow-up testing for missing genes (especially TP53, complex karyotype)
  2. Repeat cytogenetic analysis if not recently done
  3. Use result cautiously; patient management may need flexibility

## Data Quality Recommendations

For optimal IPSS-M results, ensure you have:

### Essential (Core Genes - 15 genes in genesRes)
```
BCOR, BCORL1, CEBPA, ETNK1, GATA2, GNB1, IDH1, NF1,
PHF6, PPM1D, PRPF8, PTPN11, SETBP1, STAG2, WT1
```

### Important (Other genes with high beta values)
- TP53 (multi-hit status) - highest weight (1.18)
- FLT3-ITD - weight 0.798
- Complex karyotype - weight 0.287

### Basic Clinical Data  
- Hemoglobin (HB)
- Platelet count (PLT)
- Bone marrow blast percentage (BM_BLAST)

## References

- **IPSSMwrapper()**: Main IPSS-M risk calculation function
- **IPSSMannotate()**: Consolidation function that applies range.max logic
- **range.max parameter**: Controls confidence threshold (default = 1.0)

## Next Steps

If you have many UNCERTAIN results:

1. **Review missing data pattern**: Which genes/tests are most frequently missing?
2. **Prioritize testing**: Focus on genes with highest beta values (TP53, FLT3, complex karyotype)
3. **Re-analyze**: Rerun translator_v3.py after obtaining missing data
4. **Clinical validation**: Compare uncertain results with actual patient outcomes

---

*This documentation explains the mathematical and statistical foundation of IPSS-M's robustness in handling incomplete data while maintaining clinical reliability.*
