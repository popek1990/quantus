# quantus-rewards-finder

- **What it is:** a small script that finds the Quantus mining rewards addresses your machine still remembers.
- **Who needs it:** anyone who mined a Quantus testnet and does not know which address got the rewards.
- **Why it matters:** the testnet airdrop pays that address.
- **How to run it:** `sudo python3 quantus_rewards_finder.py` on the machine you mined on.
- **Is it safe:** it only reads files and never asks for a seed phrase.

**Community tool. Not affiliated with the Quantus team.**

By popek_1990 · [x.com/popek_1990](https://x.com/popek_1990)

![quantus-rewards-finder scanning a mining machine](docs/scan.gif)

<sub>Recorded in a real terminal on a demo machine with made-up addresses. No real person's data is shown.</sub>

---

## Get it and run it

You need Python 3.8 or newer. It is one file with no dependencies. `sudo` is optional, but use it if you can:
node logs are often in folders that only root can read.

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

The Quantus testnet airdrop pays the address that received the block rewards. That is the address you gave
the node when you started it (`--rewards-address`, `--rewards-preimage` or `--rewards-inner-hash`).
Often it is not the address in your phone wallet.

Many people who mined months ago no longer remember it. The mining machine usually does, because every
Quantus node writes the address to its log when it starts:

```
⛏️Using provided rewards address: 0x000102…1e1f (qz…)    # Resonance / Schrödinger / Dirac
Using address for rewards: qz…                         # Dirac v0.4.8–v0.4.9
Rewards wormhole address: qz…                          # Planck and mainnet
```

The tool finds these lines, plus any other trace of a rewards address on the machine. For each address it
prints the chain, the node version, the dates it was used and where it was found.

---

## What it searches

| Where | What it looks for |
|---|---|
| Every readable text file under your home folder, `/var/log`, `/var/lib`, `/opt`, `/srv`, `/etc`, `/usr/local`, `/tmp`, `/mnt`, `/media` (with `sudo`, also `/root` and other home folders) | node logs, rotated logs (`.gz`, `.xz`, `.bz2`), start scripts, systemd units, `docker-compose` files, `.env` files, shell history, saved terminal output |
| `journalctl` (system and user) | nodes that ran as a systemd service |
| `docker logs` | containers whose name or image mentions Quantus or a testnet |
| `/proc/*/cmdline` and `environ` | a node running right now |
| `~/.quantus/wallets/*.json` | quantus-cli wallets (only the public `address` field is read) |

It knows every node log format from Resonance (v0.1) to mainnet (v1.x). It also reads the `--rewards-address`
flag (also split over two lines, or inside a Docker/YAML argument list), settings such as `REWARDS_ADDRESS=`,
`MINER_REWARDS_ADDRESS:` or `rewards_address =`, and key generation output. Older nodes show addresses as hex account ids;
the tool converts them to the full `qz…` form. Every address is checked (network prefix 189 and SS58 checksum),
so random text does not end up in the results.

It skips binary files, node databases (`db/full`, `paritydb`), keystores, `.git`, `node_modules` and
snap/containerd images. A 6 GB node log takes about 15 seconds.

---

## What it prints

Results are sorted into groups by how strong the evidence is.

### Addresses a node mined to

The node itself logged at start-up that block rewards go to these addresses. These are the ones that matter
for the airdrop.

### Addresses set as the rewards address

Found in a command line, start script, config file or shell history. Probably used for mining, but only a
node log proves it.

### Other Quantus addresses on this machine

Key generation output and wallets. Notes and chat exports can contain other people's addresses too, so check
the file path.

### Also listed separately

* Treasury fallback: the node ran without a rewards address, so its rewards went to the treasury. These
  addresses are not yours. It means that node earned you nothing.
* Wormhole inner hashes (Planck/mainnet), shortened on purpose. The matching address is the
  `Rewards wormhole address` the node logged at start-up.

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

Use `--json FILE` to save the results as JSON. `--json -` prints only JSON, without the text report.

---

## `--check-airdrop` and your privacy

This adds one line per address: the QTC amount, the testnets, and the claim status (`NOT CLAIMED YET`,
`claimed, waiting for payout`, `paid out`, or `not on the airdrop list`).

![--check-airdrop showing the claim status of each address](docs/airdrop.gif)

<sub>Demo with made-up addresses. The airdrop lists come from a local mock of the server, which is why the
clip shows `127.0.0.1`; a normal run downloads them from `airdrop-claim.quantus.com`.</sub>

How it works:

1. The tool downloads two public lists from `airdrop-claim.quantus.com`:
   - `GET /snapshot`: testnet airdrop amounts per account
   - `GET /unpaid`: claim statuses (claimed, unclaimed, recorded)
2. It compares them with your addresses on your own machine.
3. Your addresses are never sent anywhere. The tool only contacts those two public endpoints.
4. Without `--check-airdrop`, the tool makes no network connection at all.

You claim with the official Quantus app or `quantus-cli`, never on a website.

---

## Safety

* **It never asks for, prints, stores or sends a seed phrase, secret key or wormhole secret.** It looks for
  public addresses only and ignores everything else. The tests check this.
* Wormhole inner hashes are printed shortened (`0x1234ab…cdef`), never in full.
* One Python file, standard library only, no pip packages. You can read it before you run it.
* To try it on one file before scanning the whole machine:
  `python3 quantus_rewards_finder.py --only /path/to/node.log`
* It does not change anything. The only file it writes is the `--json` file you name.
* The report shows the file path where each address was found, and a path can include your home folder name.
  Cut those lines out before you post the output in public. The addresses alone are enough.
* No website or person needs your seed phrase to "check" an address. Addresses are public, and the address
  is all anyone needs to look it up.

---

## Nothing found?

* **Run it with `sudo`.** Node logs are often in root-only folders such as `/var/lib/quantus` or
  `/var/lib/docker/containers`. The report tells you how many files it could not read, and lists any
  source it could not search (for example `Not searched: docker logs (no access; try sudo)`).
* **Right machine?** Run it on every machine and VPS you mined on. A deleted VPS takes its logs with it.
* **Old backups or disk images:** add them as extra paths. Example: `sudo python3 quantus_rewards_finder.py /mnt/old-disk`
* **Logs already rotated away?** The address may still be in the start script or systemd unit
  (`/etc/systemd/system/*.service`). The tool reads those too.
* **Mined through a pool?** Then the blocks went to the pool's address, not yours.
* **Ran without `--rewards-address`?** Older nodes then paid the treasury. The tool lists this as a treasury fallback.
* Still nothing? The Quantus team has the full chain history. Ask on their official channels. Give them your
  addresses only, never a seed phrase.

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
  --no-color                plain text, no colours (also: NO_COLOR=1; off automatically when piped)
  --version                 show version
  --help                    show this message
```

---

## Requirements and speed

Linux, Python 3.8 or newer, nothing to install. It also works on macOS, but without the journal, Docker
and process checks.

A busy home folder (hundreds of thousands of files) takes a few minutes. Progress is shown in the terminal.
To check just one log: `python3 quantus_rewards_finder.py --only node.log`.

---

## Tests

```bash
python3 -m unittest discover -s tests
```

![the test suite passing](docs/tests.gif)

The tests run offline with generated addresses. The SS58 code is checked against addresses printed by the
official Quantus node binaries. `--check-airdrop` is tested against a local fake server.

---

## Disclaimer

The results are estimates and evidence, not promises. Airdrop amounts and statuses come from the
Quantus team's public claim server at the moment you run the tool. This project is not affiliated with
Quantus Network.

---

## Author

popek_1990 · [x.com/popek_1990](https://x.com/popek_1990)

MIT License. See [LICENSE](../LICENSE).

More Quantus data: [quantus.watch](https://quantus.watch) - airdrop progress, network hashrate and
exchange flows, updated every hour.
