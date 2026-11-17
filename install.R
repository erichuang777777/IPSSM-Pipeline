# ============================================
# 本地安裝 IPSS-M 官方 R 套件 (papaemmelab/ipssm)
# 用法: Rscript install.R
# ============================================
if (!requireNamespace("remotes", quietly = TRUE)) {
  install.packages("remotes", repos = "https://cloud.r-project.org")
}
if (!requireNamespace("ipssm", quietly = TRUE)) {
  cat("[install.R] Installing ipssm from GitHub (first time may take several minutes)...\n")
  remotes::install_github("papaemmelab/ipssm", upgrade = "never")
}
if (requireNamespace("ipssm", quietly = TRUE)) {
  cat("[install.R] SUCCESS: ipssm package is ready.\n")
} else {
  cat("[install.R] FAILED: ipssm installation failed.\n")
  quit(status = 1)
}
