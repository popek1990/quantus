#!/usr/bin/env python3
"""Can the parser lose a phrase? Run: python3 tests/test_parser.py

Every case here is a way someone actually writes a seed phrase down. The rule under test is the one
the tool promises: a valid phrase that was pasted must come out, in whatever shape it went in.
Phrases are generated from fixed entropy, so none is stored in this repository.
"""
import hashlib
import importlib.util
import pathlib
import random
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("quantus_airdrop_checker", ROOT / "quantus_airdrop_checker.py")
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)
WORDS = (ROOT / "bip39-english.txt").read_text().split()
rnd = random.Random(20260916)


def phrase(words=12):
    entropy = bytes(rnd.getrandbits(8) for _ in range(words * 11 * 32 // 33 // 8))
    bits = "".join(format(b, "08b") for b in entropy)
    bits += format(hashlib.sha256(entropy).digest()[0], "08b")[:len(entropy) * 8 // 32]
    return [WORDS[int(bits[i:i + 11], 2)] for i in range(0, len(bits), 11)]


def read(text):
    """What the tool makes of a paste: (phrases it believes, every phrase it will check)."""
    entries, unrecognised, _refused = qc.parse_paste(text)
    main = [e["words"] for e in entries if not e["alternative"]]
    every = [e["words"] for e in entries]
    return main, every, unrecognised


def grid(words, per_line):
    return "\n".join(" ".join(words[i:i + per_line]) for i in range(0, len(words), per_line))


def main():
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name}{(' -> ' + detail) if detail else ''}")

    for n in (12, 15, 18, 21, 24):                                    # every valid length, on its own
        for _ in range(20):
            p = phrase(n)
            main_, _every, unrec = read(" ".join(p))
            check(f"{n} words on one line", main_ == [p] and not unrec)

    for _ in range(40):                                               # written as a grid or a list
        p = phrase(24)
        for per_line in (1, 2, 3, 4, 6, 12):
            main_, _e, _u = read(grid(p, per_line))
            check(f"24 words, {per_line} per line", main_ == [p])
        numbered = "\n".join(f"{i + 1}. {w}" for i, w in enumerate(p))
        check("numbered, one per line", read(numbered)[0] == [p])

    for _ in range(40):                                               # several phrases in one paste
        ps = [phrase(rnd.choice((12, 24))) for _ in range(rnd.randint(2, 4))]
        for joiner in (" ", "\n", "\n\n", "\n# next one\n", "\nrig-2\n"):
            main_, _e, _u = read(joiner.join(" ".join(p) for p in ps))
            check(f"{len(ps)} phrases joined by {joiner!r}", main_ == ps, f"got {len(main_)}")

    for _ in range(40):                                               # labels, comments, notes
        p = phrase(12)
        for text in (f"rig-7 | {' '.join(p)}", f"{' '.join(p)} | rig-7",
                     "# the box in the basement\n" + " ".join(p),
                     "# " + " ".join(p)):
            check(f"labelled: {text[:18]!r}", read(text)[0] == [p])
        # a note made of wordlist words ("old", "laptop", "seed", "keep") sits in the same run as the
        # phrase, so where the phrase starts is genuinely ambiguous: it need not be the FIRST reading,
        # but it must be one of the readings that gets checked
        _main, every, _u = read("my old laptop seed keep safe\n" + " ".join(p))
        check("a note full of wordlist words", p in every, "the phrase was not among the readings")

    for _ in range(60):                                               # stray wordlist words touching it
        p = phrase(12)
        for count in (1, 2, 3):
            stray = [rnd.choice(WORDS) for _ in range(count)]
            for text in (" ".join(stray + p), " ".join(p + stray)):
                _main, every, _u = read(text)
                check(f"{count} stray wordlist word(s)", p in every, "the phrase was not among the readings")

    for _ in range(60):                                               # a typo must never become a phrase
        p = phrase(12)
        broken = p[:11] + [rnd.choice([w for w in WORDS if w not in p])]
        main_, every, unrec = read(" ".join(broken))
        if qc.bip39_valid(broken):
            continue                                                  # 1 in 16: the typo is itself valid
        check("a phrase with a typo", not every and unrec == [(12, 0, 0)], f"main={len(main_)} unrec={unrec}")

    for _ in range(20):                                               # junk must not be BELIEVED
        junk = [rnd.choice(WORDS) for _ in range(300)]
        main_, _every, unrec = read(" ".join(junk))
        check("300 random wordlist words are not read as phrases", not main_,
              f"presented {len(main_)} phrases as real")
        check("and the user is told about that run", bool(unrec))

    for _ in range(20):                                               # a phrase buried in notes
        p = phrase(12)
        noise = [rnd.choice(WORDS) for _ in range(30)]
        text = " ".join(noise[:15] + p + noise[15:])
        _main, every, _u = read(text)
        check("a phrase buried in 30 stray wordlist words", p in every, "the phrase was lost")

    for _ in range(10):                                               # many phrases, each with a note
        ps = [phrase(12) for _ in range(12)]
        text = "\n".join(" ".join([rnd.choice(WORDS), rnd.choice(WORDS)] + p) for p in ps)
        _main, every, _u = read(text)
        lost = [p for p in ps if p not in every]
        check("12 phrases each preceded by two wordlist words", not lost, f"{len(lost)} lost")

    phrases = [phrase(12) for _ in range(1500)]                       # a hostile paste must not crash
    started = time.time()
    try:
        _main, every, _u = read(" ".join(" ".join(p) for p in phrases))
        missing = [p for p in phrases if p not in every]
        ok = not missing
    except RecursionError:
        ok, missing = False, ["RecursionError"]
    check("1500 phrases in one run", ok, f"{len(missing)} phrase(s) lost")
    elapsed = time.time() - started

    print(f"1500 chained phrases parsed in {elapsed:.1f}s")
    for f in failures[:20]:
        print("FAIL: " + f)
    print(f"{len(failures)} failure(s)")
    print("PASS" if not failures else "FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
