# ============================================
# IPSSM R 套件安裝腳本 (for Streamlit Cloud)
# ============================================
# 策略：
#   1. r-cran-remotes 已由 packages.txt 透過 apt 系統級安裝，不需 install.packages()
#   2. 建立使用者可寫的 R 函式庫目錄 ~/R/library
#   3. 將 ipssm 套件安裝到該可寫目錄

# 建立使用者可寫的函式庫目錄
user_lib <- path.expand("~/R/library")
dir.create(user_lib, recursive = TRUE, showWarnings = FALSE)
.libPaths(c(user_lib, .libPaths()))

cat("[install.R] R library paths:\n")
print(.libPaths())

# 檢查 remotes 是否可用 (由 apt r-cran-remotes 安裝)
if (!require("remotes", quietly = TRUE)) {
  cat("[install.R] ERROR: remotes package not found. It should be installed via packages.txt (r-cran-remotes).\n")
  # 嘗試在使用者目錄裡安裝 remotes 作為後備方案
  install.packages("remotes", lib = user_lib, repos = "https://cloud.r-project.org")
}

# 安裝 ipssm 套件到使用者可寫目錄
if (!require("ipssm", quietly = TRUE)) {
  cat("[install.R] Installing ipssm from GitHub...\n")
  remotes::install_github("papaemmelab/ipssm", lib = user_lib, upgrade = "never")
}

# 驗證安裝成功
if (require("ipssm", quietly = TRUE)) {
  cat("[install.R] SUCCESS: ipssm package is ready.\n")
} else {
  cat("[install.R] FAILED: ipssm installation failed.\n")
  quit(status = 1)
}
