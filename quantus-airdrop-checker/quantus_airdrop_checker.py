#!/usr/bin/env python3
"""Quantus airdrop checker - a community tool. NOT affiliated with the Quantus team.

What it does
    You paste the BIP39 seed phrases you used for mining on the Quantus testnets.
    For every phrase the tool derives every historical address format Quantus ever used,
    looks each address up in the OFFICIAL public snapshots published by the team, and
    prints what you control and roughly how much QTC it should be worth.

What it never does
    Store your seed phrases (not even encrypted), send them anywhere, or claim anything.
    All matching happens on your machine against public lists.

Read the code before you run it. Anything that asks for a seed phrase deserves that.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import resource
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"
USER_AGENT = "quantus-airdrop-checker/1.0"  # Cloudflare rejects Python's default User-Agent with HTTP 403
TARGET = "x86_64-unknown-linux-gnu"

# ------------------------------------------------------------------ what we know about the chain

# The same seed phrase produced a DIFFERENT address not only on every testnet but between releases
# of the same testnet. Mapped 2026-09-16 by running one throwaway phrase through 33 chain releases:
# there are eight distinct address generations. One official binary per generation covers them all.
# (name, release used, sha256 of the release tarball, covered releases, key schemes)
GENERATIONS = [
    ("Resonance",         "v0.1.0",
     "a59838cd5ef931165a33fa86d2b352e14f8e5324340cb05bf51a2193f94c34f9", "v0.1.0-v0.1.6",   ("standard",)),
    ("Schrodinger early", "v0.2.5",
     "52a1db67b6d515a2329ee6853505d988b08ba0cc9a9cfcbe800dc9fa713ff6af", "v0.2.3-v0.2.7",   ("standard",)),
    ("Schrodinger",       "v0.2.10-matcha-shot",
     "5434b319f2eaaee2c183e8d1a63eb0a9a6a820952f25cf58a3553a6eefcd4ac3", "v0.2.9-v0.2.10",  ("standard",)),
    ("pre-Dirac",         "v0.3.1",
     "850799ec37543dd8c8fb39421458a72edc70019f6fa5c93465fd079163537bdc", "v0.3.0-v0.3.1",   ("standard",)),
    ("Dirac early",       "v0.4.2",
     "720dfc616cd82ce31fb486bdd2d4af028368439fe32e0cae23dc33ed4be4beaf", "v0.4.0-v0.4.4",   ("standard",)),
    ("Dirac middle",      "v0.4.6",
     "14e3949523dde84e59ad984f7c807ac8c791cc23b49ecf8ee21e499dc57f6544", "v0.4.5-v0.4.7",   ("standard",)),
    ("Dirac late",        "v0.4.8-buah-naga",
     "8c9c25540775c6444497f48fe5e7f8136edfa24a37c91e2fb593523ba3e9da18", "v0.4.8-v0.4.9",   ("standard",)),
    ("Planck / mainnet",  "v1.0.1",
     "5880937c2a933b97d9916c5c9e78425d9918c6d7b68abe69de43680f62c13c93", "v0.4.11-v1.0.1",  ("standard", "wormhole")),
]

# The official claim code (quantus-cli PR #163, released as v2.3.0 on 2026-09-22; the mobile app runs
# the same Rust code) does not find the default HD addresses of these two generations - only their
# --no-derivation variant. Unclaimed rows derived from them get a warning in the report.
CLAIM_GAP = {"v0.4.6", "v0.4.8-buah-naga"}

RELEASES = "https://github.com/Quantus-Network/chain/releases/download"
SNAPSHOT_API = "https://api.github.com/repos/Quantus-Network/task-master/contents/testnet_data_snapshots"

# Official miner lists, in chronological order: how many blocks each address mined on each testnet.
TESTNETS = [
    ("Resonance",   "resonance_network_miners.json"),
    ("Schrodinger", "schrodinger_miners.json"),
    ("Dirac",       "dirac_miners.json"),
    ("Planck",      "planck_miners.json"),
]

# The team's claim server (live since 2026-09-21). GET /snapshot is the airdrop list with the amount
# of every address; GET /unpaid lists the rows not paid yet (unclaimed, or claimed and waiting for a
# payout). Both are public and are downloaded whole: which addresses are yours is worked out here, on
# this machine, and never sent. The amounts on the server are the real ones - they do not follow the
# formula announced on 2026-09-09 (nearly every miner is on the list, with at least 0.10 QTC).
CLAIM_SERVER = "https://airdrop-claim.quantus.com"
CLAIM_DEADLINE = "1 December 2026"
MAX_DOWNLOAD = 64 * 1024 * 1024

UNCLAIMED = "NOT CLAIMED YET"
WAITING = "claimed, waiting for payout"
PAID = "paid out"
NOT_IN_AIRDROP = "not in the airdrop"
UNKNOWN = "unknown"
STATUS_ORDER = {UNCLAIMED: 0, WAITING: 1, UNKNOWN: 2, PAID: 3, NOT_IN_AIRDROP: 4}

SS58_PREFIX = bytes([0x6F, 0x40])  # network 189 ("qz...") in two-byte SS58 encoding
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ADDRESS_RE = re.compile(r"qz[1-9A-HJ-NP-Za-km-z]{44,50}")


def die(*msg):
    print("\n".join(str(m) for m in msg), file=sys.stderr)
    raise SystemExit(1)


# ------------------------------------------------------------------ how it looks

# Colour and the wider layout are switched off automatically when the output is piped, when the
# terminal says it is "dumb", or when NO_COLOR is set. Nothing about the result depends on them.
COLOR = False
WIDE = False
RULE = "-"
DOT = "-"


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if COLOR else str(text)


def bold(text):
    return paint(text, "1")


def dim(text):
    return paint(text, "2")


def red(text):
    return paint(text, "1;31")


def green(text):
    return paint(text, "32")


def yellow(text):
    return paint(text, "33")


def blue(text):
    return paint(text, "36")


def look_at_the_terminal():
    """Decide once whether to use colour and how wide the result table may be."""
    global COLOR, WIDE, RULE, DOT
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # a terminal that cannot encode an emoji must not crash
        except (AttributeError, ValueError, OSError):
            pass
    COLOR = (not os.environ.get("NO_COLOR") and sys.stdout.isatty()
             and os.environ.get("TERM", "dumb") not in ("", "dumb"))
    WIDE = shutil.get_terminal_size((80, 24)).columns >= 118
    try:
        "\u2500\u00b7".encode(sys.stdout.encoding or "ascii")
        RULE, DOT = "\u2500", "\u00b7"
    except (UnicodeEncodeError, LookupError):
        RULE, DOT = "-", "-"


def rule(width=88):
    return dim(RULE * width)


def count(n, one, many=None):
    """1 address / 2 addresses - small thing, but "1 address(es)" reads like a machine wrote it."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ------------------------------------------------------------------ safety rails

def harden(need_tty):
    """Same rules for every run: never as root, no core dumps, private files, a real terminal."""
    if os.geteuid() == 0:
        die("Refusing to run as root. Run this as your normal user.")
    os.umask(0o077)
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # a crash must not write a seed to disk
    except (ValueError, OSError):
        pass
    if need_tty and not (sys.stdin.isatty() and sys.stdout.isatty()):
        die("Run this in your own terminal.",
            "Seed phrases are read with the echo turned off, which needs a terminal;",
            "a pipe or a script would also put them into shell history or a log file.")
    if os.uname().machine != "x86_64":
        die(f"This machine is {os.uname().machine}; the official Quantus builds used here are {TARGET}.",
            "Linux x86_64 only for now (Windows: run it inside WSL).")


# ------------------------------------------------------------------ cache

def cache_root():
    env = os.environ.get("QUANTUS_AIRDROP_CHECKER_CACHE")
    if env:
        root = pathlib.Path(env).expanduser()
    else:
        base = os.environ.get("XDG_CACHE_HOME") or (pathlib.Path.home() / ".cache")
        root = pathlib.Path(base) / "quantus-airdrop-checker"
    try:
        root.mkdir(parents=True, exist_ok=True)
        if stat.S_IMODE(root.stat().st_mode) != 0o700:
            root.chmod(0o700)
    except OSError:
        pass          # a read-only cache is fine (--docker mounts it that way); only reading matters
    if not root.is_dir():
        die(f"The cache directory {root} does not exist and cannot be created.")
    return root


def write_private(path, data):
    """Write bytes so that no half-written file is ever left behind and nobody else can read it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ network

# The tool touches the network in exactly one step: fetching the official data below.
# Hosts: api.github.com, objects.githubusercontent.com, raw.githubusercontent.com (binaries and miner
# lists) and airdrop-claim.quantus.com (the airdrop list and claim statuses). Nothing is uploaded:
# every request is a plain download of a public file. No telemetry.
NETWORK = True


def http(url, headers=None, timeout=300):
    if not NETWORK:
        die(f"--offline, but this run still needs the network for:\n  {url}",
            "Prepare the cache on a machine that has network access:  ./quantus_airdrop_checker.sh --download-only",
            "then copy ~/.cache/quantus-airdrop-checker/ over and run with --offline.")
    headers = dict({"User-Agent": USER_AGENT}, **(headers or {}))
    # optional: a GitHub token lifts the 60-requests-per-hour limit of GitHub's API (CI, shared IPs).
    # It is sent to api.github.com only - never to any other host.
    if url.startswith("https://api.github.com/") and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        extra = ""
        if e.code == 403 and "api.github.com" in url:
            extra = ("\n(GitHub allows 60 unauthenticated API calls per hour; wait an hour, set GITHUB_TOKEN,"
                     " or use --offline with a cache.)")
        die(f"Download failed ({e.code} {e.reason}):\n  {url}{extra}")
    except urllib.error.URLError as e:
        die(f"Download failed ({e.reason}):\n  {url}")
    except OSError as e:
        die(f"Download failed ({type(e).__name__}):\n  {url}")


def git_blob_sha(data):
    """Git's own hash of a file's contents, as GitHub's API reports it for that file.

    This proves the bytes we downloaded are the bytes GitHub has - nothing was corrupted or swapped on
    the way. It does NOT prove anything about what the Quantus team put there: if they change a list,
    the new list verifies fine. That is why the report prints the hash and the date it was fetched.
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ------------------------------------------------------------------ official node binaries

def archive_name(version):
    return f"quantus-node-{version}-{TARGET}.tar.gz"


def ensure_archive(version, sha256, note=print):
    """One official release tarball, downloaded once and verified against a pinned SHA256 every run."""
    path = cache_root() / "nodes" / archive_name(version)
    if path.exists():
        if sha256_file(path) == sha256:
            return path, False
        note(f"    cached copy of {path.name} has the wrong checksum - downloading it again")
        path.unlink()
    data = http(f"{RELEASES}/{version}/{archive_name(version)}")
    if hashlib.sha256(data).hexdigest() != sha256:
        die(f"STOP: {archive_name(version)} does not match its pinned SHA256. Not running it.",
            "Either the download was corrupted or something replaced it. Nothing was executed.")
    write_private(path, data)
    return path, True


def ensure_binaries(note=print):
    total = 0
    for i, (name, version, sha256, covers, _schemes) in enumerate(GENERATIONS, 1):
        path, fetched = ensure_archive(version, sha256, note)
        size = path.stat().st_size
        total += size
        note(dim(f"      [{i}/{len(GENERATIONS)}] {name:<17} {version:<20} {size / 1e6:5.1f} MB  "
                 f"{'downloaded' if fetched else 'cached'}, sha256 ok"))
    return total


# ------------------------------------------------------------------ official snapshot lists

LIST_META = "lists.json"


def fetch_lists(note=print):
    """Download the 4 official miner lists and verify each one against GitHub's own blob hash."""
    listing = json.loads(http(SNAPSHOT_API, {"Accept": "application/vnd.github+json"}))
    by_name = {f["name"]: f for f in listing if isinstance(f, dict)}
    meta = {"fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "files": {}}
    fetched = {}
    for testnet, filename in TESTNETS:
        entry = by_name.get(filename)
        if entry is None:
            die(f"The official snapshot directory no longer contains {filename}.",
                "The team may have renamed it; this tool needs an update.")
        data = http(entry["download_url"])
        got = git_blob_sha(data)
        if got != entry["sha"]:
            die(f"STOP: {filename} does not match the hash GitHub's API reports ({got} != {entry['sha']}).",
                "Not using this data.")
        fetched[filename] = data
        meta["files"][filename] = {"blob_sha": got, "bytes": len(data), "url": entry["download_url"]}
        note(dim(f"      {testnet:<12} {filename:<32} {len(data) / 1000:5.1f} kB  "
                 f"blob {got[:12]}... ok"))
    # write only once every file is in hand: an interrupted download must not leave new lists beside an
    # old index, which the next --offline run would report as tampering
    for filename, data in fetched.items():
        write_private(cache_root() / "lists" / filename, data)
    write_private(cache_root() / "lists" / LIST_META, json.dumps(meta, indent=1).encode())
    return meta


def load_lists(note=print):
    """Read the cached lists, re-checking every file against the hash stored when it was fetched."""
    meta_path = cache_root() / "lists" / LIST_META
    if not meta_path.exists():
        die("No cached official lists.",
            "Run once with network access:  ./quantus_airdrop_checker.sh --download-only")
    try:
        meta = json.loads(meta_path.read_text())
        meta["files"].get
        meta["fetched_utc"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        die("The cached miner lists are unreadable.",
            "Fetch them again:  ./quantus_airdrop_checker.sh --download-only")
    for _testnet, filename in TESTNETS:
        info = meta["files"].get(filename)
        path = cache_root() / "lists" / filename
        if info is None or not path.exists():
            die(f"The cache is missing {filename}. Run ./quantus_airdrop_checker.sh --download-only again.")
        if git_blob_sha(path.read_bytes()) != info["blob_sha"]:
            die(f"STOP: the cached {filename} changed since it was downloaded. Not using it.",
                "Fetch it again on a machine with network access:  ./quantus_airdrop_checker.sh --download-only",
                f"(or delete {cache_root() / 'lists'} and fetch it again).")
    note(dim(f"      cached lists from {meta['fetched_utc']}, all 4 hashes ok"))
    return meta


AIRDROP_DIR = "airdrop"
AIRDROP_META = "airdrop.json"
AIRDROP_FILES = ("snapshot.json", "unpaid.json")
SOFT_GET_DEADLINE = 180          # seconds for one whole download from the claim server
MAX_AMOUNT = 10 ** 12            # in hundredths of QTC; anything outside 0..this is not a real amount
STALE_AFTER = dt.timedelta(hours=24)
HEX64 = re.compile(r"[0-9a-f]{64}")


def soft_get(url):
    """Download, but never die: the claim server may be down, slow, or gone after the claim closes."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    deadline = time.monotonic() + SOFT_GET_DEADLINE
    chunks, size = [], 0
    with urllib.request.urlopen(req, timeout=60) as r:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise ValueError("answer too large")
            if time.monotonic() > deadline:
                raise TimeoutError("download too slow")
            chunks.append(chunk)
    return b"".join(chunks)


def _no_constants(name):
    raise ValueError(f"not a number: {name}")


def _account_key(value):
    """'0x' + 64 hex characters -> the 64 characters in lower case; anything else -> None."""
    if not isinstance(value, str):
        return None
    v = value.lower()
    v = v[2:] if v.startswith("0x") else v
    return v if HEX64.fullmatch(v) else None


def parse_airdrop(snapshot_raw, unpaid_raw):
    """The two server answers -> checked data. Raises ValueError when they cannot be used at all.

    Every value from the server is checked before it is used: amounts must be plain integers in range,
    testnet names must be the four known ones, a claiming address must be a valid Quantus address.
    'status_ok' is False when the two answers do not fit together (a row without an address, an
    unpaid row that is not on the airdrop list, an empty unpaid list). Then NO claim status is shown,
    because "missing from /unpaid" would otherwise read as "paid out" - the costliest wrong answer.
    """
    snapshot = json.loads(snapshot_raw, parse_constant=_no_constants)
    unpaid = json.loads(unpaid_raw, parse_constant=_no_constants)
    raw_rows = snapshot.get("rows") if isinstance(snapshot, dict) else None
    raw_open = unpaid.get("rows") if isinstance(unpaid, dict) else None
    if not isinstance(raw_rows, list) or not isinstance(raw_open, list):
        raise ValueError("unexpected answer from the claim server")
    known = {name for name, _filename in TESTNETS}
    rows, addresses, status_ok = [], set(), True
    for r in raw_rows:
        key = _account_key(r.get("account")) if isinstance(r, dict) else None
        if key is None:
            continue
        address = r.get("address") if isinstance(r.get("address"), str) else None
        if address is None:
            status_ok = False
        else:
            addresses.add(address)
        amount = r.get("amount_hundredths")
        nets = r.get("testnets") if isinstance(r.get("testnets"), list) else []
        rows.append({"account": key, "address": address,
                     "qtc": amount / 100 if type(amount) is int and 0 <= amount < MAX_AMOUNT else None,
                     "testnets": [n for n in nets if isinstance(n, str) and n in known]})
    if not rows:
        raise ValueError("the airdrop list from the claim server is empty")
    open_rows = {}
    for r in raw_open:
        if not isinstance(r, dict) or not isinstance(r.get("address"), str):
            status_ok = False
            continue
        if r["address"] not in addresses:
            status_ok = False
        claim = r.get("claim_account")
        open_rows[r["address"]] = {
            "status": r.get("status") if isinstance(r.get("status"), str) else None,
            "claim_account": claim if isinstance(claim, str) and valid_address(claim) else None}
    if not open_rows:
        status_ok = False
    version = snapshot.get("version")
    return {"rows": rows, "open": open_rows, "status_ok": status_ok,
            "version": version if type(version) is int else None}


def save_airdrop(raw, meta):
    """Write the files into a fresh folder, then swap it in: an interrupted download never leaves
    new data next to an old index."""
    root = cache_root()
    new, old, current = root / (AIRDROP_DIR + ".new"), root / (AIRDROP_DIR + ".old"), root / AIRDROP_DIR
    shutil.rmtree(new, ignore_errors=True)
    for name, data in raw.items():
        write_private(new / name, data)
    write_private(new / AIRDROP_META, json.dumps(meta, indent=1).encode())
    shutil.rmtree(old, ignore_errors=True)
    if current.exists():
        os.replace(current, old)
    os.replace(new, current)
    shutil.rmtree(old, ignore_errors=True)


def fetch_airdrop(note=print):
    """Download the airdrop list and the claim statuses. Returns the checked data, or None."""
    try:
        raw = {"snapshot.json": soft_get(CLAIM_SERVER + "/snapshot"),
               "unpaid.json": soft_get(CLAIM_SERVER + "/unpaid")}
        data = parse_airdrop(raw["snapshot.json"], raw["unpaid.json"])
    except Exception as e:  # any failure here must not stop the check - the miner lists still work
        note(yellow(f"      \u26A0  the claim server could not be used ({type(e).__name__}); "
                    "amounts and claim status will be unknown"))
        return None
    meta = {"fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "server": CLAIM_SERVER,
            "files": {name: hashlib.sha256(data_).hexdigest() for name, data_ in raw.items()}}
    try:
        save_airdrop(raw, meta)
    except OSError:
        note(yellow("      \u26A0  the airdrop list could not be saved to the cache; it is used for this run only"))
    note(dim(f"      airdrop list  {len(data['rows'])} addresses, {len(data['open'])} not paid yet  "
             f"({CLAIM_SERVER.split('//')[1]})"))
    if not data["status_ok"]:
        note(yellow("      \u26A0  the server's two lists do not fit together; claim status will be unknown"))
    return dict(data, meta=meta)


def load_airdrop(note=print):
    """The cached airdrop list, re-checked against the hashes taken when it was downloaded.
    (The hashes catch a damaged or half-written cache; they live next to the data, so they are not
    protection against someone who can write to your cache.)"""
    folder = cache_root() / AIRDROP_DIR
    try:
        meta = json.loads((folder / AIRDROP_META).read_text())
        files, fetched = meta["files"], meta["fetched_utc"]
        if not isinstance(files, dict) or set(files) != set(AIRDROP_FILES) or not isinstance(fetched, str):
            raise ValueError("unexpected index")
    except FileNotFoundError:
        note(yellow("      \u26A0  no airdrop list in the cache; amounts and claim status will be unknown"))
        note(dim("         (run ./quantus_airdrop_checker.sh --download-only on a machine with network access)"))
        return None
    except (OSError, ValueError, KeyError, TypeError):
        note(yellow("      \u26A0  the cached airdrop list is unreadable; amounts and claim status will be unknown"))
        note(dim("         (run ./quantus_airdrop_checker.sh --download-only again)"))
        return None
    raw = {}
    for name in AIRDROP_FILES:
        path = folder / name
        data = path.read_bytes() if path.exists() else b""
        if hashlib.sha256(data).hexdigest() != files[name]:
            die(f"STOP: the cached {name} changed since it was downloaded. Not using it.",
                "Fetch it again on a machine with network access:  ./quantus_airdrop_checker.sh --download-only")
        raw[name] = data
    try:
        data = parse_airdrop(raw["snapshot.json"], raw["unpaid.json"])
    except Exception:
        note(yellow("      \u26A0  the cached airdrop list is unreadable; amounts and claim status will be unknown"))
        return None
    note(dim(f"      cached airdrop list from {fetched} - claim status may have changed since"))
    try:
        if dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(fetched) > STALE_AFTER:
            note(yellow("      \u26A0  that is more than a day old; run --download-only for today's claim status"))
    except (ValueError, TypeError):
        pass
    if not data["status_ok"]:
        note(yellow("      \u26A0  the server's two lists do not fit together; claim status will be unknown"))
    return dict(data, meta=meta)


def official_data(note=print):
    """The official inputs: 8 pinned binaries, 4 verified miner lists and the airdrop list.
    The only step that uses the network. Returns (miner-list meta, airdrop meta or None)."""
    note(dim("      eight official quantus-node builds, downloaded once and SHA256-pinned in this file:"))
    total = ensure_binaries(note)
    note(dim(f"      {total / 1e6:.0f} MB in {cache_root() / 'nodes'}"))
    note("")
    note(dim("      the four official testnet miner lists (Quantus-Network/task-master):"))
    meta = fetch_lists(note) if NETWORK else load_lists(note)
    note("")
    note(dim("      the airdrop list and claim status (the team's claim server, downloaded whole):"))
    airdrop = fetch_airdrop(note) if NETWORK else load_airdrop(note)
    return meta, airdrop


# ------------------------------------------------------------------ the airdrop formula

def account_id(ss58):
    """Address qz... -> 32-byte account id. Checks the network prefix and the SS58 checksum."""
    n = 0
    for c in ss58:
        if c not in B58:
            raise ValueError(f"not base58: {c!r}")
        n = n * 58 + B58.index(c)
    try:
        raw = n.to_bytes(36, "big")  # 2 prefix + 32 account + 2 checksum
    except OverflowError:
        raise ValueError("too long for an SS58 address") from None
    if raw[:2] != SS58_PREFIX:
        raise ValueError("not a network 189 address (qz...)")
    if hashlib.blake2b(b"SS58PRE" + raw[:34], digest_size=64).digest()[:2] != raw[34:]:
        raise ValueError("bad checksum (typo?)")
    return raw[2:34]


def valid_address(ss58):
    try:
        account_id(ss58)
        return True
    except ValueError:
        return False


class Testnet:
    """One official miner list: how many blocks each address mined there, and its rank."""

    def __init__(self, name, filename, raw, blob_sha):
        self.name, self.filename, self.blob_sha = name, filename, blob_sha
        blocks = {}
        for row in json.loads(raw)["data"]["minerStats"]:
            blocks[row["id"]] = int(row["totalMinedBlocks"])
        order = sorted(blocks.items(), key=lambda kv: (-kv[1], kv[0]))
        self.blocks = blocks
        self.miners = len(order)
        self._rank, first = {}, {}                            # ties share the better rank
        for i, (a, b) in enumerate(order):
            self._rank[a] = first.setdefault(b, i + 1)

    def lookup(self, address):
        """(blocks, rank) or None if this address never mined here."""
        if address not in self.blocks:
            return None
        return self.blocks[address], self._rank[address]


class OfficialData:
    """Everything an address is looked up in: the miner lists (blocks, rank) and the claim server's
    airdrop list (amount, claim status). Amounts come ONLY from the claim server; when it could not be
    reached they are unknown - never guessed."""

    def __init__(self, meta, airdrop=None):
        self.meta = meta
        self.airdrop = airdrop
        self.testnets = []
        for name, filename in TESTNETS:
            raw = (cache_root() / "lists" / filename).read_bytes()
            self.testnets.append(Testnet(name, filename, raw, meta["files"][filename]["blob_sha"]))
        self.by_account = {r["account"]: r for r in airdrop["rows"]} if airdrop else {}

    @property
    def has_airdrop(self):
        return self.airdrop is not None

    @property
    def airdrop_meta(self):
        return self.airdrop["meta"] if self.airdrop else None

    def _status(self, row):
        """Claim status of one airdrop row, from the list of rows not paid yet."""
        if not self.airdrop["status_ok"] or not row["address"]:
            return UNKNOWN, None
        state = self.airdrop["open"].get(row["address"])
        if state is None:
            return PAID, None               # the server drops a row from /unpaid once it is paid
        if state["status"] == "unclaimed":
            return UNCLAIMED, None
        if state["status"] == "recorded":
            return WAITING, state["claim_account"]
        return UNKNOWN, None

    def lookup(self, address):
        """What is known about one address, or None if it is on no list at all."""
        mined = []
        for t in self.testnets:
            hit = t.lookup(address)
            if hit:
                mined.append((t.name, hit[0], hit[1], t.miners))
        try:
            row = self.by_account.get(account_id(address).hex())
        except ValueError:
            row = None
        if not mined and row is None:
            return None
        mined_names = [m[0] for m in mined]
        if not self.has_airdrop:
            return {"mined": mined, "testnets": mined_names, "qtc": None, "status": UNKNOWN,
                    "claim_account": None}
        if row is None:
            return {"mined": mined, "testnets": mined_names, "qtc": 0.0, "status": NOT_IN_AIRDROP,
                    "claim_account": None}
        status, claim_account = self._status(row)
        return {"mined": mined, "testnets": row["testnets"] or mined_names, "qtc": row["qtc"],
                "status": status, "claim_account": claim_account}

    def summary_lines(self):
        yield dim(f"{'testnet':<12} {'miners':>7}")
        for t in self.testnets:
            yield f"{t.name:<12} {t.miners:>7}"
        if not self.has_airdrop:
            yield yellow("airdrop list: not available - amounts and claim status will be unknown")
            return
        rows = self.airdrop["rows"]
        total = sum(r["qtc"] for r in rows if r["qtc"] is not None)
        yield bold(f"airdrop list: {len(rows)} addresses, {total:,.2f} QTC")
        if not self.airdrop["status_ok"]:
            yield yellow("claim status: unknown (the server's two lists do not fit together)")
            return
        states = [r["status"] for r in self.airdrop["open"].values()]
        yield dim(f"not claimed yet: {states.count('unclaimed')}   claimed, waiting for payout: "
                  f"{states.count('recorded')}   (as of {self.airdrop_meta['fetched_utc']})")


# ------------------------------------------------------------------ reading seed phrases

# Seed phrases are found in whatever you paste, using the official BIP39 wordlist and each phrase's
# own checksum. So numbering, labels, blank lines, one word per line and a phrase written as a grid
# all work, and there is no format to remember. Nothing pasted here is ever echoed, written to disk,
# or sent anywhere.
#
# One rule decides every hard case below: losing a phrase someone really owns is far worse than
# checking one they do not. A phrase that is not recognised may be thrown away as worthless; a phrase
# checked for nothing costs half a second and prints "no match".

WORDLIST_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
SEED_LENGTHS = (24, 21, 18, 15, 12)  # valid BIP39 phrase lengths, longest first
MAX_ALTERNATIVES = 8                 # floor for "it could also be read this way" phrases per run
SUSPICIOUS_LENGTHS = (15, 18, 21)    # almost nobody uses these lengths; seeing one suggests a bad split
MAX_SCANNED = 32                     # more "phrases" than this in one unstructured run: that is not a backup
MAX_PASTE_BYTES = 1 << 20            # 1 MB; a person's seed backup is a few kB
MAX_LABEL = 24
LABEL_UNSAFE = re.compile(r"[^\w .,:@/+-]", re.UNICODE)
LOOSE_ADDRESS_RE = re.compile(r"qz[1-9A-HJ-NP-Za-km-z]{30,}")
_INDEX = {}


def wordlist():
    """The BIP39 English wordlist, checked against a pinned SHA256 before anything trusts it."""
    if not _INDEX:
        path = pathlib.Path(__file__).resolve().parent / "bip39-english.txt"
        try:
            raw = path.read_bytes()
        except OSError:
            die("bip39-english.txt is missing next to quantus_airdrop_checker.py.")
        if hashlib.sha256(raw).hexdigest() != WORDLIST_SHA256:
            die("STOP: bip39-english.txt does not match its pinned checksum. Not using it.")
        words = raw.decode().split()
        if len(words) != 2048:
            die("STOP: bip39-english.txt does not hold 2048 words.")
        _INDEX.update({w: i for i, w in enumerate(words)})
    return _INDEX


def bip39_valid(words):
    """True when these words are a complete BIP39 phrase, checksum included."""
    index = wordlist()
    n = len(words)
    if n not in SEED_LENGTHS or any(w not in index for w in words):
        return False
    bits = "".join(format(index[w], "011b") for w in words)
    check_bits = n * 11 // 33
    ent_bits = n * 11 - check_bits
    entropy = int(bits[:ent_bits], 2).to_bytes(ent_bits // 8, "big")
    return format(hashlib.sha256(entropy).digest()[0], "08b")[:check_bits] == bits[ent_bits:]


def solvable_positions(tokens):
    """positions[i] = True when tokens[i:] can be split into whole valid phrases with nothing left over.

    Filled from the end backwards, on purpose: the obvious recursive version dies with a RecursionError
    on a long paste, and this runs in one pass.
    """
    positions = [False] * (len(tokens) + 1)
    positions[len(tokens)] = True
    for i in range(len(tokens) - 1, -1, -1):
        for n in SEED_LENGTHS:
            if i + n <= len(tokens) and positions[i + n] and bip39_valid(tokens[i:i + n]):
                positions[i] = True
                break
    return positions


def add_windows(tokens, chosen, into):
    """Add every stretch of these words that is a valid phrase on its own and is not already known."""
    for i in range(len(tokens)):
        for n in SEED_LENGTHS:
            if i + n <= len(tokens) and tuple(tokens[i:i + n]) not in chosen and bip39_valid(tokens[i:i + n]):
                into.append(tokens[i:i + n])
                chosen.add(tuple(tokens[i:i + n]))


def split_run(tokens):
    """Split a run of words into phrases -> (phrases, alternatives, unused words, candidates dropped).

    `phrases` is the reading we believe; `alternatives` are other readings of the SAME words that are
    also valid, and they get checked too. Ambiguity is not hypothetical: a 12-word window passes its
    checksum by accident once in 16 tries, so a stray wordlist word next to a phrase (a label like
    "laptop") or two 12-word phrases that happen to form a valid 24-word one can both mislead us.
    """
    positions = solvable_positions(tokens)
    if positions[0]:
        primary, alternatives, i = [], [], 0
        while i < len(tokens):                       # the reading that prefers the longest phrase
            for n in SEED_LENGTHS:
                if i + n <= len(tokens) and positions[i + n] and bip39_valid(tokens[i:i + n]):
                    primary.append(tokens[i:i + n])
                    i += n
                    break
        # a phrase belongs to some OTHER exact split only if the words before it also split (reachable)
        # and the words after it split too (positions) - otherwise it is a window that leads nowhere
        reachable = [False] * (len(tokens) + 1)
        reachable[0] = True
        for i in range(len(tokens)):
            if reachable[i]:
                for n in SEED_LENGTHS:
                    if i + n <= len(tokens) and bip39_valid(tokens[i:i + n]):
                        reachable[i + n] = True
        chosen = {tuple(w) for w in primary}
        for i in range(len(tokens)):
            if not reachable[i]:
                continue
            for n in SEED_LENGTHS:
                if i + n <= len(tokens) and positions[i + n] and bip39_valid(tokens[i:i + n]) \
                        and tuple(tokens[i:i + n]) not in chosen:
                    alternatives.append(tokens[i:i + n])
                    chosen.add(tuple(tokens[i:i + n]))
        # a 15, 18 or 21-word phrase in the result is a warning sign: hardly anyone uses those lengths,
        # so it is more likely we swallowed a stray word next to a 12 or 24-word phrase. Check both.
        if any(len(w) in SUSPICIOUS_LENGTHS for w in primary):
            add_windows(tokens, chosen, alternatives)
        return primary, alternatives[:max(MAX_ALTERNATIVES, len(primary))], 0, 0

    for drop in range(1, 4):                         # ignore up to 3 stray words at the edges
        for left in range(drop + 1):
            inner = tokens[left:len(tokens) - (drop - left)]
            if solvable_positions(inner)[0]:
                primary, alternatives, _unused, _dropped = split_run(inner)
                chosen = {tuple(w) for w in primary} | {tuple(w) for w in alternatives}
                add_windows(tokens, chosen, alternatives)  # trimming means we guessed: keep every reading
                return primary, alternatives[:max(MAX_ALTERNATIVES, len(primary))], drop, 0

    phrases, unused, i = [], 0, 0                    # nothing splits cleanly: read left to right
    while i < len(tokens):
        taken = 0
        for n in SEED_LENGTHS:
            if i + n <= len(tokens) and bip39_valid(tokens[i:i + n]):
                taken = n
                break
        if taken:
            phrases.append(tokens[i:i + taken])
            i += taken
        else:
            unused += 1
            i += 1
    if phrases:
        # Two different things, kept apart. How CONFIDENT this reading is depends on how much of the
        # run it explains: a few notes between phrases is believable, a wall of wordlist words is not.
        # But confidence never decides whether a phrase is CHECKED - everything phrase-shaped in the
        # run is checked either way, because throwing one away is the mistake with no way back.
        alternatives = []
        add_windows(tokens, {tuple(w) for w in phrases}, alternatives)
        if unused <= max(6, len(tokens) // 8):
            return phrases, alternatives[:max(MAX_ALTERNATIVES, len(phrases))], unused, 0
        candidates = phrases + alternatives
        return [], candidates[:MAX_SCANNED], unused, max(0, len(candidates) - MAX_SCANNED)
    return [], [], len(tokens), 0     # nothing phrase-shaped in there at all


def wordlist_words(text):
    return [t for t in re.findall(r"[a-z]+", text.lower()) if t in wordlist()]


def runs_of_words(text):
    """Pasted text -> runs, each run being the lines that belong together, as lists of wordlist words.

    A run ends at a blank line or at a line holding no wordlist words at all - that is how one phrase
    is separated from the next. Inside a run the line structure is KEPT, because it is evidence: a
    phrase can be written as a grid across several lines, and it can also sit on one line next to its
    own label. Those two need opposite readings, so both are tried, joined first.
    """
    runs, current = [], []
    for line in text.lower().splitlines():
        words = [t for t in re.findall(r"[a-z]+", line) if t in wordlist()]
        if not words:
            if current:
                runs.append(current)
                current = []
            continue
        current.append(words)
    if current:
        runs.append(current)
    return runs


def find_phrases(text):
    """(phrases, alternatives, unrecognised).

    unrecognised holds (words in the run, phrases salvaged from it) for runs that did not split
    cleanly - either because there was nothing there, or because too much of the run was left over
    to believe the reading. Whatever was salvaged is still checked; the user is just told about it.
    """
    phrases, alternatives, unrecognised = [], [], []
    for lines in runs_of_words(text):
        joined = [w for line in lines for w in line]
        if len(joined) < min(SEED_LENGTHS):
            if len(joined) >= 6:
                unrecognised.append((len(joined), 0, 0))
            continue
        found, alts, unused, dropped = split_run(joined)
        # Only a reading that uses every word of the run is taken at face value. Anything else means
        # we had to guess, and then the line structure deserves a look before the guess is believed.
        if not (found and unused == 0) and len(lines) > 1:
            # The joined reading did not convince. Try the other structure people use: one phrase per
            # line, each with its own label on the same line. Accepted only if EVERY line that is long
            # enough to hold a phrase yields one - a half-working reading is not evidence of anything.
            by_line, line_alts, works = [], [], True
            for line in lines:
                if len(line) < min(SEED_LENGTHS):
                    continue                       # a short line is a label, not a phrase
                got, got_alts, _u, _d = split_run(line)
                if not got:
                    works = False
                    break
                by_line += got
                line_alts += got_alts
            if works and by_line:
                phrases += by_line
                chosen = {tuple(w) for w in by_line}
                # Keep the phrases the joined reading believed in - that is a real competing reading.
                # Not its window-noise: once every line has yielded a phrase of its own, guessing across
                # the line breaks as well would add dozens of checks for nothing.
                alternatives += line_alts + [w for w in found if tuple(w) not in chosen]
                continue
        if found:
            phrases += found
            alternatives += alts
        elif alts:
            alternatives += alts
            unrecognised.append((len(joined), len(alts), dropped))
        elif len(joined) >= 6:
            unrecognised.append((len(joined), 0, 0))
    return phrases, alternatives, unrecognised


def clean_label(text):
    """(label, refused). A label reaches the screen and the report, so control characters are stripped -
    and a label holding more than a few wordlist words is refused outright: that is a stray piece of a
    seed phrase, and printing it would be the one thing this tool must never do."""
    cleaned = " ".join(LABEL_UNSAFE.sub(" ", text).split())
    # count the wordlist words in the WHOLE label, before it is shortened: shortening first would let a
    # long label smuggle a few words of a phrase into the first 24 characters
    if max(len(wordlist_words(cleaned)), len(wordlist_words(text))) > 3:
        return None, True
    return (cleaned[:MAX_LABEL] or None), False


def parse_paste(text):
    """Pasted text -> (entries, unrecognised, refused_labels).

    An entry is {"label", "words", "alternative"}. Any shape works: "|" separates a label from a
    phrase in either order, and a line starting with # is a comment - unless it carries a phrase,
    in which case the phrase wins, because a dropped phrase is the one mistake that cannot be undone.
    """
    entries, leftover, refused = [], [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or (line.startswith("#") and len(wordlist_words(line)) < min(SEED_LENGTHS)):
            leftover.append("")        # keep the separator: a blank or comment line ends a run
            continue
        if "|" not in line:
            leftover.append(raw)
            continue
        found, alternatives, label_parts = [], [], []
        for field in line.split("|"):
            words = wordlist_words(field)
            if len(words) >= min(SEED_LENGTHS):
                got, alts, _unused, _dropped = split_run(words)
                found += got
                alternatives += alts
            elif field.strip():
                label_parts.append(field.strip())
        if not found:
            leftover.append(raw)
            continue
        label, was_refused = clean_label(" ".join(label_parts))
        refused += was_refused
        entries += [{"label": label, "words": w, "alternative": False} for w in found]
        entries += [{"label": label, "words": w, "alternative": True} for w in alternatives]
        leftover.append("")
    phrases, alternatives, unrecognised = find_phrases("\n".join(leftover))
    entries += [{"label": None, "words": w, "alternative": False} for w in phrases]
    entries += [{"label": None, "words": w, "alternative": True} for w in alternatives]
    return entries, unrecognised, refused


def fingerprint(words):
    """Short fingerprint of a phrase, used only to notice duplicates. A phrase cannot be recovered from it."""
    return hashlib.sha256(b"quant-checker-fingerprint-v1\0" + " ".join(words).encode()).hexdigest()[:12]


def short(address):
    return f"{address[:8]}...{address[-5:]}"


# ------------------------------------------------------------------ the terminal

class HiddenTerminal:
    """Echo off for the whole session, and put back however we leave.

    Echo stays off even while the tool is busy, so a phrase pasted at the wrong moment still does not
    reach the screen or the scrollback; on the way out the unread input is flushed, so a phrase pasted
    late does not land in the shell that started us. Ctrl+Z, SIGTERM, SIGQUIT and SIGHUP restore it
    too - a tool that leaves someone's terminal silently echo-less is one people rightly stop trusting.

    Because the terminal is silent by default, this class does the echoing itself, and decides what may
    be shown: stars for a password, whole lines for addresses (which are public), nothing at all for
    seed phrases. A line that turns out to look like a seed phrase is never echoed, whatever step we
    are in.
    """

    def __init__(self):
        self.tty = None
        self.fd = None
        self.saved = None
        self.can_write = False
        self.previous_signals = {}

    def __enter__(self):
        try:
            self.tty = open("/dev/tty", "r+b", buffering=0)
            self.can_write = True
        except OSError:
            try:
                self.tty = open("/dev/tty", "rb", buffering=0)
            except OSError:
                die("Cannot open /dev/tty. Run this in your own terminal.")
        self.fd = self.tty.fileno()
        self.saved = termios.tcgetattr(self.fd)
        self._hide()
        for sig, handler in ((signal.SIGTERM, self._terminate), (signal.SIGHUP, self._terminate),
                             (signal.SIGQUIT, self._terminate), (signal.SIGTSTP, self._suspend),
                             (signal.SIGCONT, self._resume)):
            try:
                self.previous_signals[sig] = signal.signal(sig, handler)
            except (ValueError, OSError, AttributeError):
                pass
        return self

    def __exit__(self, *exc):
        for sig, handler in self.previous_signals.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        self._restore()
        self.tty.close()
        return False

    def _hide(self):
        mode = termios.tcgetattr(self.fd)
        mode[3] &= ~(termios.ECHO | termios.ICANON)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)

    def _restore(self):
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
        except (termios.error, OSError, ValueError):
            pass

    def flush(self):
        """Drop anything typed while we were busy: this round starts with an empty buffer."""
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except (termios.error, OSError):
            pass

    def _terminate(self, signum, frame):
        self._restore()
        os._exit(1)  # exiting the normal way could print a traceback built from pasted text

    def _suspend(self, signum, frame):
        self._restore()
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)

    def _resume(self, signum, frame):
        try:
            signal.signal(signal.SIGTSTP, self._suspend)
        except (ValueError, OSError):
            pass
        self._hide()

    def write(self, text):
        """Put something on the terminal ourselves - the terminal driver no longer does it for us."""
        if not self.can_write:
            return
        sys.stdout.flush()
        try:
            os.write(self.fd, text.encode("utf-8", "replace"))
        except OSError:
            pass

    def read_paste(self, evaluate=None, echo=None, single_line=False):
        """Read what is pasted. Returns "" when the user just presses Enter.

        evaluate(text) -> (finished, status): stop as soon as the text makes sense (every phrase
        complete, at least one address), and otherwise show a status line. Without it, a blank line
        ends the input. single_line returns on the first Enter, for a password.

        echo="stars"  one star per character, so a password can be counted but not read;
        echo="lines"  each finished line is shown, unless it looks like a seed phrase;
        echo=None     nothing is shown at all.
        """
        buf, line = bytearray(), bytearray()
        stars, counter_shown, last_status = 0, False, None
        indent = "      "

        def clear_counter():
            nonlocal counter_shown
            if counter_shown and COLOR:
                self.write("\r\033[2K")
            counter_shown = False

        def show_counter():
            nonlocal counter_shown
            if echo == "lines" and COLOR and line:
                self.write("\r\033[2K" + dim(f"{indent}...{len(line)} characters"))
                counter_shown = True

        def echo_line():
            nonlocal line
            clear_counter()
            text_line = bytes(line).decode("utf-8", "replace").replace("\r", "")
            line = bytearray()
            visible = "".join(c for c in text_line if c.isprintable()).strip()
            if not visible:
                return
            if len(wordlist_words(visible)) >= 6:
                # never echo this, whatever step we are in: it is the one thing that must not be shown
                self.write(yellow(f"{indent}(that line looks like a seed phrase - not shown)") + "\n")
            else:
                self.write(f"{indent}{blue(visible)}\n")

        try:
            while True:
                ready, _, _ = select.select([self.fd], [], [], 1.0)
                if ready:
                    chunk = os.read(self.fd, 65536)
                    if chunk:
                        if len(buf) + len(chunk) > MAX_PASTE_BYTES:
                            buf[:] = b"\0" * len(buf)
                            buf.clear()
                            line = bytearray()
                            clear_counter()
                            print(f"{indent}that is far more text than a seed backup - dropped, paste again",
                                  flush=True)
                            continue
                        for b in chunk:
                            if b in (0x7F, 0x08):            # backspace
                                if buf:
                                    buf.pop()
                                if line:
                                    line.pop()
                                if echo == "stars" and stars:
                                    self.write("\b \b")
                                    stars -= 1
                            elif b == 0x15:                  # Ctrl+U: forget this round completely
                                buf[:] = b"\0" * len(buf)
                                buf.clear()
                                line = bytearray()
                                if echo == "stars" and stars:
                                    self.write("\b \b" * stars)
                                    stars = 0
                                clear_counter()
                            elif b == 0x04:                  # Ctrl+D: take what is there
                                buf += b"\n\n"
                            else:
                                buf.append(b)
                                if b == 0x0A:
                                    if echo == "stars":
                                        self.write("\n")
                                        stars = 0
                                    elif echo == "lines":
                                        echo_line()
                                elif echo == "stars" and b >= 0x20 and b != 0x7F:
                                    self.write(blue("*"))
                                    stars += 1
                                elif echo == "lines":
                                    line.append(b)
                        show_counter()
                        if not (single_line and b"\n" in buf):
                            continue
                    else:
                        if not buf.strip():                  # terminal closed with nothing pasted
                            return ""
                        buf += b"\n\n"
                if not buf:
                    continue
                text = (buf.decode("utf-8", "replace").replace("\r", "\n")
                        .replace("\x1b[200~", "").replace("\x1b[201~", ""))  # bracketed-paste markers
                if not text.endswith("\n"):
                    continue    # still typing, or pasted without a newline. This test comes FIRST: a
                                # stray space must not be read as "I am done" a second later, while the
                                # user is still fetching the phrase from their password manager.
                if not text.strip():
                    clear_counter()
                    return ""
                if text.endswith("\n\n") or single_line:
                    clear_counter()
                    return text
                if evaluate is None:
                    continue
                finished, status = evaluate(text)
                if finished:
                    clear_counter()
                    return text
                if status and status != last_status:
                    last_status = status
                    clear_counter()
                    print(dim(f"{indent}{status}"), flush=True)
        finally:
            buf[:] = b"\0" * len(buf)
            clear_counter()


# ------------------------------------------------------------------ the two questions

def ask_addresses(term):
    """Step 3: the addresses you already know. Optional, and only used to spot MISSING phrases."""
    print(bold("\U0001F4CB  3/5  Your addresses") + dim("   optional - press Enter to skip"))
    print("      Paste the Quantus addresses you already know. They are public, so they are shown back")
    print("      to you as you paste. They are used for one thing only: to tell you which of them no")
    print("      phrase reproduces, which is where a seed phrase is missing.")
    print(dim("      One paste is enough - it goes on by itself. Enter alone skips this step."))
    print()

    def evaluate(text):
        accepted = [a for a in dict.fromkeys(LOOSE_ADDRESS_RE.findall(text)) if valid_address(a)]
        return bool(accepted), None

    term.flush()
    text = term.read_paste(evaluate=evaluate, echo="lines")
    if not text.strip():
        print(dim("      skipped"))
        print()
        return []
    phrases, alternatives, _unrecognised = find_phrases(text)
    if phrases or alternatives:
        del phrases, alternatives
        print(yellow("      \u26A0  that contained a seed phrase, not just addresses. It was discarded and is"))
        print(yellow("         not used anywhere. Seed phrases are asked for in the next step."))
    found, rejected = [], []
    for candidate in dict.fromkeys(LOOSE_ADDRESS_RE.findall(text)):
        (found if valid_address(candidate) else rejected).append(candidate)
    print(green("      \u2714  " + count(len(found), "address", "addresses") + " accepted"))
    for candidate in rejected:
        print(yellow(f"      \u26A0  not a valid address (checksum - a typo?): {candidate[:24]}..."))
    print()
    return found


def ask_seeds(term):
    """Step 4: the seed phrases. Returns [{"label", "words", "alternative", "fingerprint"}], deduplicated."""
    print(bold("\U0001F331  4/5  Your seed phrases"))
    print("      Paste ONE phrase or ALL of them at once. Any shape works: numbered lines, one word per")
    print("      line, a phrase written as a grid, labels left in. " + bold("label | phrase") + " (in either order)")
    print("      names a phrase in the report, and a line starting with # is a comment.")
    print("      " + bold("Nothing you paste appears on screen") + " - that is deliberate, not a bug. Your phrases")
    print("      are never written to disk and never leave this machine.")
    print(dim("      Press Enter on an empty line when you are done."))

    def evaluate(text):
        found, unrecognised, _refused = parse_paste(text)
        real = [e for e in found if not e["alternative"]]
        if (real or found) and not unrecognised:
            return True, None
        words = len(wordlist_words(text))
        if not words:
            return False, None      # a paste that starts with a blank or label line: nothing to report yet
        return False, (f"{words} words in, {len(real)} complete phrase(s), "
                       f"{len(unrecognised)} unfinished - press Enter twice to go on anyway")

    entries, seen, asked_to_stop = [], set(), False
    while True:
        term.flush()
        print()
        print(dim("      waiting for seed phrases ..."), flush=True)
        text = term.read_paste(evaluate=evaluate)
        if not text.strip():
            if entries or asked_to_stop:
                break
            asked_to_stop = True
            print(yellow("      nothing pasted yet - press Enter again to stop, or paste now"))
            continue
        asked_to_stop = False
        found, unrecognised, refused = parse_paste(text)
        for n_words, salvaged, dropped in unrecognised:
            if dropped:
                print(yellow(f"      \u26A0  {n_words} words too jumbled to read - {salvaged} phrase(s) are "
                             f"checked but {dropped} more possible one(s) are NOT. Paste one per line."))
            elif salvaged:
                print(yellow(f"      \u26A0  {n_words} words that do not split cleanly into phrases - the "
                             f"{salvaged} phrase(s) inside are checked anyway"))
            else:
                print(yellow(f"      \u26A0  a run of {n_words} words is not a valid phrase (a typo, or a "
                             "missing word?) - skipped"))
        if refused:
            print(yellow(f"      \u26A0  {refused} label(s) looked like part of a phrase and were dropped, "
                         "not printed"))
        alternatives = 0
        for entry in found:
            fp = fingerprint(entry["words"])
            if fp in seen:
                if not entry["alternative"]:
                    print(dim(f"      already had this one ({fp[:6]}) - skipped"))
                continue
            seen.add(fp)
            entries.append(dict(entry, fingerprint=fp))
            if entry["alternative"]:
                alternatives += 1
                continue
            number = sum(1 for e in entries if not e["alternative"])
            name = '"' + entry["label"] + '"' if entry["label"] else f"#{number}"
            print(green("      \u2714") + f"  phrase {number:>2}  {dim(fp[:6])}  {name:<26} "
                  + dim(f"{len(entry['words'])} words"))
        if alternatives:
            print(dim(f"      + {alternatives} other way(s) to read the same words, checked as well "
                      "(a word from a label can shift where a phrase starts)"))
        del found
        print(dim("      Paste more, or press Enter on an empty line to go on."))
    return entries


# ------------------------------------------------------------------ deriving addresses

# This is the part no browser and no rewrite can do honestly: the address a phrase produced changed
# eight times across the chain's history, and the only trustworthy description of each generation is
# the release binary itself. So we run the official builds, one per generation, and read the address
# they print. Nothing they print is ever shown to you - only the addresses we parse out of it.

ADDRESS_LINE = re.compile(r"^\s*Address:\s*(qz[1-9A-HJ-NP-Za-km-z]{44,50})\s*$", re.M)

# A known answer for every generation: how many addresses it should yield per phrase, and a SHA-256
# of the address the published all-zero BIP39 test vector produces (a hash, so that this repository
# contains no Quantus address at all - the pre-commit guard enforces that). Checked on every run, before any
# result is shown. Without this, a build that cannot start on this machine (glibc too old, /tmp
# mounted noexec, a broken download) would quietly produce no address at all - and the tool would tell
# the user "nothing found", which reads as "your phrase is worthless". That mistake is unrecoverable.
CANARY = {
    "v0.1.0": (1, "593ce0d9fcb2b299"),
    "v0.2.5": (1, "16fa40ad6641ffcb"),
    "v0.2.10-matcha-shot": (10, "89f8754762a8ca49"),
    "v0.3.1": (10, "271c9cddace79e95"),
    "v0.4.2": (10, "4d8d1a309eac1414"),
    "v0.4.6": (10, "931f87da001d646e"),
    "v0.4.8-buah-naga": (10, "6dee6f52a182c330"),
    "v1.0.1": (20, "74ed3697ef13c04e"),
}
HD_ACCOUNTS = 9                                   # the team's own claim tool scans accounts 0..8
CHILD_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
DERIVE_TIMEOUT = 120


def hd_variants(has_hd):
    """Which key paths to try for one scheme."""
    if not has_hd:
        return [("default", [])]                  # the oldest builds have no derivation options
    return ([(f"HD account {i}", ["--wallet-index", str(i)]) for i in range(HD_ACCOUNTS)]
            + [("no derivation", ["--no-derivation"])])


def extract_node(version, sha256, into):
    """Unpack one official quantus-node from its archive, checking the pinned SHA256 first."""
    archive = cache_root() / "nodes" / archive_name(version)
    if not archive.exists():
        die(f"{archive.name} is missing from the cache.",
            "Fetch the official data first:  ./quantus_airdrop_checker.sh --download-only")
    if sha256_file(archive) != sha256:
        die(f"STOP: {archive.name} no longer matches its pinned SHA256. Not running it.")
    binary = into / "quantus-node"
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if pathlib.PurePosixPath(m.name).name == "quantus-node"]
        if len(members) != 1 or not members[0].isfile():
            die(f"STOP: {archive.name} does not hold exactly one quantus-node file.")
        with tar.extractfile(members[0]) as src, open(binary, "wb") as dst:
            shutil.copyfileobj(src, dst)
    binary.chmod(0o700)
    return binary


def node_features(binary, cwd):
    """(understands --wallet-index, takes the phrase as an argument). Asked, not guessed from the version.

    A probe that does not answer is retried once and then reported as a failure, never as "this build
    has no options": believing that would quietly cut nine of the ten key paths for that generation.
    """
    for attempt in range(2):
        try:
            run = subprocess.run([str(binary), "key", "quantus", "--help"], capture_output=True, text=True,
                                 timeout=DERIVE_TIMEOUT, cwd=str(cwd), env=dict(CHILD_ENV, HOME=str(cwd)),
                                 stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        helptext = run.stdout + run.stderr
        if run.returncode == 0 and "quantus" in helptext.lower():
            return "--wallet-index" in helptext, "--words <" in helptext
    return None


def derive(binary, schemes, words, cwd, features):
    """One phrase through one generation -> [(scheme, variant, address or None)]."""
    has_hd, phrase_on_argv = features
    phrase = " ".join(words)
    out = []
    for scheme in schemes:
        for variant, options in hd_variants(has_hd):
            address = None
            # the interface changed over the years: try what --help implied, then the other way round
            for on_argv in ([True, False] if phrase_on_argv else [False, True]):
                command = [str(binary), "key", "quantus", "--scheme", scheme, "--words"]
                if on_argv:
                    command.append(phrase)
                command += options
                try:
                    run = subprocess.run(command, input=None if on_argv else phrase + "\n",
                                         capture_output=True, text=True, timeout=DERIVE_TIMEOUT,
                                         cwd=str(cwd), env=dict(CHILD_ENV, HOME=str(cwd)))
                    # a build that will not accept the phrase invents a new one and prints ITS address
                    invented = "No seed or words provided" in run.stdout or "Generating a new" in run.stdout
                    found = None if invented else ADDRESS_LINE.search(run.stdout)
                    address = found.group(1) if found and valid_address(found.group(1)) else None
                except Exception:       # no details on purpose: a message could quote the phrase back
                    address = None
                if address:
                    break
            out.append((scheme, variant, address))
    return out


def test_vector(words_in_phrase=12):
    """The published all-zero BIP39 test vector. Belongs to nobody and is in every BIP39 test suite."""
    index = wordlist()
    ordered = sorted(index, key=lambda w: index[w])
    entropy = b"\x00" * (words_in_phrase * 11 * 32 // 33 // 8)
    bits = "".join(format(b, "08b") for b in entropy)
    bits += format(hashlib.sha256(entropy).digest()[0], "08b")[:len(entropy) * 8 // 32]
    return [ordered[int(bits[i:i + 11], 2)] for i in range(0, len(bits), 11)]


def check_generation(name, version, binary, schemes, work, features):
    """Refuse to go on unless this build reproduces its known answer on this machine."""
    broken = (f"STOP: the official {name} build ({version}) does not work on this machine.",
              "It is verified by SHA256, so the download is fine - it cannot RUN here.",
              "The usual reasons are an old distribution (these builds need glibc 2.39 or newer:",
              "Ubuntu 24.04+, Debian 13+, Fedora 39+) or /tmp mounted noexec.",
              "Try  ./quantus_airdrop_checker.sh --docker , which runs them in a current container.",
              "",
              "Stopping on purpose: a build that cannot run would find no addresses, and this tool",
              "would then tell you that you own nothing. That would be a lie, and you might act on it.")
    if features is None:
        die(*broken)
    expected_count, expected_digest = CANARY[version]
    results = derive(binary, schemes, test_vector(), work, features)
    found = [a for _s, _v, a in results if a]
    if len(found) != expected_count or hashlib.sha256(found[0].encode()).hexdigest()[:16] != expected_digest:
        die(*broken)


def derive_all(entries, note=print):
    """Every phrase through all eight generations -> {fingerprint: [(generation, covers, scheme, variant, address)]}.

    Generations on the outside, phrases on the inside: only one binary is ever unpacked at a time, so
    this needs ~30 MB of temporary space instead of the 600 MB all eight would take.
    """
    derived = {e["fingerprint"]: [] for e in entries}
    with tempfile.TemporaryDirectory(prefix="quantus-airdrop-checker-") as tmp:
        work = pathlib.Path(tmp)
        work.chmod(0o700)
        for i, (name, version, sha256, covers, schemes) in enumerate(GENERATIONS, 1):
            note(dim(f"      [{i}/{len(GENERATIONS)}] {name:<17} {version:<20} "), end="", flush=True)
            binary = extract_node(version, sha256, work)
            features = node_features(binary, work)
            check_generation(name, version, binary, schemes, work, features)
            addresses = 0
            for entry in entries:
                for scheme, variant, address in derive(binary, schemes, entry["words"], work, features):
                    if address:
                        derived[entry["fingerprint"]].append((name, covers, scheme, variant, address))
                        addresses += 1
            binary.unlink()
            note(green(f"{addresses:>4} addresses"))
    return derived


# ------------------------------------------------------------------ the report

def build_rows(entries, derived, data):
    """Derived addresses that are on an official list, one row per address."""
    rows, silent = [], []
    for number, entry in enumerate(entries, 1):
        hits, seen = [], set()
        for generation, covers, scheme, variant, address in derived[entry["fingerprint"]]:
            if address in seen:
                continue
            info = data.lookup(address)
            if info is None:
                continue
            seen.add(address)
            hits.append(dict(info, address=address, generation=generation, covers=covers, scheme=scheme,
                             variant=variant, blocks=sum(m[1] for m in info["mined"])))
        entry_rows = [dict(h, number=number, label=entry["label"], alternative=entry["alternative"],
                           fingerprint=entry["fingerprint"]) for h in hits]
        if entry_rows:
            rows += entry_rows
        elif not entry["alternative"]:
            silent.append((number, entry["label"], entry["fingerprint"]))
    rows.sort(key=lambda r: (STATUS_ORDER.get(r["status"], 9), -(r["qtc"] or 0), r["address"]))
    return rows, silent


def claim_gap_rows(rows):
    """Unclaimed rows the official claim code would miss - found here, but the claim tool cannot see them."""
    gap_generations = {name for name, version, *_rest in GENERATIONS if version in CLAIM_GAP}
    return [r for r in rows if r["status"] in (UNCLAIMED, UNKNOWN)
            and r["generation"] in gap_generations and r["variant"].startswith("HD account")]


def name_of(row):
    return row["label"] or f"#{row['number']}"


def show_address(address):
    """The whole address when the terminal is wide enough to take it, otherwise both ends of it."""
    return address if WIDE else short(address)


def qtc_text(value):
    return "?" if value is None else f"{value:.2f}"


def status_text(status):
    if status == UNCLAIMED:
        return bold(green(status))
    if status == WAITING:
        return yellow(status)
    if status in (PAID, NOT_IN_AIRDROP):
        return dim(status)
    return status


def totals(rows):
    """QTC per claim status, counting every address once."""
    out, seen = {}, set()
    for r in rows:
        if r["qtc"] is not None and r["status"] != NOT_IN_AIRDROP and r["address"] not in seen:
            seen.add(r["address"])
            out[r["status"]] = out.get(r["status"], 0.0) + r["qtc"]
    return out


def print_report(rows, silent, entries, known, data, missing):
    """What you came for, on screen: only what was found."""
    phrases = sum(1 for e in entries if not e["alternative"])
    address_column = 51 if WIDE else 24
    width = 79 + address_column          # the table below, including its longest status

    print()
    print(rule(width))
    print(" " + bold("\U0001F4CA  RESULT") + "    " + count(phrases, "seed phrase") + " checked    "
          + bold(count(len(rows), "address", "addresses") + " found on the official lists"))
    print(rule(width))

    if not rows:
        print()
        print("  " + yellow("None of the addresses from your phrases is on any official list."))
    else:
        print()
        print("  " + dim(f"{'#':>3}  {'Label':<12} {'Testnet':<12} {'Address':<{address_column}} "
                         f"{'Blocks':>7} {'QTC':>8}  Status"))
        for r in rows:
            line = (f"  {r['number']:>3}  {name_of(r)[:12]:<12} {'+'.join(r['testnets'])[:12]:<12} "
                    f"{show_address(r['address']):<{address_column}} {r['blocks'] or '-':>7} "
                    f"{qtc_text(r['qtc']):>8}  ")
            print((dim(line) if r["status"] in (PAID, NOT_IN_AIRDROP) else line) + status_text(r["status"]))
        print("  " + rule(width - 2))
        sums = totals(rows)
        if data.has_airdrop:
            print("  " + f"in total {sum(sums.values()):.2f} QTC:  " + f" {DOT} ".join(
                f"{k} {v:.2f}" for k, v in sorted(sums.items(), key=lambda kv: STATUS_ORDER.get(kv[0], 9))))
            unknown = [r for r in rows if r["status"] == UNKNOWN]
            if sums.get(UNCLAIMED):
                print("  " + bold(green(f"\U0001F4B0  NOT CLAIMED YET: {sums[UNCLAIMED]:.2f} QTC")
                                  + green(f"  - claim it before {CLAIM_DEADLINE}")))
            if unknown:
                print("  " + yellow(f"\u26A0  claim status unknown for {count(len(unknown), 'address', 'addresses')}"
                                    " - check them in the official Quantus app"))
            elif not sums.get(UNCLAIMED):
                print("  " + bold(f"\U0001F4B0  nothing left to claim ({sum(sums.values()):.2f} QTC in total)"))
        else:
            print("  " + yellow("amounts and claim status unknown: the claim server could not be used"))

    waiting = [r for r in rows if r["status"] == WAITING and r.get("claim_account")]
    if waiting:
        print()
        print("  " + yellow("\u26A0  claimed, waiting for payout - check that it was claimed to YOUR address:"))
        for r in waiting:
            print("     " + f"{show_address(r['address'])}  ->  {show_address(r['claim_account'])}")
        print("     " + dim("If that is not an address you control, someone else has this seed phrase."))

    not_in = [r for r in rows if r["status"] == NOT_IN_AIRDROP]
    if not_in:
        print()
        print("  " + dim(count(len(not_in), "address", "addresses") + " mined, but not on the airdrop list."))

    gap = claim_gap_rows(rows)
    if gap:
        print()
        print("  " + yellow(bold("\u26A0  the official claim tool does not find these addresses "
                                 "(checked 2026-09-22):")))
        for r in gap:
            print("     " + yellow(f"{show_address(r['address'])}  {'+'.join(r['testnets'])}  "
                                   f"from node {r['covers']}, {r['variant']}"))
        print("     " + dim("They are yours and on the list, but quantus-cli v2.3.0 and the app look only at other"))
        print("     " + dim("key paths for this node version. Ask the Quantus team in their official channels"))
        print("     " + dim("how to claim them - give them the address only, never the phrase."))

    if silent:
        print()
        names = ", ".join(f"#{n}" + (f' "{label}"' if label else "") for n, label, _fp in silent)
        print("  " + yellow("\u26A0  " + count(len(silent), "phrase") + f" matched nothing: {names}"))
        print("     " + dim('That does not mean the money is gone - see "No match?" in the README.'))

    if missing:
        print()
        print("  " + yellow("\u26A0  no phrase reproduced "
                            + count(len(missing), "address", "addresses") + " you pasted:"))
        for address, info in missing:
            where = (f"{'+'.join(info['testnets'])} {qtc_text(info['qtc'])} QTC, {info['status']}"
                     if info else "not on any official list")
            print("     " + yellow(f"{show_address(address)}  {where}"))
        print("     " + dim("You are missing the seed phrase for those."))

    print()
    if data.has_airdrop:
        print("  " + dim(f"Amounts and claim status: the team's claim server, as of "
                         f"{data.airdrop_meta['fetched_utc']}."))
        print("  " + dim("A row the server no longer lists as unpaid is shown as paid out."))
    print("  " + dim(f"Blocks: official miner lists fetched {data.meta['fetched_utc']}; "
                     "hashes are in the report file."))


def report_text(rows, silent, entries, known, derived, data, missing):
    """The long version, for the encrypted file: every address that was derived, not just the hits."""
    out = ["# Quantus airdrop check - report", "",
           f"- tool: quantus-airdrop-checker {VERSION}",
           f"- official miner lists fetched: {data.meta['fetched_utc']}"]
    for filename, info in data.meta["files"].items():
        out.append(f"  - `{filename}` git blob `{info['blob_sha']}` ({info['bytes']} bytes)")
    if data.has_airdrop:
        a = data.airdrop_meta
        out.append(f"- airdrop list and claim status: {a['server']}, fetched {a['fetched_utc']} "
                   f"({len(data.airdrop['rows'])} addresses, {len(data.airdrop['open'])} not paid yet"
                   + ("" if data.airdrop["status_ok"] else "; claim status unusable") + ")")
        for filename, digest in a["files"].items():
            out.append(f"  - `{filename}` sha256 `{digest}` (of the downloaded file)")
    else:
        out.append("- airdrop list: not available, so amounts and claim status are unknown")
    out += ["", "## What was found", "",
            "| # | label | testnets | address | blocks (rank) | QTC | status | claimed to | generation | key path |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        mined = ", ".join(f"{t} {b} (#{k}/{m})" for t, b, k, m in r["mined"]) or "-"
        out.append(f"| {r['number']} | {name_of(r)} | {'+'.join(r['testnets'])} | `{r['address']}` | {mined} | "
                   f"{qtc_text(r['qtc'])} | {r['status']} | {('`' + r['claim_account'] + '`') if r.get('claim_account') else ''} | "
                   f"{r['generation']} ({r['covers']}) | {r['scheme']}, {r['variant']} |")
    out += [""] + [f"- **{k}: {v:.2f} QTC**" for k, v in
                   sorted(totals(rows).items(), key=lambda kv: STATUS_ORDER.get(kv[0], 9))] + [""]
    if silent:
        out += ["## Phrases that matched nothing", ""]
        out += [f"- #{n}" + (f" \"{label}\"" if label else "") + f" (fingerprint {fp[:6]})"
                for n, label, fp in silent] + [""]
    if missing:
        out += ["## Addresses you pasted that no phrase reproduced", ""]
        out += [f"- `{a}` " + (f"{'+'.join(i['testnets'])} {qtc_text(i['qtc'])} QTC, {i['status']}"
                               if i else "not on any official list")
                for a, i in missing] + [""]
    out += ["## Everything that was derived", "",
            "Every address your phrases produce, whether or not it is on a list. Useful if you want to",
            "check one by hand, or to report a generation this tool gets wrong.", "",
            "| # | label | generation | scheme | key path | address |", "|---|---|---|---|---|---|"]
    numbers = {e["fingerprint"]: (i, e["label"], e["alternative"]) for i, e in enumerate(entries, 1)}
    for fingerprint, produced in derived.items():
        number, label, alternative = numbers[fingerprint]
        for generation, covers, scheme, variant, address in produced:
            out.append(f"| {number}{'a' if alternative else ''} | {label or ''} | {generation} ({covers}) | "
                       f"{scheme} | {variant} | `{address}` |")
    out += ["", "Seed phrases are not in this file, and were never written to disk.", ""]
    return "\n".join(out) + "\n"


def read_back(path, password):
    """Decrypt the report we just wrote. The password is typed once, so this is how we prove it opens."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, password.encode() + b"\n")
    finally:
        os.close(write_fd)
    try:
        run = subprocess.run(["gpg", "--batch", "--quiet", "--decrypt", "--pinentry-mode", "loopback",
                              "--no-symkey-cache", "--passphrase-fd", str(read_fd), str(path)],
                             capture_output=True, timeout=120, pass_fds=(read_fd,))
    except (OSError, subprocess.SubprocessError) as e:
        return f"it could not be read back ({type(e).__name__})"
    finally:
        os.close(read_fd)
    if run.returncode != 0 or not run.stdout.startswith(b"# Quantus airdrop check"):
        return "it could not be decrypted again with that password"
    return None


def write_encrypted(path, text, password):
    """gpg AES256, passphrase through a pipe - never on the command line, never in a file.

    Encrypts to a temporary file and moves it into place only when gpg succeeded, so a failed or
    interrupted run never leaves a half-written report that looks like the real thing.
    """
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, password.encode() + b"\n")
    finally:
        os.close(write_fd)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".quantus-airdrop-checker-", suffix=".gpg")
    os.close(tmp_fd)
    try:
        run = subprocess.run(["gpg", "--batch", "--yes", "--symmetric", "--cipher-algo", "AES256",
                              "--no-symkey-cache", "--passphrase-fd", str(read_fd), "--output", tmp_name],
                             input=text.encode(), capture_output=True, pass_fds=(read_fd,), timeout=120)
        if run.returncode != 0 or not os.path.getsize(tmp_name):
            return "gpg refused to encrypt the report"
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        tmp_name = None
        return None
    except (OSError, subprocess.SubprocessError) as e:
        return f"gpg could not be run ({type(e).__name__})"
    finally:
        os.close(read_fd)
        if tmp_name:
            pathlib.Path(tmp_name).unlink(missing_ok=True)


# ------------------------------------------------------------------ main

AUTHOR = "popek_1990"
AUTHOR_X = "https://x.com/popek_1990"
# Shown once, at the very end, and ONLY when the run actually found something. A tool that asks for
# money before it has been of any use is a tool nobody should trust.
TIP_ADDRESS = "qzpaZ83dYsCyDdCcTgDhFj3WCPuPnvaEXJjj4FECDe9i4xWE9"


def banner():
    return "\n".join([
        bold(f"\U0001FA99  Quantus airdrop checker {VERSION}")
        + dim("  -  community tool, not affiliated with the Quantus team"),
        dim("   Finds what your phrases hold in the airdrop and whether it is still unclaimed. It cannot claim"),
        dim("   anything itself. Any page or DM asking for a seed phrase is a scam."),
        dim(f"   by {AUTHOR}  {DOT}  {AUTHOR_X}"),
    ])


def steps():
    return "\n".join([
        "   " + bold("\U0001F511 1/5") + dim("  Password        encrypts the report file at the end; your phrases are never saved"),
        "   " + bold("\U0001F4E6 2/5") + dim("  Official data   8 quantus-node builds, 4 miner lists, the airdrop list"),
        "   " + bold("\U0001F4CB 3/5") + dim("  Your addresses  optional - finds out which seed phrase you are missing"),
        "   " + bold("\U0001F331 4/5") + dim("  Your phrases    paste them all at once; nothing is shown on screen"),
        "   " + bold("\U0001F4CA 5/5") + dim("  Report          on screen, and in an encrypted file"),
    ])


def tip():
    return "\n".join([
        dim("   \u2615  Found coins you had written off? A tip is welcome, never expected:"),
        dim(f"      {TIP_ADDRESS}"),
        dim("      Nothing here ever needs a payment to work - and anyone telling you that claiming"),
        dim("      an airdrop costs a fee is robbing you."),
    ])


def next_steps(rows=()):
    lines = [bold("\U0001F4CC  What now:")]
    if any(r["status"] == UNCLAIMED for r in rows):
        lines += [f"   \u2022 claim what is marked NOT CLAIMED YET before {CLAIM_DEADLINE}, with the official Quantus",
                  "     mobile app or the official quantus-cli (quantus airdrop claim) - never on a web page;"]
    if any(r["status"] == WAITING for r in rows):
        lines += ["   \u2022 'claimed, waiting for payout': the team pays in batches by hand - nothing to do but wait;"]
    if any(r["status"] == UNKNOWN for r in rows):
        lines += ["   \u2022 'unknown': run --download-only again later, or check the address in the official app;"]
    lines += ["   \u2022 keep every seed phrase you have; the amounts come from the team's claim server and the",
              "     claim itself is always done by you, with the official tools. Any page or DM asking for",
              "     your phrase is a scam.",
              "",
              dim(f"   \U0001F464  {AUTHOR}  {DOT}  {AUTHOR_X}")]
    return "\n".join(lines)


def ask_password(term):
    """Step 1: one password, typed once, and it encrypts the report file - nothing else."""
    print(bold("\U0001F511  1/5  Password"))
    if not shutil.which("gpg"):
        print(dim("      gpg is not installed here, so no file can be encrypted."))
        print(dim("      The report will be shown on screen only - nothing is written to disk."))
        print()
        return None
    print("      It encrypts the report file written at the end, and nothing else. Your seed phrases")
    print("      are " + bold("never saved") + ", not even encrypted - you paste them again next time, which takes")
    print("      seconds. So this password guards a list of addresses, not your money.")
    print(dim("      You will see one * per character. Enter alone = no file, screen only."))
    print()
    term.flush()
    term.write("      password: ")
    password = term.read_paste(echo="stars", single_line=True).strip()
    if not password:
        print(dim("      no file will be written - the report is shown on screen only"))
        print()
        return None
    print(green(f"      \u2714  {len(password)} characters"))
    print(dim("      the file is decrypted again right after writing, to prove the password works"))
    print()
    return password


def missing_addresses(known, rows, data):
    """Addresses you pasted that no phrase reproduced - i.e. where a seed phrase is missing."""
    reproduced = {r["address"] for r in rows}
    return [(address, data.lookup(address)) for address in known if address not in reproduced]


def self_test():
    """Derive from the published BIP39 test vectors and compare with the addresses pinned in tests/.

    No question is asked, no phrase of yours is involved: the vectors are the all-zero entropy phrases
    every BIP39 implementation is tested with. If this passes, the eight binaries on this machine
    behave exactly as they did when the expected values were recorded.
    """
    index = wordlist()
    words = sorted(index, key=lambda w: index[w])

    def vector(entropy):
        bits = "".join(format(b, "08b") for b in entropy)
        bits += format(hashlib.sha256(entropy).digest()[0], "08b")[:len(entropy) * 8 // 32]
        return [words[int(bits[i:i + 11], 2)] for i in range(0, len(bits), 11)]

    phrases = [vector(b"\x00" * 16), vector(b"\x00" * 32)]
    entries = [{"label": f"test vector {len(p)} words", "words": p, "alternative": False,
                "fingerprint": fingerprint(p)} for p in phrases]
    print("Deriving addresses from the public BIP39 test vectors (no phrase of yours is used):")
    derived = derive_all(entries)
    got = {e["fingerprint"]: sorted((g, s, v, a) for g, _c, s, v, a in derived[e["fingerprint"]])
           for e in entries}

    path = pathlib.Path(__file__).resolve().parent / "tests" / "vectors.json"
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        write_private(path, json.dumps({fp: [list(x) for x in rows] for fp, rows in got.items()},
                                       indent=1).encode())
        path.chmod(0o644)
        print(f"\nNo expected values yet - recorded {sum(len(v) for v in got.values())} addresses in "
              f"{path.relative_to(pathlib.Path.cwd()) if str(path).startswith(str(pathlib.Path.cwd())) else path}")
        return 0
    expected = {fp: sorted(tuple(x) for x in rows) for fp, rows in json.loads(path.read_text()).items()}
    problems = []
    for fingerprint_, rows in expected.items():
        if fingerprint_ not in got:
            problems.append(f"test vector {fingerprint_[:6]} was not derived at all")
            continue
        if got[fingerprint_] != rows:
            extra = [r for r in got[fingerprint_] if r not in rows]
            lost = [r for r in rows if r not in got[fingerprint_]]
            problems.append(f"{fingerprint_[:6]}: {len(lost)} address(es) missing, {len(extra)} unexpected")
            for row in (lost + extra)[:6]:
                problems.append(f"    {row[0]} / {row[1]} / {row[2]} -> {row[3]}")
    print()
    for problem in problems:
        print("FAIL: " + problem)
    total = sum(len(v) for v in expected.values())
    print(f"{total} expected addresses, {'all reproduced - PASS' if not problems else 'FAILED'}")
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="quantus_airdrop_checker.sh", description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--download-only", action="store_true",
                    help="fetch and verify the official data, then stop (no seed phrases asked for)")
    ap.add_argument("--offline", action="store_true",
                    help="refuse to touch the network; use the data already in the cache")
    ap.add_argument("--self-test", action="store_true",
                    help="derive from public BIP39 test vectors and check the result (asks for nothing)")
    ap.add_argument("--no-color", action="store_true", help="plain text, no colours (same as NO_COLOR=1)")
    ap.add_argument("--docker", action="store_true", help=argparse.SUPPRESS)  # handled by the launcher
    args = ap.parse_args(argv)
    if args.no_color:
        os.environ["NO_COLOR"] = "1"

    global NETWORK
    if args.offline and args.download_only:
        ap.error("--offline and --download-only contradict each other: downloading needs the network.")
    NETWORK = not args.offline

    look_at_the_terminal()
    harden(need_tty=not (args.download_only or args.self_test))
    print(banner())
    print()

    if args.self_test:
        return self_test()

    if args.download_only:
        print(bold("\U0001F4E6  2/5  Official data"))
        meta, airdrop = official_data()
        print()
        for line in OfficialData(meta, airdrop).summary_lines():
            print("      " + line)
        if airdrop is None:
            print(yellow("\nThe builds and miner lists are ready, but the airdrop list could not be downloaded."))
            print("Try again later; without it, amounts and claim status are unknown.")
            return 2
        print("\nCache is ready. You can now run this on a machine with no network:")
        print("  ./quantus_airdrop_checker.sh --offline")
        return 0

    print(steps())
    print()
    with HiddenTerminal() as term:
        password = ask_password(term)
        print(bold("\U0001F4E6  2/5  Official data") + (dim("   offline: cache only") if not NETWORK else ""))
        data = OfficialData(*official_data())
        for line in data.summary_lines():
            print("      " + line)
        print()
        # from here on the phrases are in memory, so ONE guard covers everything: no exception text and
        # no traceback ever reaches the screen, because either could be built from what was pasted
        found_something = False
        try:
            known = ask_addresses(term)
            entries = ask_seeds(term)
            if not entries:
                print(yellow("\nNo seed phrases were given, so there is nothing to check."))
                return 0

            print("\n" + bold("\U0001F4CA  5/5  Checking " + count(len(entries), "phrase")
                              + " against 8 generations of address formats"))
            print(dim("      Seven of the eight builds accept the phrase only as a command-line argument,"))
            print(dim("      so for a fraction of a second each phrase is visible in this machine's process"))
            print(dim("      list - to anyone logged in as you, and to root. That is true in --docker too,"))
            print(dim("      because a container's processes are this machine's processes. Nothing leaves"))
            print(dim("      the machine, but if someone else is logged in here, stop now."))
            derived = derive_all(entries)
            rows, silent = build_rows(entries, derived, data)
            missing = missing_addresses(known, rows, data)
            found_something = bool(rows)
            print_report(rows, silent, entries, known, data, missing)

            if password:
                day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
                path = pathlib.Path.cwd() / f"quantus-airdrop-report-{day}.md.gpg"
                problem = write_encrypted(path, report_text(rows, silent, entries, known, derived,
                                                            data, missing), password)
                if not problem:
                    problem = read_back(path, password)
                print()
                if problem:
                    print(yellow(f"  \u26A0  the report file was not kept: {problem}."))
                    pathlib.Path(path).unlink(missing_ok=True)
                else:
                    print("  " + green("\U0001F512  full report (every derived address) encrypted to "
                                       + path.name))
                    print("  " + dim(f"read it with:  gpg -d {path.name}"))
        except KeyboardInterrupt:
            print("\nStopped. Nothing was saved.")
            return 1
        except Exception as e:
            print(f"\nSomething went wrong ({type(e).__name__}). Nothing was saved.")
            print("Please report this, but NEVER include what you pasted.")
            return 1
    print()
    print(next_steps(rows if found_something else ()))
    if found_something:
        print()
        print(tip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
