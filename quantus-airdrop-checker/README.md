# quantus-airdrop-checker

- **What it is:** a script that finds every Quantus testnet address your seed phrases control, and shows how much of the testnet airdrop each one holds and whether it is still unclaimed.
- **Who needs it:** anyone who mined a Quantus testnet (Resonance, Schrödinger, Dirac, Planck) and is not sure which addresses were theirs.
- **Why it matters:** the claim is open until **1 December 2026**, and the same seed phrase produced a different address on almost every node version, so people miss coins they own.
- **How to run it:** `./quantus_airdrop_checker.sh`, then paste your phrases. Nothing you paste appears on screen.
- **Is it safe:** it works offline, never stores or sends your phrases, and cannot claim anything. Read the code first - it is one file, written to be read.

**Community tool. Not affiliated with the Quantus team.**

By popek_1990 · [x.com/popek_1990](https://x.com/popek_1990)

![quantus-airdrop-checker finding the airdrop of a seed phrase](docs/check.gif)

<sub>Recorded in a real terminal on a demo machine: the phrase pasted is the published BIP39 test phrase, and the
claim-server answers are made up. No real person's data is shown.</sub>

---

## Get it and run it

You need Linux x86_64 and Python 3.9 or newer. No packages to install.

```bash
git clone https://github.com/popek1990/quantus.git
cd quantus/quantus-airdrop-checker

./quantus_airdrop_checker.sh --download-only   # once: fetch and verify the official data (~220 MB)
./quantus_airdrop_checker.sh                   # paste your seed phrases, get your list
```

Run it from a folder of your own (for example your home folder), not inside the cloned repo: the encrypted
report file is written to the folder you run it from.

![--download-only fetching and verifying the official data](docs/download.gif)

---

## Why it needs your seed phrase, and why you should still be careful

The same seed phrase produced a **different address on every Quantus testnet**, and even between releases of
the same testnet - eight address generations in total. If you mined months ago and no longer have the
address written down, only the phrase can tell you what you own.

Scams ask for seed phrases too. So judge this tool the way you should judge any of them:

* it is **one readable Python file** (`quantus_airdrop_checker.py`) plus a short launcher - read them first;
* your phrases are **never written to disk, never sent anywhere, never printed**. To be exact about the one
  thing no program can promise: while it runs, your phrases are in its memory, and Python cannot wipe a string.
  Core dumps are disabled and nothing is written out, but a machine that swaps to disk or hibernates mid-run
  is outside any program's control. If that matters, use `--docker`, or a machine with no swap;
* it works **fully offline**: `--download-only` once, then `--offline` - you can unplug the network and watch
  it still work (claim statuses are then as of that download);
* while a phrase is being checked it is, for a fraction of a second, **visible in this machine's process
  list** (seven of the eight official builds accept it only as a command-line argument). Anyone logged in as
  you, and root, can see it in that moment - in `--docker` too. Only run this where nobody else is logged in;
* it **cannot claim anything**. You claim with the official Quantus mobile app or `quantus-cli`. Any website
  or DM that offers to "check" or "claim" with your phrase is a scam - including one that looks like this project.

---

## What it does

1. **Downloads the official data**, once:
   * eight official `quantus-node` builds from the Quantus GitHub releases, each checked against a SHA256
     pinned in the source;
   * the four official testnet miner lists from `Quantus-Network/task-master`, each checked against the hash
     GitHub's own API reports;
   * the airdrop list and the claim statuses from the team's claim server (`airdrop-claim.quantus.com`,
     `GET /snapshot` and `GET /unpaid`).

   Every one of these is a plain download of a public file. **Nothing is uploaded**: which addresses are yours
   is worked out on your machine.
2. **Derives every address your phrases ever had**: all eight generations, HD accounts 0 to 8, the un-derived
   key, and the wormhole addresses of the Planck/mainnet generation - about 72 addresses per phrase.
3. **Looks each one up** and shows what you control: testnet, blocks mined, the QTC it holds in the airdrop,
   and its claim status. The amounts are the claim server's own - the same list the official claim uses -
   so nothing here is an estimate.

If the claim server cannot be reached, the tool still shows what you mined and says plainly that amounts
and claim status are unknown.

### Claim status

| Status | What it means | What to do |
|---|---|---|
| **NOT CLAIMED YET** | on the airdrop list, and nobody has claimed it | claim it before 1 December 2026 with the official Quantus mobile app or `quantus-cli` (`quantus airdrop claim`) |
| claimed, waiting for payout | claimed; the team pays in batches by hand | nothing. The tool shows the address it was claimed **to** - if that is not yours, someone else has this seed phrase |
| paid out | the server no longer lists it as unpaid | nothing |
| not in the airdrop | mined, but not on the airdrop list | nothing to claim |
| unknown | the claim server could not be used | run again later, or with network access |

Addresses from nodes v0.4.5-v0.4.9 (HD accounts) get an extra warning: the official claim tool
(quantus-cli v2.3.0 and the app, checked 2026-09-22) does not look at those key paths. They are on the list,
but you need to ask the Quantus team how to claim them - give them the address only, never the phrase.

---

## How to paste your phrases

However you have them written down. There is no format to learn:

```
rig-1 | word word word ... word          a label and a phrase, either order, separated by |
word word word ... word | rig-1
word word word ... word                  just the phrase
word word word ... word                  several phrases, one per line
word word                                a phrase written as a grid, or one word per line -
word word                                consecutive lines are joined, a blank line separates phrases
# the box in the basement                a comment (but a phrase on a # line is still read)
```

Labels are optional. They are shown on screen and written into the report, and nowhere else. A label that
itself contains several wordlist words is dropped rather than printed - it would be a piece of a phrase. If a
run of words could be read in more than one way, every reading is checked, so nothing is lost to a guess.

Before the phrases you may paste the addresses you already know (step 3/5). They are public, so they are
shown back to you, and the report then tells you which of them no phrase reproduces - where a phrase is missing.

A BIP39 passphrase (the optional "25th word") is **not** supported.

---

## No match?

A phrase that matches nothing does not mean the money is gone. In order of likelihood:

1. **You did not mine with that phrase.** Many nodes were started with a rewards address from another wallet.
   Still have the mining machine? [quantus-rewards-finder](../quantus-rewards-finder/) reads the rewards address
   out of the node's logs and scripts - no phrase needed.
2. **That machine ran a build from between two releases** (compiled from source), so its address generation is
   one this tool does not have. Run `quantus-node --version` on it if you still can, and open an issue with the
   version.
3. **The address came from the mobile app**, which derives along a different path.
4. **You used a BIP39 passphrase** (see above).

The boundaries between the eight generations were mapped by running one test phrase through 33 chain releases.
They match every case seen so far, but "no match" means "not found by this tool", never "lost".

---

## Older Linux? Use `--docker`

The official Quantus builds need **glibc 2.39 or newer** (Ubuntu 24.04+, Debian 13+, Fedora 40+). On older
systems (Ubuntu 22.04, Debian 12) they cannot start. The tool checks this before it shows you anything: every
build must reproduce a known test address on your machine, and if one cannot, the run stops with an
explanation instead of reporting "nothing found".

`--docker` runs everything inside a container with a current glibc, **no network at all**, a read-only
filesystem and no swap. Run `--download-only` first (outside the container). The container has no gpg, so the
report is shown on screen only. It does **not** hide the phrase from this machine's process list.

---

## Options

```
./quantus_airdrop_checker.sh [--download-only] [--offline] [--self-test] [--docker] [--no-color]

  --download-only   fetch and verify the official data, including the airdrop list, then stop
                    (never asks for a phrase)
  --offline         never touch the network; use the data from the last --download-only
  --self-test       derive from the published BIP39 test phrases and check the result (asks for nothing)
  --docker          run inside a network-less container (see "Older Linux?")
  --no-color        plain text, no colours (same as NO_COLOR=1)
```

Cache: `~/.cache/quantus-airdrop-checker/` (change it with `QUANTUS_AIRDROP_CHECKER_CACHE`).
`GITHUB_TOKEN`, if set, is sent to `api.github.com` only, to lift its 60-requests-per-hour limit.

---

## The report

On screen you get only what was found. The full list - every address derived from every phrase, with its
generation and key path - goes into `quantus-airdrop-report-<date>.md.gpg`, encrypted with the one password
you are asked for at the start (gpg, AES256). Read it with `gpg -d <file>`. That password protects a list of
addresses; it is not a key to anything, because your phrases are not stored. Press Enter at the password
prompt to skip the file entirely.

---

## Requirements

Linux x86_64, Python 3.9+, about 300 MB of free disk space, and `gpg` if you want the report file.
Windows: use WSL. macOS is not supported: the tool runs the official Linux builds.

---

## Tests

```bash
python3 tests/test_parser.py            # can a phrase be lost? (generated phrases, every shape)
python3 tests/test_match.py             # amounts, claim status, warnings (synthetic lists and server answers)
python3 tests/test_end_to_end.py        # the whole tool in a terminal: nothing pasted may ever be printed
python3 tests/test_found.py             # the run that finds something: table, status, warnings, report
./quantus_airdrop_checker.sh --self-test  # do the eight official builds still produce the same addresses?
```

![the offline tests passing](docs/tests.gif)

No test uses a real seed phrase: they paste the published all-zero BIP39 test phrase or generated ones.
`test_end_to_end.py` and `test_found.py` need the official builds (`--download-only` first).

---

## Development

`../tools/install-hooks.sh` installs a pre-commit hook for the whole repository that refuses to commit
Quantus addresses or anything shaped like a seed phrase.

---

## Tip

I wrote this because I mined those testnets myself and could not tell what I owned. If it finds coins you had
written off and you feel like it:

```
qzpaZ83dYsCyDdCcTgDhFj3WCPuPnvaEXJjj4FECDe9i4xWE9
```

Entirely optional. Nothing in this tool needs a payment to work, and the tool only mentions that address after
a run that actually found something. Anyone telling you that claiming an airdrop costs a fee is robbing you.

---

popek_1990 · [x.com/popek_1990](https://x.com/popek_1990) · MIT License, see [LICENSE](../LICENSE).

More Quantus data: [quantus.watch](https://quantus.watch) - airdrop progress, network hashrate and exchange flows.
