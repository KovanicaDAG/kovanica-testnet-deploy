#!/usr/bin/env bash
# Seed1: testnet.kovanica.online (primary seed, faucet, explorer)
# VPS: srv1745734 (Hostinger)
# P2P: seed.kovanica.online:9000 (grey-cloud DNS)
# Explorer: https://testnet.kovanica.online (orange-cloud)
# Authority keys: /root/kovanica-testnet/authority-keys/authority-1.env

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# SSH connection (local machine - localhost)
export SEED1_SSH_HOST="127.0.0.1"
export SEED1_SSH_USER="root"
export SEED1_SSH_KEY="${HOME}/.ssh/id_rsa"

# Consensus: PoA (public authority set loaded from deployment repo)
export KOVANICA_CONSENSUS=poa
source "$SCRIPT_DIR/authority-keys/authorities.conf"

# Node config
export KOVANICA_LISTEN=0.0.0.0:9000
export KOVANICA_PEERS=seed2.kovanica.online:9000,seed3.kovanica.online:9000
export KOVANICA_FAUCET=0
export KOVANICA_ALLOW_RESET=0
export KOVANICA_OPERATOR=1
export KOVANICA_DATA=/root/kovanica-data
export KOVANICA_ISOLATED_HOST=1

# Block production (authority-1)
export KOVANICA_PRODUCE=1
export KOVANICA_PRODUCE_SECS=3
# Authority key loaded from separate file:
# source /root/kovanica-testnet/authority-keys/authority-1.env
