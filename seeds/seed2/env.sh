#!/usr/bin/env bash
# Seed2: seed2.kovanica.online (secondary seed)
# VPS: srv1991525 (Hostinger KVM2) - 76.13.250.65
# P2P: seed2.kovanica.online:9000 (grey-cloud DNS)
# Authority keys: /root/kovanica/deploy/testnet/authority-keys/authority-2.env

# SSH connection (for deploy/management from workspace)
export SEED2_SSH_HOST="76.13.250.65"
export SEED2_SSH_USER="root"
export SEED2_SSH_KEY="/root/.ssh/seed2_deploy_key"

# Consensus: PoA
export KOVANICA_CONSENSUS=poa
export KOVANICA_AUTHORITIES=4a4172c14e6073998caf9ad256974cd2908a67b7751fdbc2f031c24736b8e8ec,8ebc8a73235b631845d32ed4ea2d1dc563acfa1215b18428b17364c6e1563cf3,d6903aa7a17abfe681988f1b49a8adcec0d475c5e24ab955c1349a1c463bedae
export KOVANICA_AUTHORITY_THRESHOLD=2
export KOVANICA_SLOT_DURATION=3000

# Node config
export KOVANICA_LISTEN=0.0.0.0:9000
export KOVANICA_PEERS=seed.kovanica.online:9000
export KOVANICA_FAUCET=0
export KOVANICA_ALLOW_RESET=0
export KOVANICA_OPERATOR=1
export KOVANICA_DATA=/var/lib/kovanica-seed2
export KOVANICA_ISOLATED_HOST=1

# Block production (authority-2)
export KOVANICA_PRODUCE=1
export KOVANICA_PRODUCE_SECS=3
# Authority key loaded from separate file:
# source /root/kovanica/deploy/testnet/authority-keys/authority-2.env
