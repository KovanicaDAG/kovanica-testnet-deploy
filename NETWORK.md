# Kovanica Testnet Network Topology

Current live seeds for `kovanica-testnet`.

## Bootstrap peers (P2P)

Use these in `KOVANICA_PEERS` on participant/miner/explorer nodes:

```
seed.kovanica.online:9000
seed2.kovanica.online:9000
seed3.kovanica.online:9000
```

At least one reachable seed is enough to join the network. Listing all three
provides failover.

## Seed operators

| Seed | Role | Authority | P2P endpoint | Explorer (loopback) |
|---|---|---|---|---|
| seed.kovanica.online | primary seed | authority-1 | `145.223.116.178:9000` | `127.0.0.1:8080` |
| seed2.kovanica.online | secondary seed | authority-2 | `76.13.250.65:9000` | `127.0.0.1:8080` |
| seed3.kovanica.online | tertiary seed | authority-3 | `187.7.27.139:9000` | `127.0.0.1:8080` |

PoA threshold is **2 of 3**. The network tolerates any single seed being down.

## Current genesis

```
93efd2d784c19e0ea74b53c4b1aec1aa070a2d6cd8042058d934b18a6e23ab0a
```

Verify a local node has joined the right network:

```bash
curl -s http://127.0.0.1:8080/api/head | jq '.genesis'
```

## DNS notes

All `*.kovanica.online` records used for P2P must be **grey-cloud** (DNS-only)
so TCP port 9000 reaches the origin host directly. Do not proxy P2P records
through Cloudflare.
