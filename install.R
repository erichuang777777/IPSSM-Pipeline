if (!require("remotes", quietly = TRUE)) {
  install.packages("remotes", repos = "http://cran.us.r-project.org")
}
if (!require("ipssm", quietly = TRUE)) {
  remotes::install_github("papaemmelab/ipssm")
}
