#!/usr/bin/env python3
"""The whole tool, driven in a pseudo-terminal. Run: python3 tests/test_end_to_end.py

Two things are checked, and the first one is the reason this file exists:

  1. no word of a pasted phrase ever reaches the screen, the report, or the encrypted file - measured
     against a baseline run that walks the same path with DIFFERENT phrases, so the tool's own wording
     cannot mask a leak. The two runs share no words at all, by construction;
  2. the run works end to end: the password is starred out, public addresses ARE echoed back, a phrase
     pasted at the address step by mistake is hidden, phrases are recognised in every shape, addresses
     are derived from all eight generations, the report is printed and the encrypted file decrypts.

Phrases are generated here from fixed entropy, so no phrase is stored in this repository.
"""
import hashlib
import os
import pathlib
import pty
import re
import select
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORDS = (ROOT / "bip39-english.txt").read_text().split()
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PASSWORD = "Tajne-Haslo-2026"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def phrase(entropy):
    """entropy bytes -> a valid BIP39 phrase (the standard construction, checksum included)."""
    bits = "".join(format(b, "08b") for b in entropy)
    bits += format(hashlib.sha256(entropy).digest()[0], "08b")[:len(entropy) * 8 // 32]
    return [WORDS[int(bits[i:i + 11], 2)] for i in range(0, len(bits), 11)]


def fake_address(tag):
    """A valid, checksummed network-189 address that belongs to nobody (built from a hash)."""
    raw = bytes([0x6F, 0x40]) + hashlib.sha256(b"quantus-airdrop-checker-test-" + tag).digest()
    raw += hashlib.blake2b(b"SS58PRE" + raw, digest_size=64).digest()[:2]
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return out


def run(feed, workdir, timeout=300):
    """Run the tool in a pty, sending each (delay, text) in turn. Returns everything it printed."""
    pid, fd = pty.fork()
    if pid == 0:  # child: the tool itself, with the pty as its controlling terminal
        os.chdir(workdir)
        env = dict(os.environ, TERM="xterm-256color", COLUMNS="110", LINES="40")
        os.execve("/bin/bash", ["bash", str(ROOT / "quantus_airdrop_checker.sh"), "--offline"], env)
    out = bytearray()
    started, sent = time.time(), 0
    while time.time() - started < timeout:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if sent < len(feed) and time.time() - started > feed[sent][0]:
            os.write(fd, feed[sent][1].encode())
            sent += 1
    os.close(fd)
    os.waitpid(pid, 0)
    return out.decode("utf-8", "replace")


def script(tag):
    """One complete run, with phrases derived from `tag`. Two of these differ only in the secrets."""
    p = [phrase(hashlib.sha256(f"quantus-airdrop-checker-{tag}-{i}".encode()).digest()[:16]) for i in range(5)]
    long_phrase = phrase(hashlib.sha256(f"quantus-airdrop-checker-{tag}-long".encode()).digest())   # 24 words
    # 13 words that are not a valid phrase. The two extra words depend on the tag, so two runs of this
    # script share no words at all - otherwise the leak check below would be blind to the shared ones.
    filler = [w for w in phrase(hashlib.sha256(f"filler-{tag}".encode()).digest()[:16]) if w not in p[0]]
    broken = p[0][:11] + filler[:2]
    address = fake_address(tag.encode())
    esc = "\x1b[31m"

    steps = [
        (2.0,  PASSWORD + "\n"),                                   # 1/5: the password, typed once
        (5.0,  f"{address}\n" + " ".join(p[4]) + "\n"),            # 3/5: an address, plus a phrase
                                                                   #      pasted here by mistake
        (9.0,  f"rig-1 | {' '.join(p[0])}\n"),                     # 4/5: label before the phrase
        (12.0, f"{' '.join(p[1])} | basement nas\n"),              #      label after the phrase
        (15.0, "# a comment\n" + " ".join(p[2]) + "\n"),           #      comment line + bare phrase
        (18.0, " ".join(p[2]) + "\n"),                             #      the same phrase again
        (21.0, "\n".join(long_phrase[:12]) + "\n" + "\n".join(long_phrase[12:]) + "\n"),   # a grid
        (24.0, f"{esc}danger{esc} a very long label with " + " ".join(p[0][:5]) + " | "
               + " ".join(p[3]) + "\n"),                           #      a label holding phrase words
        (27.0, " ".join(broken) + "\n"),                           #      a phrase with a typo
        (30.0, "\n"),                                              #      take what you have
        (33.0, "\n"),                                              #      done: start deriving
    ]
    secret = sorted({w for group in (p[0], p[1], p[2], p[3], p[4], long_phrase, broken) for w in group})
    return steps, secret, address


def main():
    steps, secret, address = script("test")
    for attempt in range(50):
        other_steps, other_secret, _other = script(f"baseline-{attempt}")
        if not (set(secret) & set(other_secret)):
            break
    else:
        print("could not generate disjoint baseline phrases")
        return 1

    def read_report(work):
        """Decrypt the one report a run wrote, then delete it so the next run starts clean."""
        reports = list(pathlib.Path(work).glob("quantus-airdrop-report-*.md.gpg"))
        text = ""
        if reports:
            gpg = subprocess.run(["gpg", "--batch", "--quiet", "--decrypt", "--passphrase", PASSWORD,
                                  "--pinentry-mode", "loopback", str(reports[0])],
                                 capture_output=True, text=True)
            text = gpg.stdout
        for report in reports:
            report.unlink()
        return reports, text

    with tempfile.TemporaryDirectory(prefix="quantus-airdrop-checker-test-") as work:
        out = run(steps, work)
        reports, decrypted = read_report(work)
        baseline = run(other_steps, work)
        _baseline_reports, decrypted_baseline = read_report(work)

    # A pasted word "leaks" when it shows up MORE often than in a run that pasted entirely different
    # phrases: the tool's own fixed text (e.g. "check one by hand") uses wordlist words too.
    plain, plain_baseline = ANSI.sub("", out), ANSI.sub("", baseline)
    leaked = [w for w in secret
              if len(re.findall(rf"\b{w}\b", plain)) > len(re.findall(rf"\b{w}\b", plain_baseline))]
    leaked_file = [w for w in secret
                   if len(re.findall(rf"\b{w}\b", decrypted)) > len(re.findall(rf"\b{w}\b", decrypted_baseline))]
    derived = len(re.findall(r"^\| \d+\w? \|", decrypted, re.M))
    generation_lines = len(re.findall(r"\[\d/8\][^\n]*addresses", plain))

    checks = [
        ("no pasted word reached the screen", not leaked, f"leaked: {leaked[:10]}"),
        ("no pasted word reached the report file", not leaked_file, f"leaked: {leaked_file[:10]}"),
        ("the password itself is never shown", PASSWORD not in plain, ""),
        ("the password is echoed as stars", "*" * len(PASSWORD) in plain, "one star per character"),
        ("a public address IS echoed back", address in plain, "addresses are not secret"),
        ("a phrase pasted at the address step is hidden",
         "looks like a seed phrase - not shown" in plain, ""),
        ("and it says a phrase was discarded there", "contained a seed phrase" in plain, ""),
        ("5 distinct phrases recognised", len(re.findall(r"phrase\s+\d+\s+\w{6}\s", plain)) == 5,
         f"{len(re.findall(r'phrase[ ]+[0-9]+[ ]+[0-9a-f]{6}', plain))} 'phrase N' lines"),
        ("the repeated phrase was caught", "already had this one" in plain, ""),
        ("labels shown", '"rig-1"' in plain and '"basement nas"' in plain, ""),
        ("a label full of wordlist words is refused",
         "label(s) looked like part of a phrase" in plain, ""),
        ("a pasted escape sequence never survives", "\x1b[31mdanger" not in out, ""),
        ("the 13-word run is reported, not used", "run of 13 words is not a valid phrase" in plain, ""),
        ("the known address was accepted", "1 address accepted" in plain, ""),
        ("all 8 generations ran", generation_lines == 8, f"{generation_lines} generation lines"),
        ("a result was printed",
         "RESULT" in plain and re.search(r"\d+ seed phrases? checked", plain) is not None, ""),
        ("test phrases match nothing, and it says so", "matched nothing" in plain, ""),
        ("the pasted address is reported as having no phrase", "no phrase reproduced" in plain, ""),
        ("an encrypted report was written", bool(reports), "no quantus-airdrop-report-*.md.gpg"),
        ("the report decrypts", decrypted.startswith("# Quantus airdrop check"), ""),
        ("the report lists every derived address", derived >= 5 * 70, f"only {derived} rows"),
        ("closing advice printed", "What now" in plain and "keep every seed phrase" in plain, ""),
        ("no claim advice when nothing was found", "NOT CLAIMED YET" not in plain.split("What now")[-1], ""),
        ("no tip is asked for when nothing was found", "tip is welcome" not in plain,
         "the tip line must only appear after a run that found something"),
    ]
    print(out[-4500:])
    print("=" * 78)
    bad = 0
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -> {detail}"))
        bad += not ok
    print(f"\n{len(secret)} distinct words in the test phrases, {derived} addresses derived")
    print("PASS" if not bad else "FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
