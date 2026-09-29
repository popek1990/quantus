"""Offline tests. Run:  python3 -m unittest discover -s tests"""

import contextlib
import gzip
import http.server
import io
import json
import os
import pathlib
import shutil
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
