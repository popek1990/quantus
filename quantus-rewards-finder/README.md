# quantus-rewards-finder

Find every Quantus mining rewards address your machine still remembers.

**Community tool. Not affiliated with the Quantus team.**

By **popek_1990** - [x.com/popek_1990](https://x.com/popek_1990) · [github.com/popek1990](https://github.com/popek1990)

---

## Get it and run it

You need Python 3.8+. Everything is in one file with no dependencies. `sudo` is optional but
recommended, because node logs often sit in root-only folders.

### Download

```bash
# Clone the repo (this tool lives in the quantus-rewards-finder/ folder)
git clone https://github.com/popek1990/quantus.git
cd quantus/quantus-rewards-finder

# Or: just download the script
curl -O https://raw.githubusercontent.com/popek1990/quantus/main/quantus-rewards-finder/quantus_rewards_finder.py
```

### Run it

```bash
python3 quantus_rewards_finder.py --help
sudo python3 quantus_rewards_finder.py               # search this machine
sudo python3 quantus_rewards_finder.py --check-airdrop   # also check airdrop status
```

---

## Why this exists

The Quantus testnet airdrop pays the address that **received the block rewards**. That address is whatever you gave the node when you started it (`--rewards-address`, `--rewards-preimage` or `--rewards-inner-hash`). In many cases it is **not** the address of your phone wallet.

People who mined months ago often forgot which address it was. The mining machine usually still has it, because every Quantus node writes it into its log at start-up:

```
⛏️Using provided rewards address: 0x000102…1e1f (qz…)    # Resonance / Schrödinger / Dirac
Using address for rewards: qz…                         # Dirac v0.4.8–v0.4.9
Rewards wormhole address: qz…                          # Planck and mainnet
```

This tool finds those lines and every other trace of a rewards address across the whole machine. It prints the address, the chain, the node version, the dates it was used, and where it found it.

---

## What it searches

| Where | What it looks for |
|---|---|
| Every readable text file under your home folder, `/var/log`, `/var/lib`, `/opt`, `/srv`, `/etc`, `/usr/local`, `/tmp`, `/mnt`, `/media` (with `sudo`, also `/root` and other home folders) | node logs, rotated logs (`.gz`, `.xz`, `.bz2`), start scripts, systemd units, `docker-compose` files, `.env` files, shell history, saved terminal output |
| `journalctl` (system and user) | nodes that ran as a systemd service |
| `docker logs` | containers whose name or image mentions Quantus or a testnet |
| `/proc/*/cmdline` and `environ` | a node running right now |
| `~/.quantus/wallets/*.json` | quantus-cli wallets (only the public `address` field is read) |

It recognizes every log format from Resonance (v0.1) to mainnet (v1.x), the `--rewards-address` flag and `REWARDS_ADDRESS=` settings, and key generation output. Addresses shown as hex account ids (older nodes) are converted to the full `qz…` form. Every address is validated (network prefix 189 and SS58 checksum), so random text never ends up in the results.

It skips binary files, node databases (`db/full`, `paritydb`), keystores, `.git`, `node_modules` and snap/containerd images. A 6 GB node log takes about 15 seconds.

---

## What it prints

Results are grouped by how strong the evidence is:

### Addresses a node mined to

The node itself logged at start-up that block rewards go to these addresses. These are the ones that matter for the airdrop.

### Addresses set as the rewards address

Found in a command line, start script, config file or shell history. Probably used for mining, but only a node log proves it.

### Other Quantus addresses on this machine

Key generation output and wallets. Notes and chat exports can contain other people's addresses too, so check the file path.

### Also listed separately

* **Treasury fallback**: the node ran without a rewards address, so its rewards went to the treasury. Those addresses are not yours; this tells you that node earned you nothing.
* **Wormhole inner hashes** (Planck/mainnet), shortened on purpose. The matching address is the `Rewards wormhole address` the node logged at start-up.

### Example output

```
====================================================================================================
 Addresses a node mined to: 1
 The node itself logged at start-up that block rewards go to these addresses.
====================================================================================================

  1. qzjTEwCD…ay16V
     account id   0x000102…1e1f
     chain        Quantus Dirac Testnet
     node version 0.4.2
     found as     node log: rewards address
     seen         2025-12-01 10:00:00  ->  2026-02-01 18:30:00  (12 times)
     where        /var/lib/quantus/node.log  (x12)
     airdrop      12.34 QTC (Dirac)  -  NOT CLAIMED YET
```

Use `--json FILE` to export as JSON (`--json -` prints only JSON, no text report).

---

## `--check-airdrop` (privacy explained)

Adds one line per address: the QTC allocation, testnets and claim status (`NOT CLAIMED YET`, `claimed, waiting for payout`, `paid out`, or `not on the airdrop list`).

**How it works, so you know your privacy is protected:**

1. The tool downloads two **public** lists from `airdrop-claim.quantus.com`:
   - `GET /snapshot` — testnet airdrop allocations by account
   - `GET /unpaid` — claim statuses (claimed, unclaimed, recorded)
2. Matching happens entirely on your machine.
3. Your addresses are never sent anywhere. Only those two public endpoints are accessed.
4. Without `--check-airdrop`, the tool makes no network connection at all.

The claim itself is done with the official Quantus app or `quantus-cli`, never on a website.

---

## Safety

* **It never asks for, prints, stores or sends a seed phrase, secret key or wormhole secret.** It reads files to find public addresses only. Anything else is ignored. The tests verify this.
* Wormhole inner hashes are printed shortened (`0x1234ab…cdef`), never in full.
* **One Python file, standard library only, no pip packages.** Read it before you run it.
* Want to see it work before pointing it at your whole machine? Run it on one file first:
  `python3 quantus_rewards_finder.py --only /path/to/node.log`
* Read-only: it never writes anywhere except the `--json` file you name.
* The report shows the file paths where each address was found (e.g. your home folder name).
  Before you post the output anywhere public, cut those lines out; the addresses alone are enough.
* **No website or person needs your seed phrase to "check" an address.** The addresses this tool prints are public; that is all anyone needs to look them up.

---

## Nothing found?

* **Run it with `sudo`.** Node logs are often in root-only folders such as `/var/lib/quantus` or `/var/lib/docker/containers`. The report tells you how many files it could not read.
* **Right machine?** Run it on every machine and VPS you mined on. Deleted VPS take their logs with them.
* **Old backups or disk images:** pass them as extra paths. Example: `sudo python3 quantus_rewards_finder.py /mnt/old-disk`
* **Logs already rotated away?** Look in the start script or systemd unit (`/etc/systemd/system/*.service`). The tool reads those too.
* **Mined through a pool?** Then the blocks went to the pool's address, not yours.
* **Ran without `--rewards-address`?** Older nodes then paid the treasury, which the tool lists as a treasury fallback.
* Still nothing: the Quantus team has the full chain history. Ask them on their official channels with your addresses only, never with a seed phrase.

---

## Options

```
python3 quantus_rewards_finder.py [PATH ...] [FLAGS]

Paths:
  PATH ...                  extra files or folders to search (a mounted backup, a copied log)

Flags:
  --only                    search only the given paths, not the default locations
  --check-airdrop           show each address's airdrop allocation and claim status
  --json FILE               also write the results as JSON ('-' = stdout only)
  --no-journal              skip systemd journal
  --no-docker               skip docker logs
  --no-processes            skip running processes
  --quiet                   no progress output
  --version                 show version
  --help                    show this message
```

---

## Requirements and speed

Linux, Python 3.8 or newer. No packages to install. macOS works too (without journal, Docker and processes).

Scanning a busy home folder (hundreds of thousands of files) takes a few minutes. Progress is shown on the terminal. To check just one log: `python3 quantus_rewards_finder.py --only node.log`.

---

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests run offline with generated addresses. The SS58 code is checked against addresses printed by the official Quantus node binaries, and `--check-airdrop` is tested against a local fake server.

---

## Disclaimer

Estimates and evidence, not promises. Airdrop amounts and statuses come from the Quantus team's public claim server at the moment you run the tool. This project is not affiliated with Quantus Network.

---

## Author

**popek_1990**

* X / Twitter: [x.com/popek_1990](https://x.com/popek_1990)
* GitHub: [github.com/popek1990](https://github.com/popek1990)

---

MIT License. See [LICENSE](../LICENSE) for details.
