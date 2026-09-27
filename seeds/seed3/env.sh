#!/usr/bin/env bash
# Seed3: seed3.kovanica.online (tertiary seed)
# P2P: seed3.kovanica.online:9000 (grey-cloud DNS)
# Authority keys: /root/kovanica-testnet/authority-keys/authority-3.env

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# SSH connection (for deploy/management from workspace)
export SEED3_SSH_HOST="187.7.27.139"
export SEED3_SSH_USER="root"
export SEED3_SSH_KEY="/root/.ssh/seed3_deploy_key"

# Consensus: PoA (public authority set loaded from deployment repo)
export KOVANICA_CONSENSUS=poa
source "$SCRIPT_DIR/authority-keys/authorities.conf"

# Node config
export KOVANICA_LISTEN=0.0.0.0:9000
export KOVANICA_PEERS=seed.kovanica.online:9000
export KOVANICA_FAUCET=0
export KOVANICA_ALLOW_RESET=0
export KOVANICA_OPERATOR=1
export KOVANICA_DATA=/var/lib/kovanica-seed3
export KOVANICA_ISOLATED_HOST=1

# Block production (authority-3)
export KOVANICA_PRODUCE=1
export KOVANICA_PRODUCE_SECS=3
# Authority key loaded from separate file:
# source /root/kovanica-testnet/authority-keys/authority-3.env
