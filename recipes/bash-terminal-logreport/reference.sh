set -euo pipefail
export LC_ALL=C
mkdir -p report
files=$(find logs -maxdepth 1 -type f -name '*.log' | sort)
awk '$1 ~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+$/ && $2 ~ /^[A-Z]+$/ && NF >= 3 {c[$2]++} END {for (l in c) print l, c[l]}' $files \
  | sort -k2,2nr -k1,1 > report/summary.txt
awk '$1 ~ /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+$/ && $2 == "ERROR" && NF >= 3 {sub(/^[^ ]+ [^ ]+ /, ""); print}' $files > report/errors.txt
