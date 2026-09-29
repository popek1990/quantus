#!/usr/bin/env bash
# Quantus airdrop checker - launcher. Community tool, not affiliated with the Quantus team.
#
# It asks for your seed phrases. That is exactly what scams do, so:
#   - read quantus_airdrop_checker.py before you run it (it is one file, and it is meant to be read);
#   - your phrases are never written to disk, never sent anywhere, and never shown on screen;
#   - no website of ours will ever ask for a seed phrase. Any page that does is a scam.
#
# Usage:  ./quantus_airdrop_checker.sh [--download-only] [--offline] [--docker] [--self-test]
set -euo pipefail
umask 077

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
die() { printf '%s\n' "$@" >&2; exit 1; }

[[ $(id -u) != 0 ]] || die "Refusing to run as root. Run this as your normal user."
command -v python3 >/dev/null 2>&1 || die "python3 is required (3.9 or newer)." \
    "Debian/Ubuntu: sudo apt install python3"
python3 - <<'PY' || die "python3 3.9 or newer is required; yours is older."
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
[[ -f $here/quantus_airdrop_checker.py ]] || die "quantus_airdrop_checker.py is missing next to this script."

# no core dumps: a crash must never write memory (and a seed phrase) to disk
ulimit -c 0 2>/dev/null || true

# --docker: do the whole thing inside a container that HAS NO NETWORK, for a machine you share with
# someone else. The cache has to be there already, so run --download-only once beforehand.
docker_mode=0
args=()
for arg in "$@"; do
    if [[ $arg == --docker ]]; then docker_mode=1; else args+=("$arg"); fi
done
if ((docker_mode)); then
    command -v docker >/dev/null 2>&1 || die "--docker needs docker, which is not installed."
    docker info >/dev/null 2>&1 || die "--docker needs docker, and this user cannot talk to it."
    cache=${QUANTUS_AIRDROP_CHECKER_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/quantus-airdrop-checker}
    [[ -d $cache/nodes && -f $cache/lists/lists.json ]] || die \
        "The cache is empty, and a container with no network cannot fill it." \
        "Run this first:  ./quantus_airdrop_checker.sh --download-only"
    docker image inspect python:3.12-slim >/dev/null 2>&1 || docker pull -q python:3.12-slim >/dev/null
    cat >&2 <<'NOTE'
Running inside a container:
  - it has no network at all, its filesystem is read-only, and its memory cannot be swapped out;
  - it also solves an old distribution: the official builds need glibc 2.39, the image has a newer one;
  - the image has no gpg, so the report will be shown on screen only, with no file written. Run without
    --docker if you want the encrypted report file;
  - it does NOT hide anything from this machine: while a build runs, your phrase is in this machine's
    process list, container or not. Only run this where nobody else is logged in.
NOTE
    tty_flags=(-i)
    [[ -t 0 && -t 1 ]] && tty_flags=(-i -t)
    exec docker run --rm "${tty_flags[@]}" \
        --network none --log-driver none --ulimit core=0 \
        --memory 4g --memory-swap 4g --pids-limit 256 \
        --read-only --cap-drop ALL --user "$(id -u):$(id -g)" \
        --tmpfs "/home/checker:rw,nosuid,nodev,size=64m,mode=0700,uid=$(id -u),gid=$(id -g)" \
        --tmpfs "/tmp:rw,nosuid,nodev,exec,size=256m" \
        -e HOME=/home/checker -e QUANTUS_AIRDROP_CHECKER_CACHE=/opt/cache -e NO_NETWORK=1 \
        --mount "type=bind,src=$here,dst=/opt/quantus-airdrop-checker,readonly" \
        --mount "type=bind,src=$cache,dst=/opt/cache,readonly" \
        --mount "type=bind,src=$PWD,dst=/opt/out" \
        --workdir /opt/out \
        python:3.12-slim python3 /opt/quantus-airdrop-checker/quantus_airdrop_checker.py --offline "${args[@]}"
fi

exec python3 "$here/quantus_airdrop_checker.py" "$@"
