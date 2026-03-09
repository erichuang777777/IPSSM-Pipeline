if (!require("devtools", quietly = TRUE)) {
  install.packages("devtools", repos = "http://cran.us.r-project.org")
}
if (!require("ipssm", quietly = TRUE)) {
  devtools::install_github("papaemmelab/ipssm")
}
