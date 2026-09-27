#!/usr/bin/env bash
# deploy-seed1-poa.sh — Provision seed1 (PoA) on a fresh VPS.
# Run FROM operator machine that can SSH to target.

usage() { grep '^#' "$0" | cut -c4-; exit 0; }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEED1_ENV="${SCRIPT_DIR}/../testnet/seeds/seed1/env.sh"
[[ -f "$SEED1_ENV" ]] && source "$SEED1_ENV"

TARGET="${TARGET:-${SEED1_SSH_USER}@${SEED1_SSH_HOST}}"
NAME="seed1"
PEERS="seed2.kovanica.online:9000"
EXPLORER_PORT=8080
P2P_PORT=9000
KEEP_BUILD=0
METRICS_PORT=9090

AUTHORITIES="4a4172c14e6073998caf9ad256974cd2908a67b7751fdbc2f031c24736b8e8ec,8ebc8a73235b631845d32ed4ea2d1dc563acfa1215b18428b17364c6e1563cf3,d6903aa7a17abfe681988f1b49a8adcec0d475c5e24ab955c1349a1c463bedae"
THRESHOLD=2
SLOT_DURATION=3000

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help) usage ;;
        --name) NAME="$2"; shift 2 ;;
        --peers) PEERS="$2"; shift 2 ;;
        --explorer) EXPLORER_PORT="$2"; shift 2 ;;
        --p2p) P2P_PORT="$2"; shift 2 ;;
        --keep-build) KEEP_BUILD=1; shift ;;
        *)
            if [[ -z "$TARGET" ]]; then TARGET="$1"; else echo "Unknown argument: $1"; usage; fi
            shift ;;
    esac
done

[[ -z "$TARGET" ]] && { echo "Error: <user>@<host> required"; usage; }

IFS=',' read -ra AUTH_ARRAY <<< "$AUTHORITIES"
if [[ ${#AUTH_ARRAY[@]} -ne 3 ]]; then
    echo "ERROR: AUTHORITIES must contain exactly 3 comma-separated 64-char hex pubkeys"
    exit 1
fi
for key in "${AUTH_ARRAY[@]}"; do
    if [[ ! "$key" =~ ^[0-9a-f]{64}$ ]]; then
        echo "ERROR: Invalid pubkey format: $key (must be 64 hex chars)"
        exit 1
    fi
done

REPO_ROOT="/root/kovanica/protocol"
REMOTE_SRC="/opt/kovanica-src"
REMOTE_DATA="/root/kovanica-data"
HOSTNAME="seed.kovanica.online"

echo "=== Kovanica seed1 (PoA) deploy: $NAME -> $TARGET ==="
echo "Target hostname for DNS: $HOSTNAME"
echo "Authorities: ${#AUTH_ARRAY[@]}, Threshold: $THRESHOLD, Slot: ${SLOT_DURATION}s"

echo "[1/8] Shipping source tarball..."
TARBALL=$(mktemp /tmp/kovanica-src.XXXX.tar.gz)
git -C "$REPO_ROOT" archive --format=tar.gz -o "$TARBALL" HEAD
ssh -i "$SEED1_SSH_KEY" "$TARGET" "sudo mkdir -p '$REMOTE_SRC' && sudo rm -rf '$REMOTE_SRC'/*"
scp -q -i "$SEED1_SSH_KEY" "$TARBALL" "$TARGET:/tmp/kovanica-src.tar.gz"
ssh -i "$SEED1_SSH_KEY" "$TARGET" "sudo tar -xzf /tmp/kovanica-src.tar.gz -C '$REMOTE_SRC' && sudo chown -R \$(whoami) '$REMOTE_SRC'"
rm -f "$TARBALL"

echo "[2/8] Installing build prerequisites..."
ssh -i "$SEED1_SSH_KEY" "$TARGET" 'if command -v apt-get >/dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y -qq curl ca-certificates build-essential pkg-config >/dev/null
elif command -v dnf >/dev/null; then
    sudo dnf install -y -q gcc gcc-c++ make pkgconfig 2>/dev/null || sudo dnf install -y -q gcc gcc-c++ make
else
    echo "unsupported distro: need apt-get or dnf" >&2; exit 1
fi'

echo "[3/8] Ensuring swap..."
ssh -i "$SEED1_SSH_KEY" "$TARGET" 'if [ "$(free -m | awk "/Mem:/{print \$2}")" -lt 2000 ] && ! swapon --show | grep -q .; then
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile &&
    sudo mkswap /swapfile && sudo swapon /swapfile &&
    echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null;
fi'

echo "[4/8] Installing Rust toolchain..."
ssh -i "$SEED1_SSH_KEY" "$TARGET" 'if ! command -v cargo >/dev/null; then
    curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none >/dev/null 2>&1;
fi;
source "$HOME/.cargo/env"'

echo "[5/8] Building release binary..."
ssh -i "$SEED1_SSH_KEY" "$TARGET" "source \$HOME/.cargo/env && cd '$REMOTE_SRC' && cargo build --release --locked -p kovanica-node"

echo "[6/8] Installing systemd service (PoA config)..."
ssh -i "$SEED1_SSH_KEY" "$TARGET" "sudo mkdir -p '$REMOTE_DATA' && \
sudo cp '$REMOTE_SRC/target/release/kovanica-node' /usr/local/bin/kovanica-node && \
sudo tee /etc/systemd/system/kovanica-$NAME.service >/dev/null <<EOF
[Unit]
Description=Kovanica PoA validator (seed1, authority-1)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REMOTE_DATA
Environment=KOVANICA_LISTEN=0.0.0.0:$P2P_PORT
Environment=KOVANICA_CONSENSUS=poa
Environment=KOVANICA_AUTHORITIES=$AUTHORITIES
Environment=KOVANICA_AUTHORITY_THRESHOLD=$THRESHOLD
Environment=KOVANICA_SLOT_DURATION=$SLOT_DURATION
Environment=KOVANICA_PEERS=$PEERS
Environment=KOVANICA_OPERATOR=0
Environment=KOVANICA_FAUCET=0
Environment=KOVANICA_ALLOW_RESET=0
Environment=KOVANICA_DATA=$REMOTE_DATA
Environment=KOVANICA_METRICS=0.0.0.0:$METRICS_PORT
Environment=KOVANICA_PRODUCE=1
Environment=KOVANICA_PRODUCE_SECS=3
EnvironmentFile=/root/kovanica/deploy/testnet/authority-keys/authority-1.env
ExecStart=/usr/local/bin/kovanica-node explorer 127.0.0.1:$EXPLORER_PORT
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now kovanica-$NAME"

echo "[7/8] Firewall + fail2ban..."
ssh -i "$SEED1_SSH_KEY" "$TARGET" "
command -v ufw >/dev/null && sudo ufw allow ${P2P_PORT}/tcp comment 'Kovanica P2P' >/dev/null || true
command -v ufw >/dev/null && sudo ufw allow from 127.0.0.1 to any port ${EXPLORER_PORT} comment 'Explorer loopback' >/dev/null || true
command -v ufw >/dev/null && sudo ufw allow from 127.0.0.1 to any port ${METRICS_PORT} comment 'Metrics loopback' >/dev/null || true
command -v apt-get >/dev/null && sudo apt-get install -y -qq fail2ban >/dev/null 2>&1 || true
sudo tee /etc/fail2ban/jail.local >/dev/null <<'F2B'
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 3600
findtime = 600
[recidive]
enabled = true
filter = recidive
logpath = /var/log/fail2ban.log
bantime = 86400
findtime = 86400
F2B
sudo systemctl enable --now fail2ban || true
" || true

echo "[8/8] Prometheus node-exporter..."
ssh -i "$SEED1_SSH_KEY" "$TARGET" "
command -v apt-get >/dev/null && sudo apt-get install -y -qq prometheus-node-exporter >/dev/null 2>&1 || true
sudo systemctl enable --now prometheus-node-exporter || true
" || true

if [[ "$KEEP_BUILD" != 1 ]]; then
    echo "Cleaning up source tree..."
    ssh -i "$SEED1_SSH_KEY" "$TARGET" "sudo rm -rf '$REMOTE_SRC' /tmp/kovanica-src.tar.gz"
fi

echo "Waiting for sync verification..."
sleep 15

SEED_GENESIS=$(curl -sS --max-time 10 http://seed2.kovanica.online:8080/api/head 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['genesis'])" 2>/dev/null || echo "unknown")

NEW_HEAD=$(ssh -i "$SEED1_SSH_KEY" "$TARGET" "curl -sS --max-time 10 http://127.0.0.1:$EXPLORER_PORT/api/head 2>/dev/null || true")
NEW_GENESIS=$(echo "$NEW_HEAD" | python3 -c "import json,sys; print(json.load(sys.stdin).get('genesis',''))" 2>/dev/null || echo "unknown")

if [[ -n "$NEW_GENESIS" && "$NEW_GENESIS" == "$SEED_GENESIS" ]]; then
    echo "=== OK: $NAME synced to the same network (genesis $NEW_GENESIS) ==="
else
    echo "=== WARNING: could not confirm genesis match (seed: $SEED_GENESIS, got: $NEW_GENESIS) ==="
    echo "Check: ssh $TARGET journalctl -u kovanica-$NAME -n 50"
fi

cat <<EOF

=== seed1 (PoA) deployed ===

P2P endpoint      : \${TARGET#*@}:\${P2P_PORT}  (advertise this / DNS A: $HOSTNAME)
Explorer HTTP     : loopback :\${EXPLORER_PORT}  (ssh -L \${EXPLORER_PORT}:127.0.0.1:\${EXPLORER_PORT} \${TARGET})
Prometheus metrics: loopback :\${METRICS_PORT}   (ssh -L \${METRICS_PORT}:127.0.0.1:\${METRICS_PORT} \${TARGET})

Service: systemctl status kovanica-\${NAME}
Logs:    journalctl -u kovanica-\${NAME} -f
Data:    /root/kovanica-data

DNS:
  A     \$HOSTNAME   -> \${TARGET#*@}
  AAAA  \$HOSTNAME   -> (if IPv6)

Bootstrap list (add to KOVANICA_PEERS on new nodes):
  seed.kovanica.online:9000
  seed2.kovanica.online:9000

Next:
  1. Add DNS A/AAAA record for \$HOSTNAME (grey-cloud for P2P port 9000)
  2. Verify peer connectivity: curl http://\$HOSTNAME:\${EXPLORER_PORT}/api/head
  3. Verify metrics: curl http://\$HOSTNAME:\${METRICS_PORT}/metrics
  4. Test failover: stop seed2, verify seed1 continues producing blocks

Failover test:
  ssh seed2-host "sudo systemctl stop kovanica-seed2"
  # Watch seed1 produce blocks solo (threshold 2 of 3, needs 2)
  # With 3 authorities, threshold=2 tolerates 1 down
EOF
