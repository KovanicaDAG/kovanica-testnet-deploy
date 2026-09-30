# SECURITY NOTE — kovanica-testnet deploy repo

Written 2026-09-30. Read before changing this repository's git configuration.

## 1. The `origin` remote is WRONG — do not `git push` to it

| | |
|---|---|
| Configured `origin` | `https://github.com/KovanicaDAG/kovanica-testnet.git` |
| What that repo actually contains | a **~950-file stale monorepo snapshot** (`protocol/`, `docs/`, `00-Home/`, `20-Network/`, `PRE-MIGRATION-LS.txt`, `kovanica-clean-setup.sh`, …) |
| What this repo actually contains | 19 files of **deployment config** (systemd units, seed env, dashboard, authority key ceremony output) |
| Visibility | **public** — an unauthenticated clone succeeds |

They are different projects that were never reconciled. Pushing here would
overwrite that repository with unrelated content.

**Mitigation already in place:** the *push* URL is disabled:

```
git remote set-url --push origin \
  'DISABLED://kovanica-testnet-remote-is-a-stale-monorepo-snapshot-see-SECURITY-NOTE'
```

Fetch is untouched. To push to the real deploy repo, use an explicit URL:

```sh
git push git@github.com:KovanicaDAG/kovanica-testnet-deploy.git main
```

Confirm the intended target with the operator before any first push.

## 2. PoA authority signing keys were tracked — now untracked

`authority-keys/authority-{1,2,3}.env` each hold
`KOVANICA_AUTHORITY_KEY=<64 hex>` = a **32-byte Ed25519 seed** (secret,
mode 0600). They are the real, currently-active testnet authority keys, and
`systemd/kovanica-seed{1,2,3}.service` load them via `EnvironmentFile=`.

They were committed in all 6 local commits of this repo. **None of those commits
were ever pushed** (`origin/main` is `706bf6d` and contains no `authority-keys`
files), so **the keys never reached GitHub and no history rewrite is required.**

Fixed on 2026-09-30 (commit `c1331be`):

* added `.gitignore` — the repo previously had none;
* `git rm --cached` the three `.env` files; **they remain on disk, mode 0600,
  because systemd needs them.**

`authority-keys/authorities.conf` is **public and deliberately tracked** — it
holds only 64-hex public keys plus `KOVANICA_THRESHOLD`, and every node must
agree on that set. The `.gitignore` targets `*.env` only, never the directory.

## 3. Rotating these keys

If the on-disk `.env` files are ever suspected compromised:

1. Run the authority-key ceremony to mint a new key set.
2. Replace the three `.env` files on the host (still mode 0600).
3. Update `authority-keys/authorities.conf` with the new public keys and
   threshold, and distribute it to **every** seed before restarting them — a
   seed with a stale `authorities.conf` cannot validate blocks.
4. Restart all three seeds. Never rotate one key alone; `KOVANICA_THRESHOLD=2`
   means two of three must sign.
