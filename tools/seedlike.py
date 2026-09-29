#!/usr/bin/env python3
"""Refuse files that contain something shaped like a seed phrase: 12 or more BIP39 words in a row.

Used by tools/scan-secrets.sh (the pre-commit hook). Exit code 1 means "found something".

Two rules matter here:
  - the run is NOT broken by punctuation, numbering or line breaks. People write seed phrases as
    "1. abandon", "abandon, ability, able" or one word per line, and a guard that only catches the
    space-separated shape quietly lets the real file through;
  - nothing from the file is ever printed. A guard that reports "found a seed phrase starting with
    <three real words>" puts part of that phrase on the screen and into CI logs.

Two things are allowed on purpose: the official BIP39 wordlist itself (recognised by its SHA256,
not by its name) and the published all-zero test phrase ("abandon abandon ... about/art") that
every BIP39 test suite uses and that belongs to nobody.
"""
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORDLIST_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
PUBLIC_TEST_PHRASE = {"abandon", "about", "art"}
RUN = 12


def wordlist():
    for path in ROOT.glob("*/bip39-english.txt"):
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() == WORDLIST_SHA256:
            return set(data.decode().split())
    return None


def check(path, words):
    try:
        data = pathlib.Path(path).read_bytes()
    except OSError as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return 1
    if hashlib.sha256(data).hexdigest() == WORDLIST_SHA256:
        return 0
    text = data.decode("utf-8", "replace").lower()
    run, start = [], 0
    for match in re.finditer(r"[a-z]+|$", text):
        token = match.group()
        if token and (token in words if words else 3 <= len(token) <= 8):
            if not run:
                start = match.start()
            run.append(token)
            continue
        if len(run) >= RUN and not set(run) <= PUBLIC_TEST_PHRASE:
            line = text.count("\n", 0, start) + 1
            print(f"REFUSED: {path} line {line}: {len(run)} words in a row from the BIP39 wordlist - "
                  "that is the shape of a seed phrase. Nothing from the file is shown here on purpose.",
                  file=sys.stderr)
            return 1
        run = []
    return 0


if __name__ == "__main__":
    words = wordlist()
    sys.exit(max([check(p, words) for p in sys.argv[1:]] or [0]))
