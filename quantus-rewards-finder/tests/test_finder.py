"""Offline tests. Run:  python3 -m unittest discover -s tests"""

import contextlib
import gzip
import http.server
import io
import json
import os
import pathlib
import shutil
import stat
import time
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import quantus_rewards_finder as qrf  # noqa: E402

# Addresses printed by the official quantus-node binaries for the public BIP39 test phrase.
# They check our SS58 checksum and network-prefix handling against an independent source.
OFFICIAL_VECTORS = [
    "qzpmP4oA6NasjJrPDYQegoA6EEyPnbeUYsjTJ1veQducwTKaZ",
    "qznszp3e27LVTtNc8WaTjuhGFvzhTkcRaokqhMKT889pScVJ4",
    "qzmoS6MpATiXgbRTsHgxQ39LLivQ8PLXV4RE5BdQG9t4Ufkfd",
]


def acc(n):
    """Deterministic fake account id number n."""
    return bytes([n]) * 32


def addr(n):
    return qrf.account_to_ss58(acc(n))


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = qrf.main(list(args))
    return code, out.getvalue()


def found(tmp, *extra):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        qrf.main(["--only", "--quiet", "--json", "-", *extra, str(tmp)])
    data = json.loads(out.getvalue())
    return {r["address"]: r for r in data["addresses"]}, data


class SS58(unittest.TestCase):
    def test_official_vectors_round_trip(self):
        for a in OFFICIAL_VECTORS:
            self.assertEqual(qrf.account_to_ss58(qrf.ss58_to_account(a)), a)

    def test_rejects_bad_checksum(self):
        a = OFFICIAL_VECTORS[0]
        broken = a[:-1] + ("a" if a[-1] != "a" else "b")
        with self.assertRaises(ValueError):
            qrf.ss58_to_account(broken)

    def test_rejects_other_networks(self):
        with self.assertRaises(ValueError):  # a Polkadot address
            qrf.ss58_to_account("15oF4uVJwmo4TdGW7VfQxNLavjCXviqxT9S1MgbjMNHr6Sp5")

    def test_prefix_starts_with_qz(self):
        self.assertTrue(addr(7).startswith("qz"))


class Logs(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, text, mode="w"):
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "wb":
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8")
        return p

    def test_old_node_log_hex_address_chain_and_time(self):
        self.write("node.log",
                   "2025-11-21 14:25:42 ✌️  version 0.4.2-abc\n"
                   "2025-11-21 14:25:42 \U0001f4cb Chain specification: Quantus Dirac Testnet\n"
                   "2025-11-21 14:25:43 ⛏️Using provided rewards address: %s (qzXXXX...)\n"
                   "2026-03-10 22:39:36 ⛏️Using provided rewards address: %s (qzXXXX...)\n"
                   % (acc(1).hex(), acc(1).hex()))
        res, _ = found(self.tmp)
        r = res[addr(1)]
        self.assertEqual(r["chains"], ["Quantus Dirac Testnet"])
        self.assertEqual(r["versions"], ["0.4.2-abc"])
        self.assertEqual(r["first_seen"], "2025-11-21 14:25:43")
        self.assertEqual(r["last_seen"], "2026-03-10 22:39:36")
        self.assertEqual(r["count"], 2)

    def test_chain_switch_in_one_log(self):
        self.write("node.log",
                   "Chain specification: Schrodinger\n"
                   "Using provided rewards address: %s\n"
                   "Chain specification: Planck\n"
                   "Rewards wormhole address: %s\n" % (addr(2), addr(3)))
        res, _ = found(self.tmp)
        self.assertEqual(res[addr(2)]["chains"], ["Schrodinger"])
        self.assertEqual(res[addr(3)]["chains"], ["Planck"])

    def test_chain_from_base_path(self):
        self.write("data/chains/dirac/node.log", "Using address for rewards: 0x%s\n" % acc(4).hex())
        res, _ = found(self.tmp)
        self.assertEqual(res[addr(4)]["chains"], ["dirac"])

    def test_treasury_is_reported_separately(self):
        self.write("node.log", "Using treasury address for rewards: %s\n" % addr(5))
        _, out = run("--only", "--quiet", str(self.tmp))
        self.assertIn("Nothing found", out)
        self.assertIn("NOT your addresses", out)
        self.assertIn(addr(5), out)

    def test_sections_order_by_strength_of_evidence(self):
        self.write("node.log", "Rewards wormhole address: %s\n" % addr(24))
        self.write("start.sh", "--rewards-address %s\n" % addr(25))
        self.write("keys.txt", "Address: %s\n" % addr(26))
        _, out = run("--only", "--quiet", str(self.tmp))
        a, b, c = out.index(addr(24)), out.index(addr(25)), out.index(addr(26))
        self.assertLess(out.index("Addresses a node mined to"), a)
        self.assertLess(a, out.index("Addresses set as the rewards address"))
        self.assertLess(out.index("Addresses set as the rewards address"), b)
        self.assertLess(b, out.index("Other Quantus addresses"))
        self.assertLess(out.index("Other Quantus addresses"), c)

    def test_scripts_history_and_config(self):
        self.write("start.sh", "quantus-node --validator --rewards-address %s --chain dirac\n" % addr(6))
        self.write(".bash_history", "./quantus-node --rewards-address=%s\n" % addr(7))
        self.write("mining.conf", 'REWARDS_ADDRESS="%s"\n' % addr(8))
        self.write("compose.yml", "environment:\n  REWARDS_ADDRESS: %s\n" % addr(9))
        res, _ = found(self.tmp)
        for n in (6, 7, 8, 9):
            self.assertIn(addr(n), res)

    def test_invalid_address_is_ignored(self):
        bad = addr(10)[:-1] + ("a" if addr(10)[-1] != "a" else "b")
        self.write("start.sh", "--rewards-address %s\n" % bad)
        res, _ = found(self.tmp)
        self.assertEqual(res, {})

    def test_key_generation_output(self):
        self.write("keys.txt", "Address: %s\nInner Hash: 0x%s\nWormhole Address: %s\n"
                   % (addr(11), "ab" * 32, addr(12)))
        res, data = found(self.tmp)
        self.assertIn(addr(11), res)
        self.assertIn(addr(12), res)
        self.assertEqual(len(data["inner_hashes"]), 1)

    def test_inner_hash_is_never_printed_in_full(self):
        secretish = "cd" * 32
        self.write("run.sh", "quantus-node --rewards-inner-hash 0x%s\n" % secretish)
        _, out = run("--only", "--quiet", str(self.tmp))
        self.assertIn("0xcdcdcd…cdcd", out)
        self.assertNotIn(secretish, out)

    def test_seed_phrase_in_a_file_is_never_printed(self):
        phrase = "abandon " * 23 + "art"
        self.write("backup.txt", "Secret phrase: %s\nAddress: %s\n" % (phrase, addr(13)))
        _, out = run("--only", "--quiet", str(self.tmp))
        self.assertIn(addr(13), out)
        self.assertNotIn("abandon", out)

    def test_rotated_gzip_log(self):
        self.write("node.log.1.gz",
                   gzip.compress(("Rewards wormhole address: %s\n" % addr(14)).encode()), mode="wb")
        res, _ = found(self.tmp)
        self.assertIn(addr(14), res)

    def test_binary_files_are_skipped(self):
        self.write("quantus-node", b"\x7fELF\0\0Using provided rewards address: " + addr(15).encode(),
                   mode="wb")
        res, data = found(self.tmp)
        self.assertEqual(res, {})
        self.assertEqual(data["stats"]["binary"], 1)

    def test_line_split_across_read_chunks(self):
        old = qrf.CHUNK
        qrf.CHUNK = 64
        try:
            self.write("node.log", "x" * 9000 + "\n" + "2026-01-01 00:00:00 Rewards wormhole address: %s\n"
                       % addr(16) + "y" * 9000 + "\n")
            res, _ = found(self.tmp)
        finally:
            qrf.CHUNK = old
        self.assertIn(addr(16), res)

    def test_log_embedded_in_json_with_escaped_newlines(self):
        blob = json.dumps({"output": "2026-03-03 12:01:15 Chain specification: Dirac\n"
                                     "2026-03-03 12:01:15 Using provided rewards address: %s (qz...)\n"
                                     % acc(17).hex()})
        self.write("session.json", blob)
        res, _ = found(self.tmp)
        self.assertEqual(res[addr(17)]["chains"], ["Dirac"])
        self.assertEqual(res[addr(17)]["first_seen"], "2026-03-03 12:01:15")

    def test_wallet_file_reads_only_the_address(self):
        self.write(".quantus/wallets/main.json", json.dumps({
            "name": "main", "address": addr(18), "encrypted_data": [1, 2, 3],
            "argon2_salt": [9, 9], "created_at": "2025-10-01T00:00:00Z"}))
        res, _ = found(self.tmp)
        self.assertIn("quantus-cli wallet 'main'", res[addr(18)]["labels"])
        _, out = run("--only", "--quiet", str(self.tmp))
        self.assertNotIn("argon2", out)

    def test_database_folders_are_skipped(self):
        self.write("chains/dirac/db/full/000123.log", "Rewards wormhole address: %s\n" % addr(19))
        res, _ = found(self.tmp)
        self.assertEqual(res, {})


class Robustness(unittest.TestCase):
    """Cases found in review: formats that were missed, and files that used to stop the scan."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, data):
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.write_bytes if isinstance(data, bytes) else p.write_text)(data)
        return p

    def test_other_ways_to_write_the_setting(self):
        self.write("config.v2.json", '{"Args":["--validator","--rewards-address","%s"]}' % addr(40))
        self.write("compose.yml", "command:\n  - --validator\n  - --rewards-address\n  - %s\n" % addr(41))
        self.write("run.sh", "quantus-node --validator \\\n  --rewards-address \\\n  %s\n" % addr(42))
        self.write("miner.env", "MINER_REWARDS_ADDRESS=%s\n" % addr(43))
        self.write("config.toml", 'rewards_address = "%s"\n' % addr(44))
        res, _ = found(self.tmp)
        for n in (40, 41, 42, 43, 44):
            self.assertIn(addr(n), res, n)

    def test_address_glued_to_following_letters(self):
        self.write("node.log", "Rewards wormhole address: %sTue\n--rewards-address %sabc\n" % (addr(45), addr(46)))
        res, _ = found(self.tmp)
        self.assertIn(addr(45), res)
        self.assertIn(addr(46), res)

    def test_broken_files_do_not_stop_the_scan(self):
        good = gzip.compress(("Rewards wormhole address: %s\n" % addr(47)).encode() * 50)
        self.write("broken.log.gz", good[:40] + b"\x00garbage" + good[60:])
        self.write(".quantus/wallets/deep.json", "[" * 200000)
        self.write("plain-but-named.gz", "Rewards wormhole address: %s\n" % addr(48))
        self.write("zz-last.log", "Rewards wormhole address: %s\n" % addr(49))
        res, _ = found(self.tmp)
        self.assertIn(addr(48), res)  # compression is recognised by content, not by name
        self.assertIn(addr(49), res)  # the scan went on after the broken files

    def test_non_utf8_file_name(self):
        p = self.tmp / os.fsdecode(b"bad\xff.log")
        p.write_text("Rewards wormhole address: %s\n" % addr(50))
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        f = qrf.Findings()
        qrf.walk(f, str(self.tmp), lambda *_: None, set())
        qrf.print_report(f, False, out=out)
        self.assertEqual(len(f.addresses), 1)
        _, data = found(self.tmp)
        self.assertEqual(len(data["addresses"]), 1)

    def test_control_characters_from_files_are_not_printed(self):
        self.write("node.log", "Chain specification: \x1b]0;owned\x07\x1b[2J\nRewards wormhole address: %s\n" % addr(51))
        _, out = run("--only", "--quiet", str(self.tmp))
        self.assertIn(addr(51), out)
        self.assertNotIn("\x1b", out)

    def test_wallet_date_is_validated(self):
        self.write(".quantus/wallets/a.json", json.dumps({"address": addr(52), "created_at": "x" * 100000}))
        self.write(".quantus/wallets/b.json", json.dumps({"address": addr(53), "created_at": "2025-10-01T08:00:00Z"}))
        res, _ = found(self.tmp)
        self.assertIsNone(res[addr(52)]["first_seen"])
        self.assertEqual(res[addr(53)]["first_seen"], "2025-10-01 08:00:00")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_fifo_does_not_hang(self):
        os.mkfifo(str(self.tmp / "pipe.log"))
        f = qrf.Findings()
        started = time.monotonic()
        qrf.scan_file(f, str(self.tmp / "pipe.log"), lambda *_: None)
        self.assertLess(time.monotonic() - started, 2)

    def test_one_huge_line_is_fast_and_complete(self):
        old = qrf.MAX_CARRY
        qrf.MAX_CARRY = 4096
        try:
            body = "rewards " * 400000 + "Rewards wormhole address: %s " % addr(54) + "rewards " * 1000
            self.write("minified.json", body)
            started = time.monotonic()
            res, _ = found(self.tmp)
        finally:
            qrf.MAX_CARRY = old
        self.assertIn(addr(54), res)
        self.assertLess(time.monotonic() - started, 10)

    def test_journal_keeps_chain_per_service(self):
        f = qrf.Findings()
        sc = qrf.StreamScanner(f, "journald (system)", keyed=True)
        sc.feed(("2026-01-01T00:00:00+0000 host node-a[1]: Chain specification: Planck\n"
                 "2026-01-01T00:00:01+0000 host node-b[2]: Chain specification: Quantus Mainnet\n"
                 "2026-01-01T00:00:02+0000 host node-a[1]: Rewards wormhole address: %s\n" % addr(55)).encode(),
                final=True)
        self.assertEqual(f.addresses[acc(55).hex()]["chains"], {"Planck"})

    def test_missing_path_and_bad_json_target_are_reported(self):
        _, out = run("--only", "--quiet", str(self.tmp / "nope"))
        self.assertIn("Path not found", out)
        link = self.tmp / "out.json"
        os.symlink(str(self.tmp / "elsewhere"), str(link))
        with self.assertRaises(SystemExit):
            run("--only", "--quiet", "--json", str(link), str(self.tmp))

    def test_docker_logs_on_stderr_are_read(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake = bindir / "docker"
        fake.write_text("#!/bin/sh\n"
                        'if [ "$1" = ps ]; then printf "abc123\\tquantus-node\\tquantus/node:1\\n"; exit 0; fi\n'
                        'echo "Rewards wormhole address: %s" >&2\n' % addr(56))
        fake.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = str(bindir) + os.pathsep + old
        try:
            f = qrf.Findings()
            qrf.scan_docker(f, lambda *_: None)
        finally:
            os.environ["PATH"] = old
        self.assertIn(acc(56).hex(), f.addresses)


class Output(unittest.TestCase):
    def setUp(self):
        self.f = qrf.Findings()
        self.f.add_address("mined", "node log: rewards address", acc(30), "/x/node.log", "Dirac", None, None)

    def test_plain_text_when_not_a_terminal(self):
        out = io.StringIO()
        qrf.print_report(self.f, False, out=out)
        self.assertNotIn("\033[", out.getvalue())

    def test_colour_only_when_asked(self):
        out = io.StringIO()
        qrf.print_report(self.f, False, out=out, colour=True)
        self.assertIn("\033[", out.getvalue())
        self.assertIn(addr(30), out.getvalue())

    def test_no_color_environment_variable(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        old = os.environ.get("NO_COLOR")
        os.environ["NO_COLOR"] = "1"
        try:
            self.assertFalse(qrf.use_colour(Tty()))
        finally:
            if old is None:
                del os.environ["NO_COLOR"]
            else:
                os.environ["NO_COLOR"] = old

    def test_footer_credits(self):
        out = io.StringIO()
        qrf.print_report(self.f, False, out=out)
        self.assertIn("https://quantus.watch", out.getvalue())
        self.assertIn("https://x.com/popek_1990", out.getvalue())


class AirdropEdgeCases(unittest.TestCase):
    def serve(self, snapshot, unpaid):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = (snapshot if self.path == "/snapshot" else unpaid).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_port

    def scan(self, url):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "node.log").write_text("Rewards wormhole address: %s\n" % addr(60))
        return found(tmp, "--check-airdrop", "--server", url)[0]

    def test_empty_unpaid_list_is_not_reported_as_paid(self):
        snap = json.dumps({"rows": [{"address": "t", "account": acc(60).hex(), "amount_hundredths": 500,
                                     "testnets": ["Dirac"]}]})  # also: account without 0x
        res = self.scan(self.serve(snap, json.dumps({"rows": []})))
        self.assertEqual(res[addr(60)]["airdrop"]["status"], "unknown")
        self.assertEqual(res[addr(60)]["airdrop"]["qtc"], 5.0)

    def test_garbage_answer_keeps_the_report(self):
        res = self.scan(self.serve('{"rows": [1, null, {"account": null}]}', "not json"))
        self.assertIn(addr(60), res)
        self.assertNotIn("airdrop", res[addr(60)])

    def test_null_fields_do_not_crash(self):
        snap = json.dumps({"rows": [{"address": "t", "account": "0x" + acc(60).hex(),
                                     "amount_hundredths": None, "testnets": None}]})
        res = self.scan(self.serve(snap, json.dumps({"rows": [{"address": "t", "status": "unclaimed"}]})))
        self.assertEqual(res[addr(60)]["airdrop"]["status"], "NOT CLAIMED YET")


class Airdrop(unittest.TestCase):
    def test_statuses_from_public_lists(self):
        snapshot = {"rows": [
            {"address": "t-paid", "account": "0x" + acc(20).hex(), "amount_hundredths": 1234,
             "testnets": ["Dirac"], "kind": "dilithium"},
            {"address": "t-open", "account": "0x" + acc(21).hex(), "amount_hundredths": 10,
             "testnets": ["Schrodinger"], "kind": "dilithium"},
            {"address": "t-wait", "account": "0x" + acc(22).hex(), "amount_hundredths": 665,
             "testnets": ["Planck"], "kind": "wormhole"},
        ]}
        unpaid = {"rows": [{"address": "t-open", "status": "unclaimed"},
                           {"address": "t-wait", "status": "recorded"}]}
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                body = json.dumps(snapshot if self.path == "/snapshot" else unpaid).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            tmp = pathlib.Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tmp, True)
            (tmp / "node.log").write_text("".join("Rewards wormhole address: %s\n" % addr(n)
                                                  for n in (20, 21, 22, 23)))
            res, _ = found(tmp, "--check-airdrop", "--server", "http://127.0.0.1:%d" % srv.server_port)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(res[addr(20)]["airdrop"]["status"], "paid out")
        self.assertEqual(res[addr(20)]["airdrop"]["qtc"], 12.34)
        self.assertEqual(res[addr(21)]["airdrop"]["status"], "NOT CLAIMED YET")
        self.assertEqual(res[addr(22)]["airdrop"]["status"], "claimed, waiting for payout")
        self.assertIsNone(res[addr(23)]["airdrop"])
        # only the two public lists are fetched; no address ever goes to the server
        self.assertEqual(sorted(requests), ["/snapshot", "/unpaid"])


if __name__ == "__main__":
    unittest.main()
