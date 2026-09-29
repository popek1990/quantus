#!/usr/bin/env python3
"""Quantus rewards finder - a community tool. NOT affiliated with the Quantus team.

What it does
    Run it on a machine that mined Quantus (any testnet or mainnet). It searches the
    machine for every trace a Quantus node leaves about WHERE its mining rewards went:
    node logs, rotated and compressed logs, systemd journal, Docker container logs,
    start scripts, service files, shell history, config files, running processes and
    quantus-cli wallet files. It then prints every rewards address it found, the chain
    it was used on, when, and where the evidence is.

Why
    The testnet airdrop is keyed by the address that received the block rewards. That
    address is whatever was passed to the node (--rewards-address, --rewards-preimage,
    --rewards-inner-hash), which is often NOT the address of your phone wallet. If you
    lost track of it, the node logs on the mining machine still have it.

What it never does
    It never prints, stores or sends a seed phrase, secret key or wormhole secret.
    It only extracts public addresses (and shows wormhole inner hashes shortened).
    It works offline. The optional --check-airdrop downloads the PUBLIC airdrop lists
    and matches them locally: your addresses are never sent anywhere.

Read the code before you run it.
"""

import argparse
import bz2
import datetime as dt
import gzip
import hashlib
import json
import lzma
import os
import re
import stat
import subprocess
import sys
import time
import urllib.request

__version__ = "1.1.0"

# --------------------------------------------------------------------------------------
# SS58 addresses (Quantus uses network prefix 189, which makes every address start "qz")
# --------------------------------------------------------------------------------------

SS58_PREFIX = 189
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {c: i for i, c in enumerate(B58)}


def _prefix_bytes(ident):
    """Two-byte SS58 prefix encoding (used for network ids 64..16383)."""
    first = ((ident & 0b1111_1100) >> 2) | 0b0100_0000
    second = (ident >> 8) | ((ident & 0b11) << 6)
    return bytes([first, second])


PREFIX = _prefix_bytes(SS58_PREFIX)


def _checksum(payload):
    return hashlib.blake2b(b"SS58PRE" + payload, digest_size=64).digest()[:2]


def b58encode(raw):
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(raw) - len(raw.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(text):
    n = 0
    for c in text:
        if c not in B58_INDEX:
            raise ValueError("not base58")
        n = n * 58 + B58_INDEX[c]
    pad = len(text) - len(text.lstrip("1"))
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * pad + body


def account_to_ss58(account):
    """32-byte account id -> Quantus address (qz...)."""
    if len(account) != 32:
        raise ValueError("account id must be 32 bytes")
    payload = PREFIX + account
    return b58encode(payload + _checksum(payload))


def ss58_to_account(address):
    """Quantus address -> 32-byte account id. Raises ValueError if it is not a valid one."""
    raw = b58decode(address)
    if len(raw) != 36 or raw[:2] != PREFIX:
        raise ValueError("not a Quantus (network 189) address")
    if _checksum(raw[:34]) != raw[34:]:
        raise ValueError("bad checksum")
    return raw[2:34]


# --------------------------------------------------------------------------------------
# What we look for
# --------------------------------------------------------------------------------------

SS58_RE = rb"qz[1-9A-HJ-NP-Za-km-z]{44,50}"
HEX_RE = rb"(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])"
VALUE = rb"[\"']?(?:(?P<hex>" + HEX_RE + rb")|(?P<ss58>" + SS58_RE + rb"))"

# (kind, pattern, label). Kinds, from strongest evidence to weakest:
#   mined       the node itself said at start-up that it pays block rewards to this address
#   configured  set as the rewards address in a command line, script, config or shell history
#   generated   printed by key generation (quantus-node key ... / quantus-cli)
#   wallet      a quantus-cli wallet file
#   treasury    the node had no rewards address, so rewards went to the treasury (not yours)
# Order does not matter; matches on a line are processed by position.
RULES = [
    ("mined", rb"Using provided rewards address:\s*" + VALUE,
     "node log: rewards address"),
    ("mined", rb"Using address for rewards:\s*" + VALUE,
     "node log: rewards address"),
    ("mined", rb"Rewards wormhole address:\s*" + VALUE,
     "node log: rewards wormhole address"),
    ("treasury", rb"Using treasury address for rewards:\s*" + VALUE,
     "node log: NO rewards address set, rewards went to the treasury"),
    ("configured", rb"--rewards-address(?:=|\s+)" + VALUE,
     "command line: --rewards-address"),
    ("configured", rb"\bREWARDS_ADDRESS\b[\"']?\s*[:=]\s*" + VALUE,
     "config: REWARDS_ADDRESS"),
    ("generated", rb"(?:^|[\r\n]|\\n)[ \t]*(?:Wormhole )?Address:[ \t]*(?P<ss58>" + SS58_RE + rb")",
     "key generation output"),
]
INNER_HASH_RULES = [
    (rb"--rewards-(?:preimage|inner-hash)(?:=|\s+)[\"']?(?P<hex>" + HEX_RE + rb")",
     "command line: --rewards-preimage / --rewards-inner-hash"),
    (rb"\bREWARDS_(?:PREIMAGE|INNER_HASH)\b[\"']?\s*[:=]\s*[\"']?(?P<hex>" + HEX_RE + rb")",
     "config: REWARDS_PREIMAGE / REWARDS_INNER_HASH"),
    (rb"Inner Hash:\s*(?P<hex>" + HEX_RE + rb")",
     "key generation output: Inner Hash"),
]
CHAIN_RE = re.compile(rb"Chain specification:\s*([^\r\n\\]{1,60})")
VERSION_RE = re.compile(rb"\xe2\x9c\x8c\xef?\xb8?\x8f?\s*version\s+([0-9][0-9A-Za-z.+_-]{0,40})")
TIME_RE = re.compile(rb"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")

COMPILED = [(k, re.compile(p), label) for k, p, label in RULES]
COMPILED_INNER = [(re.compile(p), label) for p, label in INNER_HASH_RULES]

# Cheap first pass: a line is only examined in detail if one of these words is in it.
NEEDLES = (b"ewards", b"REWARDS", b"Address:", b"Inner Hash", b"Chain specification",
           b"\xe2\x9c\x8c")
PREFILTER = re.compile(b"|".join(re.escape(n) for n in NEEDLES))

CHUNK = 8 * 1024 * 1024
MAX_CARRY = 1024 * 1024
SAMPLE = 8192

SKIP_DIR_NAMES = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".cargo", ".rustup",
                  ".npm", ".venv", "venv", ".tox", ".mypy_cache"}
SKIP_ROOT_DIRS = {"/proc", "/sys", "/dev", "/run", "/snap"}
# Node databases hold no addresses in readable form and can be hundreds of GB.
SKIP_PATH_PARTS = ("/db/full/", "/paritydb/", "/rocksdb/", "/keystore/",
                   "/var/lib/snapd/", "/var/lib/containerd/")

AIRDROP_SERVER = "https://airdrop-claim.quantus.com"


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------

class Findings:
    def __init__(self):
        self.addresses = {}     # account hex -> record
        self.inner_hashes = {}  # full hex -> record (never printed in full)
        self.stats = {"files": 0, "bytes": 0, "binary": 0, "unreadable": 0, "sources": 0}
        self.unreadable_examples = []

    def add_address(self, kind, label, account, source, chain, version, when):
        key = account.hex()
        rec = self.addresses.setdefault(key, {
            "address": account_to_ss58(account), "account_hex": "0x" + key, "kinds": set(),
            "labels": set(), "chains": set(), "versions": set(), "sources": {}, "count": 0,
            "first_seen": None, "last_seen": None,
        })
        rec["kinds"].add(kind)
        rec["labels"].add(label)
        if chain:
            rec["chains"].add(chain)
        if version:
            rec["versions"].add(version)
        rec["sources"][source] = rec["sources"].get(source, 0) + 1
        rec["count"] += 1
        if when:
            if rec["first_seen"] is None or when < rec["first_seen"]:
                rec["first_seen"] = when
            if rec["last_seen"] is None or when > rec["last_seen"]:
                rec["last_seen"] = when

    def add_inner_hash(self, value, label, source, chain):
        rec = self.inner_hashes.setdefault(value.lower(), {
            "labels": set(), "chains": set(), "sources": {}})
        rec["labels"].add(label)
        if chain:
            rec["chains"].add(chain)
        rec["sources"][source] = rec["sources"].get(source, 0) + 1

    def unreadable(self, path):
        self.stats["unreadable"] += 1
        if len(self.unreadable_examples) < 5:
            self.unreadable_examples.append(path)


# --------------------------------------------------------------------------------------
# Scanning a stream of bytes (file, journal, docker logs, process command lines)
# --------------------------------------------------------------------------------------

def _decode_value(m):
    """Return a 32-byte account id from a regex match, or None if it is not a valid one."""
    hx = m.groupdict().get("hex")
    if hx:
        hx = hx[2:] if hx[:2].lower() == b"0x" else hx
        return bytes.fromhex(hx.decode())
    ss = m.groupdict().get("ss58")
    if ss:
        try:
            return ss58_to_account(ss.decode())
        except ValueError:
            return None
    return None


def _timestamp_before(line, pos):
    last = None
    for t in TIME_RE.finditer(line, max(0, pos - 400), pos):
        last = t
    if last:
        return "%s %s" % (last.group(1).decode(), last.group(2).decode())
    return None


def _chain_from_path(source):
    m = re.search(r"/chains/([^/]+)/", source)
    return m.group(1) if m else None


class StreamScanner:
    """Feeds bytes line by line through the rules. Keeps the last chain/version it saw,
    so every address is attributed to the chain the node was running at that moment."""

    def __init__(self, findings, source):
        self.f = findings
        self.source = source
        self.chain = _chain_from_path(source)
        self.version = None
        self.carry = b""

    def feed(self, data, final=False):
        block = self.carry + data
        if final:
            cut = len(block)
        else:
            cut = block.rfind(b"\n") + 1
            if cut == 0 and len(block) > MAX_CARRY:
                cut = len(block)
        self.carry = block[cut:]
        self._scan(block[:cut])

    def close(self):
        self.feed(b"", final=True)

    def _scan(self, block):
        # Almost all of a node log is sync/import chatter; skip such chunks at memchr speed.
        if not any(n in block for n in NEEDLES):
            return
        last_line = -1
        for m in PREFILTER.finditer(block):
            start = block.rfind(b"\n", 0, m.start()) + 1
            if start == last_line:
                continue
            last_line = start
            end = block.find(b"\n", m.end())
            self._line(block[start:len(block) if end < 0 else end])

    def _line(self, line):
        events = []
        for cm in CHAIN_RE.finditer(line):
            events.append((cm.start(), "chain", cm))
        for vm in VERSION_RE.finditer(line):
            events.append((vm.start(), "version", vm))
        for kind, rx, label in COMPILED:
            for m in rx.finditer(line):
                events.append((m.start(), kind, (m, label)))
        for rx, label in COMPILED_INNER:
            for m in rx.finditer(line):
                events.append((m.start(), "inner", (m, label)))
        events.sort(key=lambda e: e[0])
        for pos, kind, payload in events:
            if kind == "chain":
                self.chain = payload.group(1).decode("utf-8", "replace").strip()
            elif kind == "version":
                self.version = payload.group(1).decode("utf-8", "replace")
            elif kind == "inner":
                m, label = payload
                self.f.add_inner_hash(m.group("hex").decode(), label, self.source, self.chain)
            else:
                m, label = payload
                account = _decode_value(m)
                if account is not None:
                    self.f.add_address(kind, label, account, self.source, self.chain,
                                       self.version, _timestamp_before(line, pos))


# --------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------

def _open_maybe_compressed(path):
    lower = path.lower()
    if lower.endswith(".gz"):
        return gzip.open(path, "rb")
    if lower.endswith(".xz") or lower.endswith(".lzma"):
        return lzma.open(path, "rb")
    if lower.endswith(".bz2"):
        return bz2.open(path, "rb")
    return open(path, "rb")


def scan_wallet_file(findings, path):
    """quantus-cli keeps one JSON file per wallet. Only the public 'address' is read."""
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(4 * 1024 * 1024))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("address"), str):
        return False
    try:
        account = ss58_to_account(data["address"])
    except ValueError:
        return False
    name = data.get("name") if isinstance(data.get("name"), str) else os.path.basename(path)
    findings.add_address("wallet", "quantus-cli wallet '%s'" % name[:40], account, path,
                         None, None, data.get("created_at") if isinstance(data.get("created_at"), str) else None)
    return True


def scan_file(findings, path, progress):
    if "/.quantus/wallets/" in path and path.endswith(".json"):
        if scan_wallet_file(findings, path):
            findings.stats["files"] += 1
            return
    try:
        fh = _open_maybe_compressed(path)
    except OSError:
        findings.unreadable(path)
        return
    try:
        first = fh.read(SAMPLE)
        if b"\0" in first or first.startswith(b"\x7fELF"):
            findings.stats["binary"] += 1
            return
        findings.stats["files"] += 1
        scanner = StreamScanner(findings, path)
        scanner.feed(first)
        total = len(first)
        big = False
        while True:
            data = fh.read(CHUNK)
            if not data:
                break
            total += len(data)
            scanner.feed(data)
            if total > 256 * 1024 * 1024:
                big = True
                progress("  reading %s  %.1f GB" % (path, total / 1e9))
        scanner.close()
        findings.stats["bytes"] += total
        if big:
            progress(None)
    except (OSError, EOFError, lzma.LZMAError, ValueError):
        findings.unreadable(path)
    finally:
        fh.close()


def walk(findings, root, progress, seen):
    root = os.path.abspath(root)
    try:
        st = os.stat(root)
    except OSError:
        return
    if stat.S_ISREG(st.st_mode):
        scan_file(findings, root, progress)
        return
    last = 0.0

    def onerror(err):
        findings.unreadable(getattr(err, "filename", str(err)))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIR_NAMES
                       # Docker's "merged" view duplicates the layers already walked
                       and not (d == "merged" and os.path.basename(os.path.dirname(dirpath)) == "overlay2")
                       and os.path.join(dirpath, d) not in SKIP_ROOT_DIRS
                       and not any(p in os.path.join(dirpath, d) + "/" for p in SKIP_PATH_PARTS)]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                findings.unreadable(path)
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
                continue
            ident = (st.st_dev, st.st_ino)
            if ident in seen:
                continue
            seen.add(ident)
            if not os.access(path, os.R_OK):
                findings.unreadable(path)
                continue
            scan_file(findings, path, progress)
            now = time.monotonic()
            if now - last > 0.5:
                last = now
                progress("  %s  (%d files read)" % (dirpath[-70:], findings.stats["files"]))
    progress(None)


def scan_command(findings, source, argv):
    """Stream the output of a command (journalctl, docker logs) through the scanner."""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    scanner = StreamScanner(findings, source)
    total = 0
    while True:
        data = proc.stdout.read(CHUNK)
        if not data:
            break
        total += len(data)
        scanner.feed(data)
    scanner.close()
    proc.wait()
    findings.stats["bytes"] += total
    findings.stats["sources"] += 1
    return proc.returncode == 0 or total > 0


def scan_journal(findings, progress):
    progress("  systemd journal ...")
    scan_command(findings, "journald (system)", ["journalctl", "--no-pager", "-o", "short-iso"])
    scan_command(findings, "journald (user)", ["journalctl", "--user", "--no-pager", "-o", "short-iso"])
    progress(None)


def scan_docker(findings, progress):
    try:
        out = subprocess.run(["docker", "ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}"],
                             capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) != 3 or not re.search(r"quantus|qtc|resonance|schrodinger|dirac|planck",
                                            row, re.IGNORECASE):
            continue
        cid, name, image = parts
        progress("  docker logs %s (%s)" % (name, image))
        scan_command(findings, "docker logs %s (%s)" % (name, image),
                     ["docker", "logs", "--timestamps", cid])
    progress(None)


def scan_processes(findings):
    """Running nodes: their command line and environment show the rewards settings."""
    if not os.path.isdir("/proc"):
        return
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % pid, "rb") as fh:
                comm = fh.read().strip().decode("utf-8", "replace")
        except OSError:
            continue
        for what in ("cmdline", "environ"):
            try:
                with open("/proc/%s/%s" % (pid, what), "rb") as fh:
                    data = fh.read().replace(b"\0", b"\n" if what == "environ" else b" ")
            except OSError:
                continue
            if data:
                scanner = StreamScanner(findings, "running process %s (%s), %s" % (pid, comm, what))
                scanner.feed(data + b"\n", final=True)


def default_roots():
    roots = [os.path.expanduser("~")]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        roots += ["/root", "/home"]
    roots += ["/var/log", "/var/lib", "/opt", "/srv", "/etc", "/usr/local", "/tmp", "/mnt", "/media"]
    out = []
    for r in roots:
        r = os.path.realpath(r)
        if os.path.isdir(r) and not any(r == o or r.startswith(o + os.sep) for o in out):
            out = [o for o in out if not o.startswith(r + os.sep)] + [r]
    return out


# --------------------------------------------------------------------------------------
# Optional: look the addresses up in the public airdrop lists
# --------------------------------------------------------------------------------------

def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "quantus-rewards-finder/" + __version__})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def check_airdrop(findings, server):
    """Downloads the two PUBLIC lists and matches locally. Nothing about you is sent."""
    snapshot = _get_json(server + "/snapshot")["rows"]
    unpaid = {r["address"]: r for r in _get_json(server + "/unpaid")["rows"]}
    by_account = {}
    for row in snapshot:
        acc = str(row.get("account", "")).lower()
        if acc:
            by_account[acc] = row
    for rec in findings.addresses.values():
        row = by_account.get(rec["account_hex"].lower())
        if row is None:
            rec["airdrop"] = None
            continue
        state = unpaid.get(row["address"])
        if state is None:
            status = "paid out"
        elif state.get("status") == "unclaimed":
            status = "NOT CLAIMED YET"
        elif state.get("status") == "recorded":
            status = "claimed, waiting for payout"
        else:
            status = str(state.get("status"))
        rec["airdrop"] = {"qtc": row["amount_hundredths"] / 100, "testnets": row.get("testnets", []),
                          "status": status}
    return len(snapshot)


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------

def _times(n):
    return "1 time" if n == 1 else "%d times" % n


def _short_hash(value):
    v = value[2:] if value.startswith("0x") else value
    return "0x%s…%s" % (v[:6], v[-4:])


def _sources_text(sources, limit=3):
    items = sorted(sources.items(), key=lambda kv: -kv[1])
    lines = ["%s  (x%d)" % (s, n) if n > 1 else s for s, n in items[:limit]]
    if len(items) > limit:
        lines.append("… and %d more" % (len(items) - limit))
    return lines


SECTIONS = [
    ("mined", "Addresses a node mined to",
     "The node itself logged at start-up that block rewards go to these addresses."),
    ("configured", "Addresses set as the rewards address",
     "Found in a command line, start script, config file or shell history. Probably used for\n"
     " mining, but only a node log proves the node actually ran with it."),
    ("other", "Other Quantus addresses on this machine",
     "Key generation output and quantus-cli wallets. Not necessarily used for mining, and notes\n"
     " or chat logs can also contain other people's addresses."),
]


def _section(rec):
    if "mined" in rec["kinds"]:
        return "mined"
    if "configured" in rec["kinds"]:
        return "configured"
    return "other"


AUTHOR = "popek_1990"
AUTHOR_X = "https://x.com/popek_1990"
PROJECT_SITE = "https://quantus.watch"


class Style:
    """ANSI colours for a terminal; plain text when piped, redirected, or NO_COLOR is set."""

    CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
             "blue": "34", "cyan": "36"}

    def __init__(self, enabled):
        self.enabled = enabled

    def __call__(self, text, *styles):
        if not self.enabled or not styles:
            return text
        return "\033[%sm%s\033[0m" % (";".join(self.CODES[x] for x in styles), text)


def use_colour(stream, disabled=False):
    if disabled or os.environ.get("NO_COLOR"):
        return False
    try:
        return stream.isatty() and os.environ.get("TERM") != "dumb"
    except (AttributeError, ValueError):
        return False


STATUS_STYLE = {"NOT CLAIMED YET": ("green", "bold"), "claimed, waiting for payout": ("yellow",),
                "paid out": ("dim",)}


def _print_record(w, c, i, r, checked):
    w("\n %s %s\n" % (c("%2d." % i, "dim"), c(r["address"], "bold")))
    w("     %s   %s\n" % (c("account id", "dim"), r["account_hex"]))
    w("     %s        %s\n" % (c("chain", "dim"), c(", ".join(sorted(r["chains"])) or "unknown", "cyan")))
    if r["versions"]:
        w("     %s %s\n" % (c("node version", "dim"), ", ".join(sorted(r["versions"]))))
    w("     %s     %s\n" % (c("found as", "dim"), "; ".join(sorted(r["labels"]))))
    if r["first_seen"]:
        span = r["first_seen"] if r["first_seen"] == r["last_seen"] else \
            "%s  ->  %s" % (r["first_seen"], r["last_seen"])
        w("     %s         %s  (%s)\n" % (c("seen", "dim"), span, _times(r["count"])))
    else:
        w("     %s         %s\n" % (c("seen", "dim"), _times(r["count"])))
    for j, line in enumerate(_sources_text(r["sources"])):
        w("     %s %s\n" % (c("where       ", "dim") if j == 0 else "            ", line))
    if checked:
        a = r.get("airdrop")
        if a is None:
            w("     %s      %s\n" % (c("airdrop", "dim"), c("not on the airdrop list", "dim")))
        else:
            w("     %s      %s (%s)  -  %s\n"
              % (c("airdrop", "dim"), c("%.2f QTC" % a["qtc"], "bold"), ", ".join(a["testnets"]),
                 c(a["status"], *STATUS_STYLE.get(a["status"], ()))))


def print_report(findings, checked, out=None, colour=None):
    out = out or sys.stdout
    w = out.write
    c = Style(use_colour(out) if colour is None else colour)
    recs = sorted(findings.addresses.values(), key=lambda r: (r["first_seen"] or "9999", r["address"]))
    treasury = [r for r in recs if r["kinds"] == {"treasury"}]
    recs = [r for r in recs if r["kinds"] != {"treasury"}]
    s = findings.stats
    w("\n%s\n" % c("Scanned %d text files and %d other sources, %.2f GB. Skipped %d binary files."
                   % (s["files"], s["sources"], s["bytes"] / 1e9, s["binary"]), "dim"))
    if s["unreadable"]:
        w(c("Could not read %d files or folders (permission denied). Run with sudo to include them,\n"
            "for example /var/lib/quantus or /var/lib/docker. First few: %s"
            % (s["unreadable"], ", ".join(findings.unreadable_examples[:3])), "yellow") + "\n")
    if not recs:
        w("\n Nothing found. See 'Nothing found?' in the README for where else to look.\n")

    n = 0
    rule = c("=" * 100, "blue")
    for key, title, explain in SECTIONS:
        group = [r for r in recs if _section(r) == key]
        if not group:
            continue
        w("\n" + rule + "\n")
        w(" %s\n %s\n" % (c("%s: %d" % (title, len(group)), "bold", "blue"), c(explain, "dim")))
        w(rule + "\n")
        for r in group:
            n += 1
            _print_record(w, c, n, r, checked)

    if treasury:
        w("\n %s\n" % c("Treasury fallback (NOT your addresses)", "bold", "red"))
        w(" The node ran without a rewards address/preimage, so blocks paid the treasury:\n")
        for r in treasury:
            w("   %s  on %s  (%s)\n" % (c(r["address"], "red"), ", ".join(sorted(r["chains"])) or "unknown",
                                        _times(r["count"])))

    if findings.inner_hashes:
        w("\n %s\n" % c("Wormhole inner hashes (shown shortened on purpose)", "bold"))
        w(" Planck / mainnet nodes are given an inner hash instead of an address. The node prints\n"
          " the matching address at start-up as 'Rewards wormhole address', listed above if the\n"
          " log still exists. The full value is in the file shown.\n")
        for value, rec in findings.inner_hashes.items():
            w("   %s  on %s\n" % (_short_hash(value), ", ".join(sorted(rec["chains"])) or "unknown"))
            for line in _sources_text(rec["sources"], 2):
                w("       %s\n" % line)

    w("\n %s\n" % c("These are public addresses. Never paste a seed phrase or secret anywhere to 'check' them.",
                     "yellow"))
    w(" %s %s\n" % (c("Live Quantus data (airdrop progress, hashrate, exchange flows):", "dim"),
                    c(PROJECT_SITE, "cyan")))
    w(" %s %s  %s\n" % (c("quantus-rewards-finder by", "dim"), AUTHOR, c(AUTHOR_X, "cyan")))


def to_json(findings, checked):
    rows = []
    for r in findings.addresses.values():
        row = {k: (sorted(v) if isinstance(v, set) else v) for k, v in r.items()}
        if not checked:
            row.pop("airdrop", None)
        rows.append(row)
    return {
        "tool": "quantus-rewards-finder", "version": __version__,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "stats": findings.stats,
        "addresses": rows,
        "inner_hashes": [{"short": _short_hash(v), "chains": sorted(r["chains"]),
                          "labels": sorted(r["labels"]), "sources": r["sources"]}
                         for v, r in findings.inner_hashes.items()],
    }


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def make_progress(enabled):
    state = {"len": 0}

    def progress(text):
        if not enabled:
            return
        if text is None:
            sys.stderr.write("\r" + " " * state["len"] + "\r")
            state["len"] = 0
        else:
            text = text[:110]
            sys.stderr.write("\r" + text + " " * max(0, state["len"] - len(text)))
            state["len"] = len(text)
        sys.stderr.flush()
    return progress


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Find every Quantus mining rewards address left on this machine.",
        epilog="Nothing is sent anywhere unless you add --check-airdrop, and even then only the "
               "public lists are downloaded; matching happens locally.")
    ap.add_argument("paths", nargs="*", help="extra files or folders to search (e.g. a mounted backup)")
    ap.add_argument("--only", action="store_true",
                    help="search only the given paths, not the default locations")
    ap.add_argument("--no-journal", action="store_true", help="skip the systemd journal")
    ap.add_argument("--no-docker", action="store_true", help="skip Docker container logs")
    ap.add_argument("--no-processes", action="store_true", help="skip running processes")
    ap.add_argument("--check-airdrop", action="store_true",
                    help="download the public airdrop lists and show each address's allocation")
    ap.add_argument("--server", default=AIRDROP_SERVER, help=argparse.SUPPRESS)
    ap.add_argument("--json", metavar="FILE", help="also write the results as JSON ('-' = stdout)")
    ap.add_argument("--quiet", action="store_true", help="no progress output")
    ap.add_argument("--no-color", dest="no_colour", action="store_true",
                    help="plain text output (also: NO_COLOR=1)")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = ap.parse_args(argv)

    progress = make_progress(not args.quiet and sys.stderr.isatty())
    findings = Findings()
    roots = [os.path.abspath(p) for p in args.paths]
    if not args.only:
        roots += default_roots()
    if not args.quiet:
        c = Style(use_colour(sys.stderr, args.no_colour))
        sys.stderr.write("%s %s  -  community tool, not affiliated with the Quantus team\n"
                         % (c("quantus-rewards-finder", "bold"), __version__))
        sys.stderr.write("by %s  %s\n" % (AUTHOR, c(AUTHOR_X, "cyan")))
        sys.stderr.write("%s %s\n" % (c("Searching:", "dim"), ", ".join(roots)))
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            sys.stderr.write("Tip: node logs often live in root-only folders. Re-run with sudo if nothing shows up.\n")

    seen = set()
    for root in roots:
        walk(findings, root, progress, seen)
    if not args.only:
        if not args.no_journal:
            scan_journal(findings, progress)
        if not args.no_docker:
            scan_docker(findings, progress)
        if not args.no_processes:
            scan_processes(findings)

    checked = False
    if args.check_airdrop and findings.addresses:
        try:
            n = check_airdrop(findings, args.server.rstrip("/"))
            checked = True
            if not args.quiet:
                sys.stderr.write("Airdrop list: %d addresses downloaded from %s\n" % (n, args.server))
        except (OSError, ValueError, KeyError) as exc:
            sys.stderr.write("Could not download the airdrop lists (%s). Showing addresses only.\n" % exc)

    if args.json != "-":
        print_report(findings, checked, colour=use_colour(sys.stdout, args.no_colour))
    if args.json:
        text = json.dumps(to_json(findings, checked), indent=1, ensure_ascii=False)
        if args.json == "-":
            print(text)
        else:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        sys.exit(130)
