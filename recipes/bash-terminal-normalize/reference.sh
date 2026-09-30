set -euo pipefail
export LC_ALL=C
cd inbox
declare -A taken=()
mapfile -d '' names < <(find . -maxdepth 1 -type f ! -name INDEX -printf '%P\0' | sort -z)
index=""
for old in "${names[@]}"; do
  if [[ "$old" == *.* ]]; then stem="${old%.*}"; ext=".${old##*.}"; else stem="$old"; ext=""; fi
  stem=$(printf '%s' "$stem" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9.]+/-/g; s/^-+//; s/-+$//')
  ext=$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')
  new="$stem$ext"; n=2
  while [[ -n "${taken[$new]:-}" ]]; do new="$stem-$n$ext"; n=$((n+1)); done
  taken[$new]=1
  index+="$new $old"$'\n'
  if [[ "$old" != "$new" ]]; then mv -- "$old" ".tmp-$new"; fi
done
for f in .tmp-*; do [[ -e "$f" ]] && mv -- "$f" "${f#.tmp-}"; done
printf '%s' "$index" | sort -k1,1 > INDEX
