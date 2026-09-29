#!/usr/bin/env python3
"""The run that actually finds something. Run: python3 tests/test_found.py

Every other test drives a run where nothing matches. This one builds synthetic official lists that
contain addresses really derived from the published BIP39 test vector, so the whole positive path is
exercised end to end: the result table, the amounts and claim statuses from a synthetic claim-server
answer, the claim-risk warning, the encrypted report, and the tip line - which must appear only here,
never in a run that found nothing.

No real seed phrase is involved: the phrase pasted is the all-zero test vector every BIP39 library
ships with, and the addresses come from tests/vectors.json.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from test_end_to_end import ANSI, PASSWORD, fake_address, run  # noqa: E402

spec = importlib.util.spec_from_file_location("quantus_airdrop_checker", ROOT / "quantus_airdrop_checker.py")
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)


def build_cache(cache, real_nodes, lists, amounts, unpaid):
    """A cache of our own: the real (pinned) binaries, but official lists and claim-server answers we control."""
    os.symlink(real_nodes, cache / "nodes")
    (cache / "lists").mkdir()
    meta = {"fetched_utc": "2026-09-16T00:00:00+00:00", "files": {}}
    for filename, miners in lists.items():
        data = json.dumps({"data": {"minerStats": [{"id": a, "totalMinedBlocks": b}
                                                   for a, b in miners.items()]}}).encode()
        (cache / "lists" / filename).write_bytes(data)
        meta["files"][filename] = {"blob_sha": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(),
                                   "bytes": len(data), "url": "test"}
    (cache / "lists" / "lists.json").write_text(json.dumps(meta))
    folder = cache / "airdrop"
    folder.mkdir()
    snapshot = json.dumps({"version": 1, "sha256": "test", "rows": [
        {"address": a, "account": "0x" + qc.account_id(a).hex(), "amount_hundredths": round(q * 100),
         "testnets": nets, "kind": "dilithium"} for a, (q, nets) in amounts.items()]}).encode()
    unpaid_raw = json.dumps({"rows": unpaid}).encode()
    (folder / "snapshot.json").write_bytes(snapshot)
    (folder / "unpaid.json").write_bytes(unpaid_raw)
    (folder / "airdrop.json").write_text(json.dumps({
        "fetched_utc": "2026-09-29T00:00:00+00:00", "server": "test", "snapshot_version": 1,
        "snapshot_sha256_reported": "test", "addresses": len(amounts), "unpaid_rows": len(unpaid),
        "files": {"snapshot.json": hashlib.sha256(snapshot).hexdigest(),
                  "unpaid.json": hashlib.sha256(unpaid_raw).hexdigest()}}))


def main():
    phrase = qc.test_vector(12)
    rows = json.loads((ROOT / "tests" / "vectors.json").read_text())[qc.fingerprint(phrase)]

    def address_of(generation, variant="HD account 0", scheme="standard"):
        return next(a for g, s, v, a in rows if (g, s, v) == (generation, scheme, variant))

    plain_hit = address_of("Dirac early")            # an ordinary match
    gap_hit = address_of("Dirac late")               # the generation the team's own tool misses
    planck_hit = address_of("Planck / mainnet")      # a second testnet for the same phrase

    dirac = {fake_address(f"filler-d{i}".encode()): 100 + i for i in range(10)}
    dirac.update({plain_hit: 5000, gap_hit: 900})
    planck = {fake_address(f"filler-p{i}".encode()): 100 + i for i in range(10)}
    planck.update({planck_hit: 2000})
    lists = {"resonance_network_miners.json": {fake_address(b"r"): 5},
             "schrodinger_miners.json": {fake_address(b"s"): 5},
             "dirac_miners.json": dirac, "planck_miners.json": planck}
    claimer = fake_address(b"claimed-to")
    amounts = {plain_hit: (12.34, ["Dirac"]), gap_hit: (3.21, ["Dirac"]), planck_hit: (6.40, ["Planck"])}
    unpaid = [{"address": plain_hit, "status": "unclaimed"}, {"address": gap_hit, "status": "unclaimed"},
              {"address": planck_hit, "status": "recorded", "claim_account": claimer}]
    unclaimed_total = 12.34 + 3.21

    real_cache = pathlib.Path(os.environ.get("QUANTUS_AIRDROP_CHECKER_CACHE")
                              or pathlib.Path.home() / ".cache" / "quantus-airdrop-checker")
    if not (real_cache / "nodes").is_dir():
        print("no cache - run ./quantus_airdrop_checker.sh --download-only first")
        return 1

    with tempfile.TemporaryDirectory(prefix="quantus-airdrop-checker-found-") as tmp:
        cache = pathlib.Path(tmp) / "cache"
        cache.mkdir()
        build_cache(cache, real_cache / "nodes", lists, amounts, unpaid)
        work = pathlib.Path(tmp) / "work"
        work.mkdir()
        os.environ["QUANTUS_AIRDROP_CHECKER_CACHE"] = str(cache)
        out = run([(2.0, PASSWORD + "\n"),          # 1/5 password
                   (4.5, "\n"),                     # 3/5 no addresses of my own
                   (7.0, " ".join(phrase) + "\n"),  # 4/5 the public test vector
                   (11.0, "\n")],                   #     done
                  work, timeout=180)
        reports = list(work.glob("quantus-airdrop-report-*.md.gpg"))
        decrypted = ""
        if reports:
            gpg = subprocess.run(["gpg", "--batch", "--quiet", "--decrypt", "--passphrase", PASSWORD,
                                  "--pinentry-mode", "loopback", str(reports[0])],
                                 capture_output=True, text=True)
            decrypted = gpg.stdout

    plain = ANSI.sub("", out)
    checks = [
        ("the result says three addresses were found", "3 addresses found on the official lists" in plain, ""),
        ("the plain hit is in the table", plain_hit[:8] in plain, ""),
        ("both testnets are shown", "Dirac" in plain and "Planck" in plain, ""),
        ("the unclaimed total is the server's", f"NOT CLAIMED YET: {unclaimed_total:.2f} QTC" in plain,
         f"expected {unclaimed_total:.2f}"),
        ("the claimed-to address is shown", claimer[:6] in plain, ""),
        ("claim advice with the deadline", "claim what is marked NOT CLAIMED YET" in plain
         and qc.CLAIM_DEADLINE in plain, ""),
        ("the claim-risk warning fired", "does not find these addresses" in plain, ""),
        ("...and it names the right address", gap_hit[:8] in plain.split("does not find")[-1], ""),
        ("nothing is reported as unmatched", "matched nothing" not in plain, ""),
        ("the tip line appears when something was found", "tip is welcome" in plain, ""),
        ("the report was written and decrypts", decrypted.startswith("# Quantus airdrop check"), ""),
        ("the report holds the found addresses", plain_hit in decrypted and gap_hit in decrypted, ""),
        ("no word of the phrase is on screen", "abandon" not in plain.replace("abandoned", ""), ""),
    ]
    print(out[-3000:])
    print("=" * 78)
    bad = 0
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -> {detail}"))
        bad += not ok
    print("PASS" if not bad else "FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
