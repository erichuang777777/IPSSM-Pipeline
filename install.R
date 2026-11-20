# ============================================
# 安裝 IPSS-M 官方 R 套件 (papaemmelab/ipssm)
# 用法: Rscript install.R        (或在網頁介面按「自動安裝」)
#
# 環境變數 IPSSM_INSTALL_SOURCE:
#   未設定 -> 從 GitHub 安裝 (需要網路)
#   設為本地 ipssm 原始碼資料夾 -> 離線安裝 (相依套件 car、readxl 仍需已安裝或能連 CRAN)
# ============================================

# 選一個「可以寫入」的函式庫位置：優先用系統預設；沒有權限時改用使用者自己的資料夾
# (R_LIBS_USER，R 下次啟動時會自動把它加進搜尋路徑，所以之後不需要任何額外設定)
choose_lib <- function() {
  for (l in .libPaths()) {
    if (dir.exists(l) && file.access(l, 2) == 0) return(l)
  }
  user <- Sys.getenv("R_LIBS_USER")
  if (!nzchar(user)) user <- file.path(path.expand("~"), "R", "library")
  user <- path.expand(user)
  dir.create(user, recursive = TRUE, showWarnings = FALSE)
  .libPaths(c(user, .libPaths()))
  user
}

lib <- choose_lib()
cat("[install.R] 安裝位置:", lib, "\n")

repos <- getOption("repos")
if (is.null(repos) || identical(unname(repos["CRAN"]), "@CRAN@")) {
  options(repos = c(CRAN = "https://cloud.r-project.org"))
}

if (!requireNamespace("remotes", quietly = TRUE)) {
  cat("[install.R] 安裝 remotes ...\n")
  install.packages("remotes", lib = lib)
}

if (!requireNamespace("ipssm", quietly = TRUE)) {
  source_dir <- Sys.getenv("IPSSM_INSTALL_SOURCE", "")
  if (nzchar(source_dir)) {
    cat("[install.R] 從本地資料夾安裝 ipssm:", source_dir, "\n")
    remotes::install_local(source_dir, lib = lib, upgrade = "never", force = TRUE)
  } else {
    cat("[install.R] 從 GitHub 安裝 ipssm (首次可能需要數分鐘，會一併編譯相依套件) ...\n")
    remotes::install_github("papaemmelab/ipssm", lib = lib, upgrade = "never")
  }
}

if (!requireNamespace("ipssm", quietly = TRUE)) {
  cat("[install.R] FAILED: ipssm installation failed.\n")
  quit(status = 1)
}

# ipssm 在計算時才會用到它宣告的相依套件 (例如 car)，載入套件時不會檢查；
# 這裡主動補齊，避免「裝好了卻在計算時才失敗」。
imports <- packageDescription("ipssm")$Imports
imports <- if (is.null(imports)) character(0) else trimws(strsplit(gsub("\\([^)]*\\)", "", imports), ",")[[1]])
imports <- imports[nzchar(imports)]
missing <- imports[!vapply(imports, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing)) {
  cat("[install.R] 補裝相依套件:", paste(missing, collapse = ", "), "\n")
  install.packages(missing, lib = lib)
  missing <- imports[!vapply(imports, requireNamespace, logical(1), quietly = TRUE)]
}
if (length(missing)) {
  cat("[install.R] FAILED: 相依套件安裝失敗:", paste(missing, collapse = ", "), "\n")
  quit(status = 1)
}
cat("[install.R] SUCCESS: ipssm package is ready.\n")
