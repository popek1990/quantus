#!/usr/bin/env python3
"""Are the amounts, the claim statuses and the warnings right? Run: python3 tests/test_match.py

The addresses come from tests/vectors.json (derived from public BIP39 test vectors). The official
miner lists and the claim server's answers are replaced with synthetic ones built here, so every
expected value is known in advance. No binaries are run, nothing touches the network, and no real
phrase is involved.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def fake_address(tag):
    raw = bytes([0x6F, 0x40]) + hashlib.sha256(b"quantus-airdrop-checker-filler-" + tag).digest()
    raw += hashlib.blake2b(b"SS58PRE" + raw, digest_size=64).digest()[:2]
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return out


def build_cache(cache, lists):
    """Write synthetic official lists plus the index the tool checks them against."""
    (cache / "lists").mkdir(parents=True, exist_ok=True)
    meta = {"fetched_utc": "2026-09-16T00:00:00+00:00", "files": {}}
    for filename, miners in lists.items():
        data = json.dumps({"data": {"minerStats": [{"id": a, "totalMinedBlocks": b}
                                                   for a, b in miners.items()]}}).encode()
        (cache / "lists" / filename).write_bytes(data)
        meta["files"][filename] = {"blob_sha": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(),
                                   "bytes": len(data), "url": "test"}
    (cache / "lists" / "lists.json").write_text(json.dumps(meta))
    return meta


def build_airdrop(cache, qc, amounts, unpaid):
    """Synthetic claim-server answers: amounts {address: (qtc, [testnets])}, unpaid [row, ...]."""
    folder = cache / "airdrop"
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = json.dumps({"version": 1, "sha256": "test", "rows": [
        {"address": a, "account": "0x" + qc.account_id(a).hex(), "amount_hundredths": round(q * 100),
         "testnets": nets, "kind": "dilithium"} for a, (q, nets) in amounts.items()]}).encode()
    unpaid_raw = json.dumps({"rows": unpaid}).encode()
    (folder / "snapshot.json").write_bytes(snapshot)
    (folder / "unpaid.json").write_bytes(unpaid_raw)
    meta = {"fetched_utc": "2026-09-29T00:00:00+00:00", "server": "test", "snapshot_version": 1,
            "snapshot_sha256_reported": "test", "addresses": len(amounts), "unpaid_rows": len(unpaid),
            "files": {"snapshot.json": hashlib.sha256(snapshot).hexdigest(),
                      "unpaid.json": hashlib.sha256(unpaid_raw).hexdigest()}}
    (folder / "airdrop.json").write_text(json.dumps(meta))
    return qc.load_airdrop(note=lambda *_a, **_k: None)


def raw_airdrop(qc, snapshot_rows, unpaid_rows):
    """Parse hand-made server answers directly (no cache involved)."""
    return qc.parse_airdrop(json.dumps({"rows": snapshot_rows}).encode(),
                            json.dumps({"rows": unpaid_rows}).encode())


def main():
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name}{(' -> ' + detail) if detail else ''}")

    with tempfile.TemporaryDirectory(prefix="quantus-airdrop-checker-match-") as tmp:
        cache = pathlib.Path(tmp)
        os.environ["QUANTUS_AIRDROP_CHECKER_CACHE"] = str(cache)
        spec = importlib.util.spec_from_file_location("quantus_airdrop_checker", ROOT / "quantus_airdrop_checker.py")
        qc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qc)

        vectors = json.loads((ROOT / "tests" / "vectors.json").read_text())
        fingerprints = sorted(vectors)
        rows_a = vectors[fingerprints[0]]

        def address_of(generation, variant, scheme="standard"):
            for g, s, v, a in rows_a:
                if g == generation and s == scheme and v == variant:
                    return a
            raise KeyError((generation, variant, scheme))

        dirac_hd = address_of("Dirac early", "HD account 0")          # unclaimed
        gap_hd = address_of("Dirac late", "HD account 0")             # unclaimed, and the claim tool misses it
        planck = address_of("Planck / mainnet", "HD account 0")       # claimed, waiting for payout
        paid = address_of("Resonance", "default")                     # already paid out
        mined_only = address_of("Schrodinger", "HD account 0")        # mined, but not in the airdrop
        missing = fake_address(b"known-but-lost")                     # pasted, but no phrase for it
        claimer = fake_address(b"someone-who-claimed")

        def testnet(entries):
            miners = {fake_address(f"{i}".encode()): 100 + i for i in range(10)}
            miners.update(entries)
            return miners

        lists = {
            "resonance_network_miners.json": testnet({paid: 1}),
            "schrodinger_miners.json": testnet({mined_only: 7}),
            "dirac_miners.json": testnet({dirac_hd: 5000, gap_hd: 4000, missing: 3000}),
            "planck_miners.json": testnet({planck: 2000}),
        }
        meta = build_cache(cache, lists)
        amounts = {dirac_hd: (12.34, ["Dirac"]), gap_hd: (5.0, ["Dirac"]), planck: (6.4, ["Planck"]),
                   paid: (0.1, ["Resonance"]), missing: (3.33, ["Dirac"])}
        unpaid = [{"address": dirac_hd, "status": "unclaimed"}, {"address": gap_hd, "status": "unclaimed"},
                  {"address": missing, "status": "unclaimed"},
                  {"address": planck, "status": "recorded", "claim_account": claimer}]
        data = qc.OfficialData(meta, build_airdrop(cache, qc, amounts, unpaid))

        entries = [{"label": "rig-1", "words": ["x"] * 12, "alternative": False, "fingerprint": "aaa"},
                   {"label": None, "words": ["y"] * 12, "alternative": False, "fingerprint": "bbb"}]
        derived = {
            "aaa": [("Dirac early", "v0.4.0-v0.4.4", "standard", "HD account 0", dirac_hd),
                    ("Dirac late", "v0.4.8-v0.4.9", "standard", "HD account 0", gap_hd),
                    ("Planck / mainnet", "v0.4.11-v1.0.1", "standard", "HD account 0", planck),
                    ("Resonance", "v0.1.0-v0.1.6", "standard", "default", paid),
                    ("Schrodinger", "v0.2.9-v0.2.10", "standard", "HD account 0", mined_only),
                    ("Dirac early", "v0.4.0-v0.4.4", "standard", "HD account 1", fake_address(b"nowhere"))],
            "bbb": [("Dirac early", "v0.4.0-v0.4.4", "standard", "HD account 0", fake_address(b"nothing"))],
        }
        rows, silent = qc.build_rows(entries, derived, data)
        found = {r["address"]: r for r in rows}

        check("the unclaimed hit is found", dirac_hd in found)
        check("the amount is the claim server's, not a formula", found[dirac_hd]["qtc"] == 12.34,
              str(found[dirac_hd]["qtc"]))
        check("unclaimed is recognised", found[dirac_hd]["status"] == qc.UNCLAIMED)
        check("claimed-and-waiting is recognised, with the claiming address",
              found[planck]["status"] == qc.WAITING and found[planck]["claim_account"] == claimer)
        check("a row gone from /unpaid is paid out", found[paid]["status"] == qc.PAID)
        check("mined but not in the airdrop", found[mined_only]["status"] == qc.NOT_IN_AIRDROP
              and found[mined_only]["qtc"] == 0.0)
        check("blocks come from the miner lists", found[dirac_hd]["blocks"] == 5000
              and found[dirac_hd]["mined"][0][2] == 1)
        check("unclaimed rows come first", rows[0]["status"] == qc.UNCLAIMED, rows[0]["status"])
        check("a phrase that matches nothing is listed", [s_[2] for s_ in silent] == ["bbb"])
        check("an address that is on no list is not invented", len(rows) == 5, f"{len(rows)} rows")
        check("the claim-gap warning picks the right row",
              [r["address"] for r in qc.claim_gap_rows(rows)] == [gap_hd])

        lost = qc.missing_addresses([missing, dirac_hd], rows, data)
        check("an address with no phrase is reported", [a for a, _i in lost] == [missing])
        check("and it says what that address holds",
              lost[0][1]["qtc"] == 3.33 and lost[0][1]["status"] == qc.UNCLAIMED)

        screen = io.StringIO()
        with contextlib.redirect_stdout(screen):
            qc.print_report(rows, silent, entries, [missing], data, lost)
        text = screen.getvalue()
        check("the unclaimed total is printed", "NOT CLAIMED YET: 17.34 QTC" in text)
        check("the deadline is printed", qc.CLAIM_DEADLINE in text)
        check("the claimed-to address is shown", claimer[:6] in text and "someone else has this seed" in text)
        check("the claim-gap warning is printed", "does not find these addresses" in text)
        check("the missing address is printed", "no phrase reproduced" in text)
        check("no old formula text", "estimate" not in text.lower() and "2500" not in text)

        advice = qc.next_steps(rows)
        check("advice says how to claim", "official Quantus" in advice and qc.CLAIM_DEADLINE in advice)
        check("advice explains waiting", "waiting for payout" in advice)

        # the server answered, but the unpaid list is empty: that proves nothing
        empty = qc.OfficialData(meta, build_airdrop(cache, qc, amounts, []))
        check("an empty unpaid list gives 'unknown', not 'paid out'",
              empty.lookup(dirac_hd)["status"] == qc.UNKNOWN)
        screen = io.StringIO()
        with contextlib.redirect_stdout(screen):
            qc.print_report(qc.build_rows(entries, derived, empty)[0], [], entries, [], empty, [])
        check("unknown status never says 'nothing left to claim'",
              "nothing left to claim" not in screen.getvalue() and "claim status unknown" in screen.getvalue())

        # the two server lists disagree (an unpaid row that is not on the airdrop list): no status at all
        stranger = fake_address(b"not-on-the-list")
        odd = qc.OfficialData(meta, build_airdrop(cache, qc, amounts, unpaid + [{"address": stranger, "status": "unclaimed"}]))
        check("lists that do not fit together give 'unknown', never 'paid out'",
              odd.lookup(paid)["status"] == qc.UNKNOWN and odd.lookup(dirac_hd)["status"] == qc.UNKNOWN)

        # hostile or broken values from the server
        acc = "0x" + qc.account_id(dirac_hd).hex()
        for bad_amount in (10 ** 400, -5, True, 1.5, "12"):
            parsed = raw_airdrop(qc, [{"address": dirac_hd, "account": acc, "amount_hundredths": bad_amount,
                                       "testnets": ["Dirac"]}], [{"address": dirac_hd, "status": "unclaimed"}])
            check(f"amount {str(bad_amount)[:12]!r} is treated as unknown", parsed["rows"][0]["qtc"] is None)
        try:
            qc.parse_airdrop(b'{"rows": [{"account": "0x00", "amount_hundredths": NaN}]}', b'{"rows": []}')
            check("NaN in the answer is refused", False)
        except ValueError:
            pass
        parsed = raw_airdrop(qc, [{"address": dirac_hd, "account": acc, "amount_hundredths": 100,
                                   "testnets": ["Dirac", "\x1b]0;evil\x07", 7]}],
                             [{"address": dirac_hd, "status": "recorded", "claim_account": "\x1b[2Jqz-not-an-address"},
                              {"address": ["list"], "status": "unclaimed"}])
        check("unknown testnet names and escape sequences are dropped", parsed["rows"][0]["testnets"] == ["Dirac"])
        check("a claiming address that is not a valid address is not shown",
              parsed["open"][dirac_hd]["claim_account"] is None)
        check("a non-text address makes the status unusable, not a crash", parsed["status_ok"] is False)
        parsed = raw_airdrop(qc, [{"address": dirac_hd, "account": acc.replace("0x", ""), "amount_hundredths": 1}],
                             [{"address": dirac_hd, "status": "unclaimed"}])
        check("an account id without 0x still matches", parsed["rows"][0]["account"] == acc[2:])

        # no claim server at all: amounts are unknown, never guessed
        # a damaged cache index is reported, not a traceback
        (cache / "airdrop" / "airdrop.json").write_text("{not json")
        check("an unreadable cached airdrop list gives None", qc.load_airdrop(note=lambda *_a, **_k: None) is None)

        blind = qc.OfficialData(meta, None)
        info = blind.lookup(dirac_hd)
        check("without the claim server the amount is unknown", info["qtc"] is None and info["status"] == qc.UNKNOWN)
        screen = io.StringIO()
        with contextlib.redirect_stdout(screen):
            qc.print_report(qc.build_rows(entries, derived, blind)[0], [], entries, [], blind, [])
        check("and the report says so", "claim server could not be used" in screen.getvalue())

        # a typo in the tip address would send someone's thank-you nowhere
        check("the tip address is a real network-189 address", qc.valid_address(qc.TIP_ADDRESS))
        check("the tip is only offered after a run that found something",
              "found_something" in (ROOT / "quantus_airdrop_checker.py").read_text())
        check("no GitHub author link", "github.com/popek1990" not in (ROOT / "quantus_airdrop_checker.py").read_text())

    for f in failures:
        print("FAIL: " + f)
    print(f"{len(failures)} failure(s)")
    print("PASS" if not failures else "FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
