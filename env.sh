#!/usr/bin/env bash
# Testnet participant/explorer environment
# Source: source /root/kovanica-testnet/env.sh
# Runs: kovanica-node explorer 127.0.0.1:8080

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Consensus: PoA (public authority set loaded from deployment repo)
export KOVANICA_CONSENSUS=poa
source "$SCRIPT_DIR/authority-keys/authorities.conf"

# Node config
export KOVANICA_LISTEN=127.0.0.1:9000
export KOVANICA_PEERS=seed.kovanica.online:9000
export KOVANICA_FAUCET=0
export KOVANICA_ALLOW_RESET=0
export KOVANICA_OPERATOR=0
export KOVANICA_DATA=/root/kovanica-testnet/data/kovanica-data

# Block production (optional)
# export KOVANICA_PRODUCE=1
# export KOVANICA_PRODUCE_SECS=3
# Authority key loaded from separate file if producing:
# source /root/kovanica-testnet/authority-keys/authority-1.env
