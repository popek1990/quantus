#!/usr/bin/env bash
# Refuse to let personal data into this repository. Run by the pre-commit hook (tools/install-hooks.sh),
# and by hand before any push:  tools/scan-secrets.sh   (every tracked file)
#
# It looks for the two things that must never be committed here:
#   1. Quantus addresses (qz...). Allowed: the author's public tip address, and the addresses in
#      */tests/vectors.json - those are derived from the published BIP39 test phrase, and each tool's
#      --self-test proves it by deriving them again.
#   2. Long runs of BIP39 words - i.e. something shaped like a seed phrase (tools/seedlike.py).
#
# Usage: tools/scan-secrets.sh [--staged | <path>...]
set -euo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"

# The author's own public tip address is allowed to appear in this repository.
TIP='qzpaZ83dYsCyDdCcTgDhFj3WCPuPnvaEXJjj4FECDe9i4xWE9'

if [[ ${1-} == --staged ]]; then
    mapfile -t files < <(git diff --cached --name-only --diff-filter=ACMR)
elif (($#)); then
    files=("$@")
else
    mapfile -t files < <(git ls-files)
fi

allowed=$(mktemp)
trap 'rm -f "$allowed"' EXIT
printf '%s\n' "$TIP" > "$allowed"
for vectors in */tests/vectors.json; do
    [[ -f $vectors ]] && command grep -aoE 'qz[1-9A-HJ-NP-Za-km-z]{44,}' "$vectors" >> "$allowed" || true
done

bad=0
for f in "${files[@]}"; do
    [[ -f $f ]] || continue
    # binary files are NOT skipped: one NUL byte would otherwise hide a whole seed phrase from both checks
    # -o so each ADDRESS is checked on its own: someone else's address cannot ride along on a line
    # that also holds an allowed one
    hits=$(command grep -aoE 'qz[1-9A-HJ-NP-Za-km-z]{44,}' "$f" | command grep -vxFf "$allowed" || true)
    if [[ -n $hits ]]; then
        echo "REFUSED: $f contains what looks like a Quantus address (first characters shown):" >&2
        printf '%s\n' "$hits" | cut -c1-8 | sed -n '1,3s/$/.../p' >&2
        bad=1
    fi
    python3 "$root/tools/seedlike.py" "$f" || bad=1
done
exit "$bad"
