# Kovanica Protocol — Project Management & Development Plan

**Version:** 1.0  
**Date:** 2026-10-01  
**Status:** Active  
**Owner:** Core Team

---

## 1. Repository Structure & Ownership

| Repository | Purpose | Owner | Deploy Target |
|------------|---------|-------|---------------|
| `KovanicaDAG/kovanica` | Monorepo: protocol crates, node, CLI, SDK, web, mobile, installer | Core Team | N/A (library) |
| `KovanicaDAG/kovanica-dashboard` | Developer Dashboard (React + Python proxy) | Web Team | `dash.kovanica.online` |
| `KovanicaDAG/kovanica-testnet-deploy` | Testnet deployment configs | Ops Team | seed1/2/3 VPS |
| `KovanicaDAG/kovanica-mainnet-deploy` | Mainnet deployment configs | Ops Team | mainnet seeds |
| `KovanicaDAG/kovanica-devnet` | Devnet config (isolated loopback) | Core Team | Local/dev |

**Branch policy:** `main` only, PRs required, linear history (rebase merge).  
**Tag format:** `v{major}.{minor}.{patch}-{network}` (e.g., `v0.4.0-testnet`, `v1.0.0-mainnet`).

---

## 2. Network Layers & Environments

| Layer | Network ID | Consensus | Seeds | Purpose |
|-------|------------|-----------|-------|---------|
| Devnet | `kovanica-devnet` | PoA (local) | 1 (loopback :9002) | Isolated development, `ALLOW_RESET=1`, `FAUCET=1` |
| Testnet | `kovanica-testnet` | PoA (3 seeds) | 3 (seed1/2/3) | Public testing, faucet enabled, RFC-006 active |
| Mainnet | `kovanica-mainnet` | PoA (TBD) | TBD (governance) | Production, no faucet, no reset |

**Seed roles:**
- `seed1` (genesis authority) — HTTP 8081, P2P 9000
- `seed2` — HTTP 8082, P2P 9001
- `seed3` — HTTP 8083, P2P 9002

---

## 3. Release Gates (must pass before tag)

| Gate | Command | Threshold |
|------|---------|-----------|
| Format | `cargo fmt --all --check` | 0 errors |
| Clippy | `cargo clippy --workspace --all-targets -- -D warnings` | 0 warnings |
| Tests | `cargo test --workspace` | 100% pass (68 suites, 874+ tests) |
| Build | `cargo build --release --workspace` | Success |
| Web build | `cd web/site && npm run build` | Success |
| Dashboard build | `cd ../kovanica-dashboard/frontend && npm run build` | Success |

**No exceptions.** Gates enforced in CI and pre-push hooks.

---

## 4. Development Workflow

### 4.1 Task Classification
Every change must be classified in PR description:
- **consensus-safe**: GHOSTDAG, block validation, supply rules, UTXO logic
- **ledger-safe**: Wallet, tx building, UTXO selection, indexes
- **client-only**: UI, API proxy, CLI, SDK, docs, deploy configs

### 4.2 PR Requirements
- Title: `[scope] verb: summary` (e.g., `consensus: fix blue-score calc on reorg`)
- Description: classification, risk, test plan, migration notes (if any)
- Review: 1 approval from relevant domain owner
- Gates: all must pass before merge

### 4.3 Commits
- Conventional commits: `feat/fix/chore/docs/refactor/test: ...`
- Single logical change per commit
- No fixup/squash commits in history (rebase before merge)

---

## 5. Milestone Roadmap

| Milestone | Target | Exit Criteria |
|-----------|--------|---------------|
| **M1: Testnet Stability** | 2026-10-15 | 3 seeds producing, sync < 5min, dashboard live, 0 P0 bugs |
| **M2: RFC-006 Hardening** | 2026-10-31 | Supply cap enforced at 100% height, maturity 100 blocks, fee burn 75% |
| **M3: API Stability** | 2026-11-15 | All node endpoints documented, versioned, backward-compatible |
| **M4: Wallet/UX Polish** | 2026-11-30 | Web wallet + dashboard parity, mobile wallet v0.2.0 |
| **M5: Advanced Features** | 2027-Q1 | Multisig UI, HTLC swap UI, NFT minting, DEX |
| **M6: Mainnet Readiness** | 2027-Q2 | Security audit, governance docs, seed ceremony, launch checklist |

**Hard gate:** M2 (RFC-006) must complete before M3+.

---

## 6. Security & Operational Rules

| Rule | Enforcement |
|------|-------------|
| No private keys in git | `.gitignore` + pre-commit hook (`gitleaks`) |
| Authority keys mode 0600, env files | Systemd `EnvironmentFile`, never inlined |
| P2P: plaintext TCP:9000 only, DNS seed `seed.kovanica.online` | Code review + `kovanica-p2p` skill |
| No Cloudflare orange-cloud for P2P | Deploy checklist |
| Faucet: testnet only, 5 KVNC cap, rate-limited | Runtime flag `KOVANICA_FAUCET` + network check |
| `ALLOW_RESET=1` only on devnet | Runtime flag + network check |
| Disk > 85% → alert | Prometheus rule |
| Node RSS > 8GB → alert + auto-restart | systemd `MemoryMax=12G` |
| Secret rotation: 90-day max for deploy keys | Calendar reminder + runbook |

---

## 7. Incident Response

| Severity | Response Time | Escalation |
|----------|---------------|------------|
| **P0** (chain halt, consensus fork, key compromise) | 15 min | Page core team, halt deployments |
| **P1** (seed down, API 502, sync stall > 10min) | 30 min | Page on-call |
| **P2** (dashboard slow, non-critical bug) | 4 hours | Next business day |
| **P3** (cosmetic, docs, feature request) | Next sprint | Backlog |

**Runbooks:** `/runbooks/` in each deploy repo (testnet-deploy, mainnet-deploy).

---

## 8. Monitoring & Alerting

| Metric | Alert Threshold | Source |
|--------|----------------|--------|
| `kovanica_block_height` stall | > 5 min no increase | Prometheus |
| `kovanica_peer_count` | < 2 for > 5 min | Prometheus |
| `kovanica_mempool_tx_count` | > 10000 | Prometheus |
| Node CPU | > 90% for 5 min | Prometheus / node_exporter |
| Node RSS | > 10 GB | systemd / Prometheus |
| Disk / | > 85% | Prometheus |
| HTTP 5xx rate | > 1% for 5 min | nginx / Prometheus |

**Dashboards:** Grafana (kovanica-dashboard, kovanica-nodes, kovanica-network).

---

## 9. Documentation Standards

| Doc Type | Location | Update Trigger |
|----------|----------|----------------|
| RFC/KVP | `protocol/docs/` or `docs.kovanica.online` | New feature, consensus change |
| API Reference | `web/site/src/routes/api-reference/` | Endpoint add/change |
| Deploy Runbooks | `deploy/{testnet,mainnet}/runbooks/` | New seed, config change |
| Architecture | `protocol/docs/architecture/` | Major refactor |
| Changelog | `CHANGELOG.md` per repo | Every release |

---

## 10. Communication

| Channel | Purpose |
|---------|---------|
| GitHub Issues | Bugs, features, tasks |
| GitHub Discussions | RFC proposals, design reviews |
| Discord `#kovanica-dev` | Real-time coordination |
| Discord `#kovanica-ops` | On-call, incidents |
| Email `security@kovanica.online` | Vulnerability reports |

---

## 11. Decision Log (append-only)

| Date | Decision | Rationale | Author |
|------|----------|-----------|--------|
| 2026-09-25 | PoA-only consensus ratified | Hybrid PoW/PoS complexity not justified | Core Team |
| 2026-09-30 | Testnet deploy repo separated from monorepo | Security (authority keys), clarity | Ops Team |
| 2026-10-01 | Dashboard built as separate repo | Independent deploy, different stack | Web Team |
| 2026-10-01 | Mainnet deploy repo created | Parity with testnet | Ops Team |

---

## 12. Appendix: Key Files Quick Reference

```
kovanica/
├── protocol/Cargo.toml          # Workspace root
├── protocol/crates/             # 7 crates
├── web/site/                    # TanStack web app
├── mobile/                      # Tauri apps
├── deploy/testnet/              # → kovanica-testnet-deploy repo
├── deploy/mainnet/              # → kovanica-mainnet-deploy repo
├── config/devnet/network.env    # Devnet env
├── CHANGELOG.md
└── PROJECT-MANAGEMENT.md        # This file

kovanica-testnet-deploy/
├── env.sh                       # Testnet env (seeds, genesis)
├── authority-keys/              # *.env (0600), authorities.conf
├── systemd/                     # kovanica-seed{1,2,3}.service
├── dashboard/                   # dashboard.py (legacy)
├── deploy-seed{1,2,3}-poa.sh    # Seed install scripts
├── SECURITY-NOTE.md
└── PROJECT-MANAGEMENT.md        # Symlink or copy

kovanica-mainnet-deploy/
├── env.sh                       # Mainnet template
├── mainnet-authority-keys/      # (empty — governance TBD)
├── systemd/                     # kovanica-validator, kovanica-seed
├── deploy-mainnet-seed.sh
├── deploy-mainnet-validator.sh
└── PROJECT-MANAGEMENT.md
```

---

**End of Document** — This is a living document. Update on every major decision.