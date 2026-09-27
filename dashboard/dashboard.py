#!/usr/bin/env python3
"""Kovanica testnet PoA network dashboard (DEV / non-production).

Self-contained: Python standard library only, no external dependencies.

Polls a small JSON endpoint set from every PoA seed (locally over HTTP, or over
SSH for remote hosts) and serves a single-page dashboard:

  * per-seed status cards (blocks, peers, slot, latency, uptime)
  * RFC-006 consensus + supply panel (90.2M cap, maturity, fee burn, fee floor)
  * health / alert panel with an authority production matrix
  * selectable + zoomable history chart with CSV export
  * optionally, a loopback-only, token-gated ops panel (restart / diagnostics)

Safety: this process never holds a signing key, never writes to a node's data
directory, and binds to 127.0.0.1 by default. Ops actions are disabled unless
DASHBOARD_OPS_TOKEN is set, and every action is appended to ops-audit.jsonl.

Environment:
  DASHBOARD_BIND   bind address (default 127.0.0.1)
  DASHBOARD_PORT   TCP port (default 3001)
  DASHBOARD_OPS_TOKEN  enables the ops panel; ops stay disabled without it
  DASHBOARD_INTERVAL  poll interval in seconds (default 5)
"""
import csv
import hmac
import io
import json
import os
import re
import re
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG = os.path.join(HERE, "ops-audit.jsonl")

# ---------------------------------------------------------------------------
# Seed inventory. `authority_pk` is the public key each seed derives from its
# own operator key; it is public data and is only used to label the round-robin
# schedule in the UI. No private material lives in this file.
# ---------------------------------------------------------------------------
SEEDS = [
    {
        "name": "seed1",
        "label": "Seed 1",
        "host": "127.0.0.1",
        "ssh_key": None,
        "authority_pk": "d1a14d2c0d228b9d04a7f852404eefc6e1057698bf1b8105b6f0c6e443bce632",
        "service": "kovanica-seed1",
    },
    {
        "name": "seed2",
        "label": "Seed 2",
        "host": "76.13.250.65",
        "ssh_key": "/root/.ssh/seed2_deploy_key",
        "authority_pk": "a5e261ae6d582f31c37b727a3abda3f1b536822d62f7a7515721fcbeb7a1a662",
        "service": "kovanica-seed2",
    },
    {
        "name": "seed3",
        "label": "Seed 3",
        "host": "187.7.27.139",
        "ssh_key": "/root/.ssh/seed3_deploy_key",
        "authority_pk": "a1affed944312b0a8a1627b126d8162bba7a3abba7710bb349b621bac732266f",
        "service": "kovanica-seed3",
    },
]

SEED_NAMES = [s["name"] for s in SEEDS]
SEED_BY_NAME = {s["name"]: s for s in SEEDS}

# Genesis this dashboard expects. Divergence raises an alert.
EXPECTED_GENESIS = "93efd2d784c19e0ea74b53c4b1aec1aa070a2d6cd8042058d934b18a6e23ab0a"

PROBE_PATHS = ["/api/head", "/api/network", "/api/bootstrap", "/api/p2p"]
LOCAL_BASE = "http://127.0.0.1:8080"
REMOTE_BASE = "http://127.0.0.1:8080"

INTERVAL_SEC = int(os.environ.get("DASHBOARD_INTERVAL", "5"))
MAX_HISTORY = 180  # ~15 minutes at 5s

# RFC-006 canonical constants (display only; the node is authoritative).
RFC006 = {
    "max_supply_atoms": 9_020_000_000_000_000,  # 90.2M KVNC
    "maturity_blocks": 100,
    "fee_burn_pct": 75,
    "fee_producer_pct": 25,
    "era_len": 2_000_000,
    "genesis_subsidy_atoms": 1_000_000_000,  # 10 KVNC
    "decay_numerator": 3,
    "decay_denominator": 4,
    "fee_floor_divisor": 500_000,
}

HEX64 = re.compile(r"^[0-9a-f]{64}$")

state = {
    "started": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    "started_epoch": time.time(),
    "history": [],
    "latest": {},
    "health": {},
    "alerts": [],
    "consensus": {},
    "owner_matrix": {},
    "polls": 0,
    "divergent_polls": 0,
    "tips": {},
    "last_tip_change": {},
}

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Fetch layer
# ---------------------------------------------------------------------------
def _parse_probe_stream(raw):
    """Parse the `---<path>` framed output produced by the remote probe.

    Splits on the marker anywhere in the stream rather than only at a line
    start: `curl` does not terminate its body with a newline, so a marker can
    arrive glued to the end of the previous JSON body. A line-based parser
    silently loses that section and reports the seed as offline.
    """
    marker = re.compile(r"---(?=/api/[A-Za-z0-9/_.-]*)")
    starts = [m.start() for m in marker.finditer(raw)]
    parsed = {}
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(raw)
        chunk = raw[start:end]
        nl = chunk.find("\n")
        if nl == -1:
            continue
        path, body = chunk[3:nl].strip(), chunk[nl + 1:].strip()
        if not path.startswith("/api/"):
            continue
        try:
            parsed[path] = json.loads(body) if body else {}
        except Exception:
            parsed[path] = {}
    return parsed


def probe_local(seed):
    started = time.time()
    parsed = {}
    try:
        for path in PROBE_PATHS:
            with urllib.request.urlopen(LOCAL_BASE + path, timeout=5) as r:
                parsed[path] = json.loads(r.read().decode())
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "rtt_ms": int((time.time() - started) * 1000),
            "head": {},
            "network": {},
            "bootstrap": {},
            "p2p": {},
        }
    return {
        "ok": True,
        "error": None,
        "rtt_ms": int((time.time() - started) * 1000),
        "head": parsed.get("/api/head", {}),
        "network": parsed.get("/api/network", {}),
        "bootstrap": parsed.get("/api/bootstrap", {}),
        "p2p": parsed.get("/api/p2p", {}),
    }


def probe_remote(seed):
    started = time.time()
    # One SSH round-trip per remote seed: emit a framed curl for every probe path.
    # `echo` after every curl: curl writes no trailing newline, so without this
    # the next ---marker is glued onto the end of the previous JSON body and the
    # framed parser cannot find the section boundary.
    remote = "for p in " + " ".join(PROBE_PATHS) + "; do echo ---$p; curl -sS --max-time 8 " + REMOTE_BASE + "$p; echo; done"
    cmd = [
        "ssh", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5",
        "-i", seed["ssh_key"], f"root@{seed['host']}", remote,
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=45)
        parsed = _parse_probe_stream(out.decode(errors="replace"))
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "rtt_ms": int((time.time() - started) * 1000),
            "head": {},
            "network": {},
            "bootstrap": {},
            "p2p": {},
        }
    return {
        "ok": bool(parsed.get("/api/head")),
        "error": None if parsed.get("/api/head") else "no /api/head response",
        "rtt_ms": int((time.time() - started) * 1000),
        "head": parsed.get("/api/head", {}),
        "network": parsed.get("/api/network", {}),
        "bootstrap": parsed.get("/api/bootstrap", {}),
        "p2p": parsed.get("/api/p2p", {}),
    }


def probe_seed(seed):
    return probe_remote(seed) if seed["ssh_key"] else probe_local(seed)


def probe_all():
    # Probes are independent; run them concurrently so a slow remote seed
    # cannot delay the local one.
    results = {}
    threads = []

    def run(s):
        results[s["name"]] = probe_seed(s)

    for s in SEEDS:
        t = threading.Thread(target=run, args=(s,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=60)
    for s in SEEDS:
        results.setdefault(s["name"], {
            "ok": False, "error": "probe timeout", "rtt_ms": 0,
            "head": {}, "network": {}, "bootstrap": {}, "p2p": {},
        })
    return results


# ---------------------------------------------------------------------------
# Health tracking
# ---------------------------------------------------------------------------
def new_health():
    return {
        "polls": 0,
        "ok_polls": 0,
        "fail_streak": 0,
        "max_fail_streak": 0,
        "first_seen": time.time(),
        "last_ok": None,
        "last_error": None,
        "rtt_ms": None,
        "rtt_avg": None,
        "rtt_max": None,
        "blocks_prev": None,
        "advanced_last_poll": None,
    }


def update_health(health, name, probe):
    h = health.setdefault(name, new_health())
    h["polls"] += 1
    if probe.get("ok"):
        h["ok_polls"] += 1
        h["fail_streak"] = 0
        h["last_ok"] = time.time()
        h["last_error"] = None
        rtt = probe.get("rtt_ms")
        if isinstance(rtt, int):
            h["rtt_ms"] = rtt
            h["rtt_avg"] = rtt if h["rtt_avg"] is None else round(h["rtt_avg"] * 0.8 + rtt * 0.2, 1)
            h["rtt_max"] = rtt if h["rtt_max"] is None else max(h["rtt_max"], rtt)
        blocks = probe["head"].get("blocks")
        if isinstance(blocks, int):
            if h["blocks_prev"] is not None:
                h["advanced_last_poll"] = blocks > h["blocks_prev"]
            h["blocks_prev"] = blocks
    else:
        h["fail_streak"] += 1
        h["max_fail_streak"] = max(h["max_fail_streak"], h["fail_streak"])
        h["last_error"] = probe.get("error") or "unknown error"
        h["advanced_last_poll"] = None
    h["uptime_pct"] = round(100.0 * h["ok_polls"] / h["polls"], 2) if h["polls"] else 0.0
    return h


# ---------------------------------------------------------------------------
# RFC-006 consensus / supply derivation
# ---------------------------------------------------------------------------
def subsidy_at(height):
    """Display-side mirror of the RFC-006 geometric decay (s0 * (3/4)^era)."""
    if height < 0:
        return RFC006["genesis_subsidy_atoms"]
    era = height // RFC006["era_len"]
    subsidy = RFC006["genesis_subsidy_atoms"]
    num, den = RFC006["decay_numerator"], RFC006["decay_denominator"]
    for _ in range(era):
        subsidy = subsidy * num // den
        if subsidy == 0:
            break
    return subsidy


def fee_floor(subsidy):
    return max(1, subsidy // RFC006["fee_floor_divisor"])


def kvnc(atoms, decimals=8):
    if not isinstance(atoms, int):
        return "—"
    sign = "-" if atoms < 0 else ""
    v = abs(atoms)
    scale = 10 ** decimals
    whole, frac = divmod(v, scale)
    return f"{sign}{whole:,}.{frac:0{decimals}d}"


def owner_for_slot(slot, authority_list):
    """Classic PoA schedule: authorities[slot % len] (see kovanica-dag authority.rs)."""
    if not authority_list or slot is None:
        return None
    return authority_list[slot % len(authority_list)]


def build_consensus(latest):
    """Fold the per-seed probes into one consensus/supply view.

    Numbers reported by the node win; the RFC-006 constants are used as the
    reference for anything the node does not expose, and are flagged when the
    node disagrees.
    """
    healthy = {n: p for n, p in latest.items() if p.get("ok")}
    if not healthy:
        return {}

    ref_name = next(iter(healthy))
    head = healthy[ref_name]["head"]
    net = healthy[ref_name]["network"]
    boot = healthy[ref_name]["bootstrap"]

    height = head.get("blocks")
    slot = net.get("current_slot", head.get("current_slot"))
    auth_block = head.get("authority_set", {}) or {}
    authorities = sorted(auth_block.get("authorities", []) or [])
    threshold = auth_block.get("threshold")
    k = boot.get("k", 3)

    minted = boot.get("native_minted")
    total = boot.get("total", minted)
    burned = boot.get("burned", 0)
    cap = boot.get("max_supply", RFC006["max_supply_atoms"])
    circulating = boot.get("circulating")

    expected_subsidy = subsidy_at(height) if isinstance(height, int) else None
    node_subsidy = boot.get("subsidy")
    node_min_fee = head.get("min_fee", boot.get("min_fee"))
    expected_floor = fee_floor(expected_subsidy) if expected_subsidy else None

    owner = owner_for_slot(slot, authorities)
    owner_name = next(
        (n for n, s in SEED_BY_NAME.items() if s["authority_pk"] == owner), None
    )
    schedule = []
    if isinstance(slot, int) and authorities:
        for i in range(6):
            sl = slot + i
            pk = owner_for_slot(sl, authorities)
            nm = next((n for n, s in SEED_BY_NAME.items() if s["authority_pk"] == pk), "unknown")
            schedule.append({"slot": sl, "seed": nm, "pk": pk, "current": i == 0})

    cap_pct = round(100.0 * minted / cap, 6) if isinstance(minted, int) and cap else None

    return {
        "ref_seed": ref_name,
        "network": boot.get("network", head.get("network")),
        "genesis": head.get("genesis"),
        "height": height,
        "tip": head.get("tip"),
        "blue_score": net.get("blue_score"),
        "k": k,
        "threshold": threshold,
        "authority_count": len(authorities),
        "authorities": authorities,
        "authority_set_hash": auth_block.get("hash"),
        "slot": slot,
        "slot_duration_ms": head.get("slot_duration_ms") or net.get("slot_duration_ms"),
        "time_to_next_slot_ms": net.get("time_to_next_slot_ms"),
        "slot_owner": owner,
        "slot_owner_seed": owner_name,
        "schedule": schedule,
        "atom": boot.get("atom", head.get("atom", 100_000_000)),
        "decimals": boot.get("decimals", 8),
        "token": boot.get("token", "KVNC"),
        "minted_atoms": minted,
        "total_atoms": total,
        "circulating_atoms": circulating,
        "burned_atoms": burned,
        "max_supply_atoms": cap,
        "cap_pct": cap_pct,
        "remaining_atoms": (cap - minted) if isinstance(minted, int) and isinstance(cap, int) else None,
        "minted_kvnc": kvnc(minted),
        "remaining_kvnc": kvnc((cap - minted) if isinstance(minted, int) and isinstance(cap, int) else None),
        "era": (height // RFC006["era_len"]) if isinstance(height, int) else None,
        "subsidy_atoms": node_subsidy,
        "subsidy_expected_atoms": expected_subsidy,
        "subsidy_agrees": (node_subsidy == expected_subsidy) if node_subsidy is not None else None,
        "fee_floor_atoms": node_min_fee,
        "fee_floor_expected": expected_floor,
        "fee_floor_agrees": (node_min_fee == expected_floor) if node_min_fee is not None else None,
        "maturity_blocks": RFC006["maturity_blocks"],
        "fee_burn_pct": RFC006["fee_burn_pct"],
        "fee_producer_pct": RFC006["fee_producer_pct"],
        "finality_depth": head.get("finality_depth", boot.get("finality_depth")),
        "admission": boot.get("admission"),
        "poa_enabled": head.get("poa_enabled", boot.get("poa_enabled")),
        "pruning": {
            "payload": head.get("payload_pruning_depth"),
            "block": head.get("block_pruning_depth"),
        },
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def build_alerts(latest, health, consensus):
    alerts = []

    def add(level, code, message, seed=None):
        alerts.append({
            "level": level,       # error | warn | info
            "code": code,
            "message": message,
            "seed": seed,
        })

    genesis_seen = {}
    for name, probe in latest.items():
        if probe.get("ok"):
            genesis_seen[name] = (probe["head"] or {}).get("genesis")

    for name, g in genesis_seen.items():
        if g != EXPECTED_GENESIS:
            add("error", "genesis.mismatch",
                f"{name} reports genesis {str(g)[:12]}…, expected {EXPECTED_GENESIS[:12]}…", name)

    distinct = {g for g in genesis_seen.values() if g}
    if len(distinct) > 1:
        add("error", "genesis.divergent",
            "Seeds disagree on genesis: " + ", ".join(sorted(str(g)[:12] for g in distinct)))

    for name in SEED_NAMES:
        h = health.get(name) or {}
        probe = latest.get(name) or {}
        if not probe.get("ok"):
            add("error", "seed.offline",
                f"{name} unreachable — {probe.get('error') or 'no response'}", name)
        elif h.get("fail_streak"):
            add("warn", "seed.flapping",
                f"{name} recovered after {h['fail_streak']} failed probe(s)", name)

    # GHOSTDAG: the total block count is DAG size, NOT chain height. Blocks are
    # gossiped out of order and many become red (set-inclusion losers), so two
    # healthy nodes can hold the same blue-set tip while one has ingested more
    # red blocks. Chain position is the tip hash and blue score, so divergence
    # is judged on those and only on the block count as a stall hint.
    tips = {
        n: (latest[n]["head"] or {}).get("tip")
        for n in SEED_NAMES
        if (latest.get(n) or {}).get("ok") and (latest[n]["head"] or {}).get("tip")
    }
    disagree = len(set(tips.values())) > 1
    stuck = {n: ts for n, ts in (state.get("last_tip_change") or {}).items()}
    now = time.time()
    frozen = sorted(n for n, ts in stuck.items() if now - ts > max(60, INTERVAL_SEC * 8))

    if disagree:
        # In a live chain with multi-second slots, tips are almost never identical:
        # a node that just produced or just received sits one or two slots off its
        # peers. Tip inequality is therefore normal gossip-in-flight, and the real
        # stall signal is a tip that stops moving (see tip.frozen). Only escalate
        # when the disagreement has persisted for a long stretch, which suggests a
        # partition rather than slot skew.
        polls = state.get("divergent_polls", 0)
        detail = ", ".join(f"{n}={str(t)[:8]}" for n, t in sorted(tips.items()))
        if polls * INTERVAL_SEC >= 180:
            add("warn", "tip.persistent_divergence",
                f"Tips have differed for ~{polls * INTERVAL_SEC}s: {detail} — "
                f"check for a partition or a node stuck on a stale peer set")
        else:
            add("info", "tip.in_flight",
                f"Tips differ by a slot or two (normal gossip lag, {polls} poll(s)): {detail}")
    if frozen:
        worst = max(int(now - stuck[n]) for n in frozen)
        add("error", "tip.frozen",
            f"Tip has not moved for {worst}s on " + ", ".join(frozen)
            + " while peers are producing — node is stalled or partitioned")

    blue = {
        n: (latest[n]["network"] or {}).get("blue_score")
        for n in SEED_NAMES
        if (latest.get(n) or {}).get("ok")
        and isinstance((latest[n]["network"] or {}).get("blue_score"), int)
    }
    if len(blue) > 1:
        hi, lo = max(blue.values()), min(blue.values())
        lag = max(blue, key=lambda n: hi - blue[n])
        if hi - lo > 3:
            add("warn", "blue.drift",
                f"{lag} blue score is {hi - lo} behind the leader ({blue[lag]} vs {hi})", lag)
        if hi - lo > 40:
            add("error", "blue.stalled",
                f"{lag} has not advanced its blue set in {hi - lo} points — likely stalled", lag)

    # A node whose blue score matches the leader but whose DAG is much larger is
    # healthy: it simply holds more red blocks. Only surface the size gap when
    # the blue set also disagrees, otherwise it is storage, not a safety issue.
    blocks = {
        n: (latest[n]["head"] or {}).get("blocks")
        for n in SEED_NAMES
        if (latest.get(n) or {}).get("ok") and isinstance((latest[n]["head"] or {}).get("blocks"), int)
    }
    if len(blocks) > 1:
        hi = max(blocks, key=lambda n: blocks[n])
        lo = min(blocks, key=lambda n: blocks[n])
        gap = blocks[hi] - blocks[lo]
        if gap > 200:
            blue_agrees = len(blue) > 1 and (max(blue.values()) - min(blue.values())) <= 3
            if blue_agrees:
                add("info", "dag.red_block_gap",
                    f"{lo} holds {gap} fewer DAG blocks than {hi} but the blue set agrees — "
                    f"red-block storage lag, not a fork")
            elif disagree:
                add("error", "dag.size_divergent",
                    f"{lo} holds {blocks[lo]} DAG blocks vs {hi} at {blocks[hi]} while tips and "
                    f"blue score differ — possible reset or partition", lo)

    cons = consensus or {}
    auths = cons.get("authorities") or []
    for s in SEEDS:
        if auths and s["authority_pk"] not in auths:
            add("error", "authority.absent",
                f"{s['label']} key is not in the active authority set — it can never produce", s["name"])
    if auths and cons.get("threshold") is not None:
        online = sum(
            1 for n in SEED_NAMES
            if (latest.get(n) or {}).get("ok")
            and SEED_BY_NAME[n]["authority_pk"] in auths
        )
        if online < cons["threshold"]:
            add("error", "threshold.at_risk",
                f"Only {online} authority node(s) online, threshold is {cons['threshold']}")
        else:
            add("info", "threshold.ok",
                f"{online} of {cons['threshold']} required authority nodes online")
    if cons.get("subsidy_agrees") is False:
        add("warn", "subsidy.mismatch",
            f"Node subsidy {cons.get('subsidy_atoms')} differs from RFC-006 curve {cons.get('subsidy_expected_atoms')}")
    if cons.get("fee_floor_agrees") is False:
        add("warn", "fee_floor.mismatch",
            f"Node min_fee {cons.get('fee_floor_atoms')} differs from RFC-006 floor {cons.get('fee_floor_expected')}")
    if not auths and any((latest.get(n) or {}).get("ok") for n in SEED_NAMES):
        add("warn", "authority.unknown", "No authority set reported by any reachable seed")

    return alerts


def build_owner_matrix(history, consensus, latest):
    """For each scheduled authority, did its own seed advance when it owned a slot?

    Reads back over the retained history: whenever the slot owner changed to
    seed X, seed X's block count is expected to move on the next poll.
    """
    auths = consensus.get("authorities") or []
    if not auths:
        return {}
    matrix = {}
    for s in SEEDS:
        matrix[s["name"]] = {"pk": s["authority_pk"], "owned": 0, "produced": 0, "missed": 0, "pending": 0}
    for i in range(1, len(history)):
        prev, cur = history[i - 1], history[i]
        if not prev.get("slot_owner") or prev.get("slot_owner") == cur.get("slot_owner"):
            continue
        owner = prev.get("slot_owner")
        row = matrix.get(owner)
        if row is None:
            continue
        row["owned"] += 1
        p, c = prev.get("blocks") or {}, cur.get("blocks") or {}
        if owner in p and owner in c:
            if c[owner] > p[owner]:
                row["produced"] += 1
            else:
                row["missed"] += 1
        else:
            row["pending"] += 1
    return matrix


def poller():
    prev_tip = {n: None for n in SEED_NAMES}
    while True:
        snapshot = probe_all()
        with _lock:
            health = state["health"]
            for name in SEED_NAMES:
                update_health(health, name, snapshot.get(name) or {})
            consensus = build_consensus(snapshot)

            # GHOSTDAG: the tip is the maximum of the *known* blue set, so tips
            # legitimately differ for a poll or two while gossip settles. Only a
            # divergence that survives several polls is an incident, so track
            # how long the current disagreement has lasted.
            tips = {
                n: ((snapshot.get(n) or {}).get("head") or {}).get("tip")
                for n in SEED_NAMES if (snapshot.get(n) or {}).get("ok")
            }
            if len(set(tips.values())) > 1:
                state["divergent_polls"] += 1
            else:
                state["divergent_polls"] = 0
            state["tips"] = tips
            state["last_tip_change"] = {
                n: state["last_tip_change"].get(n, ts) if tips.get(n) == prev_tip.get(n)
                else ts
                for n, ts in [(n, time.time()) for n in SEED_NAMES]
            }
            prev_tip = dict(tips)

            state["latest"] = snapshot
            state["consensus"] = consensus
            state["alerts"] = build_alerts(snapshot, health, consensus)

            ts_epoch = time.time()
            row = {
                "t": int(ts_epoch),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_epoch)),
                "slot_owner": consensus.get("slot_owner_seed"),
            }
            for name in SEED_NAMES:
                probe = snapshot.get(name) or {}
                head = probe.get("head") or {}
                net = probe.get("network") or {}
                p2p = probe.get("p2p") or {}
                row[name] = {
                    "ok": 1 if probe.get("ok") else 0,
                    "b": head.get("blocks"),
                    "bl": net.get("blue_score"),
                    "p": len(p2p.get("peers", []) or []) if probe.get("ok") else None,
                    "r": probe.get("rtt_ms"),
                    "s": net.get("current_slot", head.get("current_slot")),
                }
            state["history"].append(row)
            if len(state["history"]) > MAX_HISTORY:
                state["history"].pop(0)
            state["owner_matrix"] = build_owner_matrix(state["history"], consensus, snapshot)
            state["polls"] += 1
        time.sleep(INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Ops layer (opt-in, token gated, allowlisted, audited)
# ---------------------------------------------------------------------------
OPS_TOKEN = os.environ.get("DASHBOARD_OPS_TOKEN", "").strip()

# action -> (description, needs_seed, destructive)
OPS_ACTIONS = {
    "ping": ("SSH reachability + service state + head height", False, False),
    "restart": ("systemctl restart the seed's kovanica unit", True, True),
    "diagnostics": ("collect a read-only diagnostics bundle", True, False),
}

DIAG_SCRIPT = r"""
set -u
echo "## systemd"
systemctl is-active {service} 2>&1 || true
systemctl show {service} -p ActiveState -p SubState -p NRestarts -p ExecMainStatus 2>&1 || true
echo "## head"
curl -sS --max-time 8 http://127.0.0.1:8080/api/head 2>&1 || true
echo
echo "## network"
curl -sS --max-time 8 http://127.0.0.1:8080/api/network 2>&1 || true
echo
echo "## last 40 log lines"
journalctl -u {service} -n 40 --no-pager 2>&1 || true
echo "## disk + data dir"
df -h / 2>&1 || true
du -sh {data} 2>&1 || true
"""


def ops_enabled():
    return bool(OPS_TOKEN)


def audit(entry):
    entry = dict(entry)
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def token_ok(candidate):
    if not OPS_TOKEN or not candidate:
        return False
    return hmac.compare_digest(str(candidate), OPS_TOKEN)


def run_ops(action, seed_name, client_ip):
    if action not in OPS_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}"}
    desc, needs_seed, destructive = OPS_ACTIONS[action]

    seed = None
    if needs_seed or action == "ping":
        if seed_name not in SEED_BY_NAME:
            return {"ok": False, "error": f"unknown seed {seed_name!r}"}
        seed = SEED_BY_NAME[seed_name]
    if not seed:
        seed = SEED_BY_NAME["seed1"]

    # seed1 is local; only remote seeds need an SSH hop.
    if seed["ssh_key"] is None and action in ("ping", "restart", "diagnostics"):
        targets = [
            ["/bin/sh", "-c", f"systemctl is-active {seed['service']} 2>&1"]
        ] if action == "ping" else (
            [["/bin/sh", "-c", f"systemctl restart {seed['service']}"]] if action == "restart"
            else [["/bin/sh", "-c", DIAG_SCRIPT.format(service=seed["service"], data="ROOT_DATA_PLACEHOLDER")]]
        )
    else:
        if action == "ping":
            remote = f"systemctl is-active {seed['service']} 2>&1; echo ---; curl -sS --max-time 8 http://127.0.0.1:8080/api/head 2>&1 | head -c 400"
        elif action == "restart":
            remote = f"sudo systemctl restart {seed['service']} && echo restarted"
        else:
            data_dir = ops_data_dir(seed)
            remote = DIAG_SCRIPT.format(service=seed["service"], data=data_dir)
        targets = [[
            "ssh", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10", "-i", seed["ssh_key"],
            f"root@{seed['host']}", remote,
        ]]

    started = time.time()
    try:
        proc = subprocess.run(
            targets[0], capture_output=True, timeout=(120 if action == "restart" else 60)
        )
        out = proc.stdout.decode(errors="replace")
        err = proc.stderr.decode(errors="replace")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        audit({"action": action, "seed": seed["name"], "client": client_ip,
               "result": "timeout", "destructive": destructive})
        return {"ok": False, "error": "command timed out", "output": ""}
    except Exception as e:
        audit({"action": action, "seed": seed["name"], "client": client_ip,
               "result": "error", "error": str(e), "destructive": destructive})
        return {"ok": False, "error": str(e), "output": ""}

    audit({
        "action": action, "seed": seed["name"], "client": client_ip,
        "rc": rc, "destructive": destructive,
        "duration_ms": int((time.time() - started) * 1000),
    })
    return {
        "ok": rc == 0,
        "rc": rc,
        "output": (out + ("\n[stderr]\n" + err if err.strip() else "")).strip(),
        "error": None if rc == 0 else f"exit code {rc}",
    }


def ops_data_dir(seed):
    """Data dir is per seed; local seed1 uses the known deployment path."""
    if seed["name"] == "seed1":
        return "/root/kovanica-data"
    return f"/var/lib/kovanica-{seed['name']}"


def history_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["epoch", "local_time", "seed", "online", "dag_blocks", "blue_score", "peers", "rtt_ms", "slot", "slot_owner"])
    for row in state["history"]:
        for name in SEED_NAMES:
            d = row.get(name) or {}
            w.writerow([
                row.get("t"), row.get("ts"), name, d.get("ok"),
                d.get("b"), d.get("bl"), d.get("p"), d.get("r"), d.get("s"),
                row.get("slot_owner"),
            ])
    return buf.getvalue()


def snapshot_payload():
    with _lock:
        return {
            "started": state["started"],
            "uptime_sec": int(time.time() - state["started_epoch"]),
            "interval_sec": INTERVAL_SEC,
            "polls": state["polls"],
            "seeds": [
                {"name": s["name"], "label": s["label"], "service": s["service"]} for s in SEEDS
            ],
            "ops": {"enabled": ops_enabled(), "actions": sorted(OPS_ACTIONS)},
            "latest": state["latest"],
            "health": state["health"],
            "alerts": state["alerts"],
            "consensus": state["consensus"],
            "owner_matrix": state["owner_matrix"],
            "history": state["history"],
            "rfc006": RFC006,
            "expected_genesis": EXPECTED_GENESIS,
            "tips": state["tips"],
            "divergent_polls": state["divergent_polls"],
            "tip_age_sec": {
                n: int(time.time() - ts)
                for n, ts in (state["last_tip_change"] or {}).items()
            },
        }


HTML = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kovanica Testnet · PoA Network Dashboard</title>
<style>
  :root {
    --bg:#f8fafc; --surface:#fff; --surface-2:#f1f5f9; --text:#0f172a; --text-muted:#64748b;
    --border:#e2e8f0; --accent:#0891b2; --accent-2:#d97706; --accent-glow:rgba(8,145,178,.18);
    --success:#10b981; --error:#ef4444; --warning:#f59e0b;
    --shadow:0 1px 3px 0 rgb(0 0 0/.05),0 1px 2px -1px rgb(0 0 0/.05);
    --shadow-lg:0 10px 25px -5px rgb(0 0 0/.08),0 8px 10px -6px rgb(0 0 0/.05);
    --radius:1rem;
    --font-display:"SF Pro Display","Segoe UI",system-ui,-apple-system,BlinkMacSystemFont,sans-serif;
    --font-body:"SF Pro Text","Segoe UI",system-ui,-apple-system,BlinkMacSystemFont,sans-serif;
    --font-mono:"SF Mono","Cascadia Code","Fira Code",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  [data-theme="dark"] {
    --bg:#030712; --surface:#0b1221; --surface-2:#111827; --text:#f8fafc; --text-muted:#94a3b8;
    --border:#1e293b; --accent:#22d3ee; --accent-2:#fbbf24; --accent-glow:rgba(34,211,238,.18);
    --success:#34d399; --error:#f87171; --warning:#fbbf24;
    --shadow:0 1px 3px 0 rgb(0 0 0/.25),0 1px 2px -1px rgb(0 0 0/.2);
    --shadow-lg:0 10px 40px -10px rgb(0 0 0/.6),0 4px 12px -4px rgb(0 0 0/.4);
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;font-family:var(--font-body);background:var(--bg);color:var(--text);line-height:1.5;
       min-height:100vh;background-image:radial-gradient(1200px 600px at 15% -10%,var(--accent-glow),transparent 60%)}
  .wrap{max-width:1400px;margin:0 auto;padding:1.5rem}
  @keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

  header{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap;margin-bottom:1.25rem}
  .brand-title{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
  .brand h1{margin:0;font-family:var(--font-display);font-size:1.5rem;font-weight:700;letter-spacing:-.02em}
  .brand p{margin:.125rem 0 0;font-size:.8125rem;color:var(--text-muted)}
  .env-badge{font-family:var(--font-mono);font-size:.6875rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
    padding:.1875rem .5rem;border-radius:.375rem;color:var(--warning);background:rgba(245,158,11,.12);
    border:1px solid rgba(245,158,11,.3);white-space:nowrap}
  .controls{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
  .meta{font-size:.8125rem;color:var(--text-muted);text-align:right}
  .theme-toggle,.btn{background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:.625rem;
    font-family:var(--font-body);font-size:.8125rem;padding:.4375rem .75rem;cursor:pointer;transition:transform .15s ease,box-shadow .2s ease,border-color .2s}
  .theme-toggle{width:2.25rem;height:2.25rem;display:grid;place-items:center;padding:0}
  .theme-toggle:hover,.btn:hover{transform:translateY(-1px);box-shadow:var(--shadow);border-color:var(--accent)}
  .btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
  .btn.primary{background:var(--accent);color:#04121a;border-color:transparent;font-weight:600}
  .btn.warn{border-color:var(--warning);color:var(--warning)}
  .btn.danger{border-color:var(--error);color:var(--error)}
  .btn.tiny{padding:.25rem .5rem;font-size:.75rem}

  .panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:1.25rem;
    box-shadow:var(--shadow);margin-bottom:1.25rem;animation:fadeUp .5s ease both}
  .panel h2{margin:0 0 1rem;font-size:1rem;font-weight:600;letter-spacing:-.01em;
    display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
  .panel h2 .hint{font-size:.75rem;font-weight:400;color:var(--text-muted)}
  .panel-actions{margin-left:auto;display:flex;gap:.375rem;flex-wrap:wrap}

  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem;margin-bottom:1.25rem}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:1.25rem;
    box-shadow:var(--shadow);transition:transform .2s ease,box-shadow .2s ease;animation:fadeUp .5s ease both}
  .card:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg)}
  .card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem}
  .seed-label{display:flex;align-items:center;gap:.5rem;font-size:.875rem;font-weight:600}
  .seed-dot{width:.5rem;height:.5rem;border-radius:50%;background:var(--error);flex:none}
  .seed-dot.ok{background:var(--success);animation:pulse 2s ease-in-out infinite}
  .status-badge{font-size:.6875rem;font-weight:600;padding:.125rem .4375rem;border-radius:.25rem;
    font-family:var(--font-mono);text-transform:uppercase;letter-spacing:.04em}
  .status-badge.ok{color:var(--success);background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3)}
  .status-badge.err{color:var(--error);background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.3)}
  .metric{font-family:var(--font-display);font-size:2rem;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums}
  .metric-label{font-size:.75rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em}
  .card-footer{display:flex;justify-content:space-between;margin-top:.875rem;padding-top:.625rem;
    border-top:1px solid var(--border);font-size:.75rem;color:var(--text-muted);font-family:var(--font-mono)}
  .error-text{margin-top:.5rem;font-size:.75rem;color:var(--error);font-family:var(--font-mono);word-break:break-word}

  .stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem}
  .stat{background:var(--surface-2);border:1px solid var(--border);border-radius:.75rem;padding:.75rem}
  .stat .k{font-size:.6875rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.25rem}
  .stat .v{font-family:var(--font-mono);font-size:1rem;font-weight:600;font-variant-numeric:tabular-nums;word-break:break-all}
  .stat .s{font-size:.6875rem;color:var(--text-muted);margin-top:.125rem}
  .stat.good .v{color:var(--success)} .stat.bad .v{color:var(--error)} .stat.warnv .v{color:var(--warning)}

  .bar{height:.5rem;border-radius:.25rem;background:var(--surface-2);border:1px solid var(--border);overflow:hidden;margin:.5rem 0 .25rem}
  .bar > span{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent-2));transition:width .5s ease}
  .bar-label{display:flex;justify-content:space-between;font-size:.75rem;color:var(--text-muted);font-family:var(--font-mono)}

  .slot-strip{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.25rem}
  .slot-chip{font-family:var(--font-mono);font-size:.75rem;padding:.4375rem .625rem;border-radius:.5rem;
    border:1px solid var(--border);background:var(--surface-2);display:flex;flex-direction:column;gap:.125rem;min-width:7.5rem}
  .slot-chip.current{border-color:var(--accent);background:var(--accent-glow)}
  .slot-chip .sl{font-size:.625rem;color:var(--text-muted)}
  .slot-chip .ow{font-weight:600}
  .countdown{height:.25rem;border-radius:.125rem;background:var(--surface-2);border:1px solid var(--border);overflow:hidden;margin-top:.5rem}
  .countdown > span{display:block;height:100%;background:var(--accent)}

  .alert-list{display:flex;flex-direction:column;gap:.5rem}
  .alert{display:flex;gap:.625rem;align-items:flex-start;padding:.625rem .75rem;border-radius:.625rem;
    border:1px solid var(--border);background:var(--surface-2);font-size:.8125rem;animation:fadeUp .3s ease both}
  .alert .lvl{font-family:var(--font-mono);font-size:.625rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
    padding:.125rem .375rem;border-radius:.25rem;flex:none;margin-top:.0625rem}
  .alert.error{border-color:rgba(239,68,68,.4)} .alert.error .lvl{color:var(--error);background:rgba(239,68,68,.14)}
  .alert.warn{border-color:rgba(245,158,11,.4)} .alert.warn .lvl{color:var(--warning);background:rgba(245,158,11,.14)}
  .alert.info{border-color:rgba(16,185,129,.35)} .alert.info .lvl{color:var(--success);background:rgba(16,185,129,.12)}
  .alert code{font-family:var(--font-mono);font-size:.6875rem;color:var(--text-muted);display:block;margin-top:.125rem}

  .table-wrap{overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-size:.8125rem}
  th{position:sticky;top:0;background:var(--surface);text-align:left;padding:.5rem .625rem;font-size:.6875rem;
    text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.5rem .625rem;border-bottom:1px solid var(--border);white-space:nowrap}
  tbody tr:hover{background:var(--surface-2)}
  code{font-family:var(--font-mono);font-size:.75rem}
  .ok-yes{color:var(--success);font-weight:600} .ok-no{color:var(--error);font-weight:600} .ok-na{color:var(--text-muted)}

  .legend{display:flex;gap:1rem;flex-wrap:wrap;margin-top:.75rem;font-size:.75rem;color:var(--text-muted)}
  .legend span{display:flex;align-items:center;gap:.375rem}
  .legend i{width:.625rem;height:.625rem;border-radius:.125rem;display:inline-block}
  .chart-shell{position:relative}
  #chart{width:100%;height:280px;display:block}
  .readout{position:absolute;top:.5rem;right:.5rem;font-family:var(--font-mono);font-size:.6875rem;
    color:var(--text-muted);background:var(--surface);border:1px solid var(--border);border-radius:.375rem;padding:.25rem .5rem;pointer-events:none}
  select{background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:.5rem;
    font-family:var(--font-body);font-size:.8125rem;padding:.3125rem .5rem;cursor:pointer}

  .ops-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:.75rem}
  .ops-card{background:var(--surface-2);border:1px solid var(--border);border-radius:.75rem;padding:.875rem}
  .ops-card h3{margin:0 0 .5rem;font-size:.875rem;display:flex;align-items:center;gap:.5rem}
  .ops-card .row{display:flex;gap:.375rem;flex-wrap:wrap;margin-top:.5rem}
  #opsLog{margin-top:.875rem;max-height:16rem;overflow-y:auto;background:var(--surface-2);border:1px solid var(--border);
    border-radius:.75rem;padding:.75rem;font-family:var(--font-mono);font-size:.6875rem;white-space:pre-wrap;word-break:break-word;line-height:1.45}
  .hidden{display:none !important}
  .warnbox{border:1px solid var(--warning);background:rgba(245,158,11,.1);color:var(--text);border-radius:.75rem;
    padding:.625rem .75rem;font-size:.75rem;margin-bottom:1rem;display:flex;gap:.5rem;align-items:center}
  .warnbox b{font-family:var(--font-mono);font-size:.6875rem;text-transform:uppercase;letter-spacing:.06em;color:var(--warning);flex:none}

  .dev-footer{margin-top:1.5rem;padding:.875rem 1rem;border-radius:.875rem;font-size:.8125rem;color:var(--text-muted);
    background:var(--surface);border:1px solid var(--border);text-align:center}
  .dev-footer strong{font-family:var(--font-mono);font-size:.6875rem;font-weight:700;letter-spacing:.08em;
    text-transform:uppercase;color:var(--warning);margin-right:.375rem}
  .dev-footer code{font-family:var(--font-mono);font-size:.75rem;color:var(--text)}

  @media (max-width:640px){
    .wrap{padding:1rem} .metric{font-size:1.5rem} #chart{height:220px}
    header{flex-direction:column} .meta{text-align:left}
  }
  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{animation-duration:.001ms !important;transition-duration:.001ms !important}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="brand-title">
        <h1>Kovanica</h1>
        <span class="env-badge" title="Non-production network. Not mainnet. No real value.">DEV</span>
      </div>
      <p>kovanica-testnet · PoA Dashboard · <span id="started">—</span></p>
    </div>
    <div class="controls">
      <div class="meta">
        <div id="last-updated">—</div>
        <div id="poll-info">—</div>
      </div>
      <select id="metricPick" title="Chart metric">
        <option value="bl" selected>Blue score (chain)</option>
        <option value="b">DAG blocks</option>
        <option value="s">Slot</option>
        <option value="p">Peers</option>
        <option value="r">Latency ms</option>
      </select>
      <select id="rangePick" title="Chart window">
        <option value="12">1 min</option>
        <option value="60" selected>5 min</option>
        <option value="180">15 min</option>
      </select>
      <a class="btn tiny" href="/api/history.csv" download>CSV</a>
      <button class="theme-toggle" id="themeToggle" aria-label="Toggle theme">☾</button>
    </div>
  </header>

  <div id="opsWarn" class="warnbox hidden"><b>Ops</b><span>Ops panel is enabled on this loopback dashboard. Actions are token-gated, allowlisted and written to <code>ops-audit.jsonl</code>.</span></div>

  <section class="panel" id="alertPanel">
    <h2>Alerts <span class="hint" id="alertSummary">—</span></h2>
    <div class="alert-list" id="alerts"><div class="alert info"><span class="lvl">info</span><span>Waiting for first probe…</span></div></div>
  </section>

  <section class="grid" id="cards"></section>

  <section class="panel">
    <h2>Consensus &amp; supply <span class="hint">RFC-006 · GHOSTDAG k=<span id="kVal">—</span> · PoA <span id="thrVal">—</span></span></h2>
    <div class="bar" title="Minted supply against the 90.2M hard cap"><span id="capBar" style="width:0%"></span></div>
    <div class="bar-label"><span id="capLeft">—</span><span id="capPct">—</span></div>
    <div class="stat-grid" id="supplyStats" style="margin-top:1rem"></div>

    <h2 style="margin-top:1.5rem">Slot schedule <span class="hint">classic PoA: authorities[slot % n]</span></h2>
    <div class="slot-strip" id="slots"></div>
    <div class="countdown" title="time to next slot"><span id="cdBar" style="width:0%"></span></div>
    <div class="bar-label"><span id="cdText">—</span><span id="slotDurText">—</span></div>
  </section>

  <section class="panel">
    <h2>Health <span class="hint">probe latency, uptime, blue-set drift</span></h2>
    <div class="table-wrap">
      <table id="health">
        <thead><tr>
          <th>Seed</th><th>State</th><th>Uptime</th><th>Latency</th><th>Latency max</th>
          <th>Blue score</th><th>Blue drift</th><th>DAG blocks</th><th>Tip</th><th>Peers</th>
          <th>Consecutive fails</th><th>Last error</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <p class="hint" style="margin-top:.5rem">
      GHOSTDAG note: <strong>DAG blocks</strong> is every block a node ingested, red set-inclusion losers
      included, so it is <em>not</em> chain height and a large spread is normal.
      <strong>Blue score</strong> and <strong>Tip</strong> are the real chain position.
    </p>
    <h2 style="margin-top:1.5rem">Authority production matrix <span class="hint">did the scheduled owner actually advance?</span></h2>
    <div class="table-wrap">
      <table id="ownerTable">
        <thead><tr><th>Authority</th><th>Node</th><th>Slots observed</th><th>Produced</th><th>Missed</th><th>Unobserved</th><th>Success rate</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </section>

  <section class="panel">
    <h2>History <span class="hint" id="histHint">—</span></h2>
    <div class="chart-shell">
      <canvas id="chart"></canvas>
      <div class="readout" id="readout"></div>
    </div>
    <div class="legend" id="legend"></div>
  </section>

  <section class="panel">
    <h2>Raw heads</h2>
    <div class="table-wrap">
      <table id="raw">
        <thead><tr><th>Seed</th><th>Genesis</th><th>Blue score</th><th>DAG blocks</th><th>Tip</th><th>Slot</th><th>Peers</th><th>Latency</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </section>

  <section class="panel hidden" id="opsPanel">
    <h2>Ops <span class="hint">loopback only · token gated · audited to ops-audit.jsonl</span></h2>
    <div class="ops-grid" id="opsGrid"></div>
    <div id="opsLog">No actions run yet.</div>
  </section>

  <footer class="dev-footer">
    <strong>DEV</strong> — non-production <code>kovanica-testnet</code>. No real value, no mainnet funds. Loopback-only; access via SSH tunnel.
  </footer>
</div>

<script>
const SEED_META = {
  seed1: { label: "Seed 1", color: "#22d3ee", light: "#0891b2" },
  seed2: { label: "Seed 2", color: "#34d399", light: "#059669" },
  seed3: { label: "Seed 3", color: "#fbbf24", light: "#d97706" },
};
let SEED_NAMES = Object.keys(SEED_META);
let snap = null;
let opsToken = "";

const $ = id => document.getElementById(id);
const fmt = n => (n === null || n === undefined || n === "") ? "—" : Number(n).toLocaleString();
const trunc = h => (!h) ? "—" : (h.length > 18 ? h.slice(0, 10) + "…" + h.slice(-8) : h);
const cssVar = n => getComputedStyle(document.body).getPropertyValue(n).trim();
const isLight = () => document.documentElement.getAttribute("data-theme") === "light";
const color = n => isLight() ? SEED_META[n].light : SEED_META[n].color;
const esc = s => String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function kvncFromAtoms(atoms, decimals) {
  if (typeof atoms !== "number") return "—";
  const d = decimals || 8;
  const neg = atoms < 0 ? "-" : "";
  const v = Math.abs(atoms);
  const whole = Math.floor(v / Math.pow(10, d));
  const frac = String(v % Math.pow(10, d)).padStart(d, "0");
  return neg + whole.toLocaleString() + "." + frac;
}

// --- theme -----------------------------------------------------------------
const themeToggle = $("themeToggle");
const stored = localStorage.getItem("kovanica-theme");
if (stored) document.documentElement.setAttribute("data-theme", stored);
const paintToggle = () => themeToggle.textContent = isLight() ? "☀" : "☾";
paintToggle();
themeToggle.addEventListener("click", () => {
  const next = isLight() ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("kovanica-theme", next);
  paintToggle();
  render();
});

// --- alerts ----------------------------------------------------------------
function renderAlerts() {
  const list = snap.alerts || [];
  const errors = list.filter(a => a.level === "error").length;
  const warns = list.filter(a => a.level === "warn").length;
  $("alertSummary").textContent = list.length
    ? `${errors} error · ${warns} warning · ${list.length} total`
    : "no active alerts";
  const box = $("alerts");
  if (!list.length) {
    box.innerHTML = `<div class="alert info"><span class="lvl">ok</span><span>All checks passing.</span></div>`;
    return;
  }
  box.innerHTML = list.map(a => `
    <div class="alert ${a.level}">
      <span class="lvl">${esc(a.level)}</span>
      <span>${esc(a.message)}<code>${esc(a.code)}${a.seed ? " · " + esc(a.seed) : ""}</code></span>
    </div>`).join("");
}

// --- cards -----------------------------------------------------------------
function renderCards() {
  const cards = $("cards");
  cards.innerHTML = "";
  SEED_NAMES.forEach((name, i) => {
    const probe = (snap.latest || {})[name] || {};
    const h = (snap.health || {})[name] || {};
    const head = probe.head || {};
    const net = probe.network || {};
    const p2p = probe.p2p || {};
    const ok = !!probe.ok;
    const peers = Array.isArray(p2p.peers) ? p2p.peers : [];
    // Chain position is the blue set, not the DAG size. `blocks` counts every
    // block a node ingested, red ones included, so it is not a lag indicator.
    const drift = (typeof net.blue_score === "number" && typeof snap.consensus.blue_score === "number")
      ? snap.consensus.blue_score - net.blue_score : null;
    cards.innerHTML += `
      <article class="card" style="animation-delay:${i * 80}ms;border-top:3px solid ${color(name)}">
        <div class="card-header">
          <div class="seed-label"><span class="seed-dot ${ok ? "ok" : "err"}"></span>${SEED_META[name].label}</div>
          <span class="status-badge ${ok ? "ok" : "err"}">${ok ? "Online" : "Offline"}</span>
        </div>
        <div class="metric">${fmt(net.blue_score != null ? net.blue_score : head.blocks)}</div>
        <div class="metric-label">blue score${net.blue_score == null ? " (fallback: blocks)" : ""}</div>
        ${probe.error ? `<div class="error-text">${esc(probe.error)}</div>` : ""}
        <div class="card-footer">
          <span>dag ${fmt(head.blocks)}</span>
          <span>rtt ${h.rtt_ms != null ? h.rtt_ms + "ms" : "—"}</span>
          <span>peers ${ok ? peers.length : "—"}</span>
        </div>
        <div class="card-footer" style="border-top:none;padding-top:.25rem;margin-top:.25rem">
          <span>uptime ${h.uptime_pct != null ? h.uptime_pct + "%" : "—"}</span>
          <span>blue ${drift == null ? "—" : (drift > 0 ? "-" + drift : "leader")}</span>
          <span>slot ${fmt(net.current_slot != null ? net.current_slot : head.current_slot)}</span>
        </div>
      </article>`;
  });
}

// --- consensus / supply ----------------------------------------------------
function renderConsensus() {
  const c = snap.consensus || {};
  $("kVal").textContent = c.k ?? "—";
  $("thrVal").textContent = c.threshold ? `${c.threshold}-of-${c.authority_count}` : "—";

  const pct = typeof c.cap_pct === "number" ? c.cap_pct : 0;
  $("capBar").style.width = Math.max(0, Math.min(100, pct)) + "%";
  $("capPct").textContent = pct ? pct.toFixed(4) + "% of cap" : "—";
  $("capLeft").textContent = "Remaining " + (c.remaining_kvnc || "—") + " " + (c.token || "KVNC");

  const d = c.decimals || 8;
  const stats = [
    { k: "Minted", v: c.minted_kvnc || "—", s: fmt(c.minted_atoms) + " atoms" },
    { k: "Circulating", v: kvncFromAtoms(c.circulating_atoms, d), s: "excludes immature coinbase" },
    { k: "Burned (fees)", v: kvncFromAtoms(c.burned_atoms, d), s: c.fee_burn_pct + "% of fees" },
    { k: "Hard cap", v: kvncFromAtoms(c.max_supply_atoms, d), s: "RFC-006 · never exceeded" },
    { k: "Block subsidy", v: kvncFromAtoms(c.subsidy_atoms, d) + " " + (c.token || ""), s: "era " + (c.era ?? "—") + " · s₀ = 10", cls: c.subsidy_agrees === false ? "bad" : "good" },
    { k: "Fee floor", v: fmt(c.fee_floor_atoms), s: "atoms/byte · max(1, subsidy/500k)", cls: c.fee_floor_agrees === false ? "bad" : "good" },
    { k: "Coinbase maturity", v: fmt(c.maturity_blocks) + " blocks", s: "hard rule" },
    { k: "Fee split", v: c.fee_burn_pct + "% / " + c.fee_producer_pct + "%", s: "burn / producer" },
    { k: "Finality depth", v: fmt(c.finality_depth), s: "reorg tolerance" },
    { k: "Authority set hash", v: trunc(c.authority_set_hash), s: "must match on every node" },
    { k: "Admission", v: c.admission || "—", s: c.poa_enabled ? "PoA enabled" : "PoA disabled" },
    { k: "Pruning depth", v: `${fmt(c.pruning?.payload)} / ${fmt(c.pruning?.block)}`, s: "payload / block" },
    { k: "Genesis", v: trunc(c.genesis), s: "expected " + trunc(snap.expected_genesis) },
  ];
  $("supplyStats").innerHTML = stats.map(s => `
    <div class="stat ${s.cls || ""}">
      <div class="k">${esc(s.k)}</div>
      <div class="v">${esc(s.v)}</div>
      <div class="s">${esc(s.s)}</div>
    </div>`).join("");

  const slots = c.schedule || [];
  $("slots").innerHTML = slots.map(s => {
    const nm = s.seed && SEED_META[s.seed] ? s.seed : null;
    return `<div class="slot-chip ${s.current ? "current" : ""}" ${nm ? `style="border-color:${color(nm)}"` : ""}>
      <span class="sl">${s.current ? "current" : "next"} · ${fmt(s.slot)}</span>
      <span class="ow" ${nm ? `style="color:${color(nm)}"` : ""}>${nm ? SEED_META[nm].label : "unknown"}</span>
    </div>`;
  }).join("");

  const dur = c.slot_duration_ms || 3000;
  const left = Math.max(0, c.time_to_next_slot_ms || 0);
  $("cdBar").style.width = Math.max(0, Math.min(100, (left / dur) * 100)) + "%";
  $("cdText").textContent = "next slot in " + (left / 1000).toFixed(2) + "s";
  $("slotDurText").textContent = "slot duration " + dur + "ms";
}

// --- health ----------------------------------------------------------------
function renderHealth() {
  const rows = [];
  const latest = snap.latest || {};
  const health = snap.health || {};
  const blues = SEED_NAMES.map(n => (latest[n] || {}).network?.blue_score).filter(v => typeof v === "number");
  const high = blues.length ? Math.max(...blues) : null;

  SEED_NAMES.forEach(name => {
    const probe = latest[name] || {};
    const h = health[name] || {};
    const blocks = (probe.head || {}).blocks;
    const blue = (probe.network || {}).blue_score;
    const drift = (high !== null && typeof blue === "number") ? high - blue : null;
    const tip = (probe.head || {}).tip;
    rows.push(`<tr>
      <td><strong style="color:${color(name)}">${SEED_META[name].label}</strong></td>
      <td><span class="status-badge ${probe.ok ? "ok" : "err"}">${probe.ok ? "Online" : "Offline"}</span></td>
      <td>${h.uptime_pct != null ? h.uptime_pct + "%" : "—"}</td>
      <td>${h.rtt_avg != null ? h.rtt_avg + " ms" : "—"}</td>
      <td>${h.rtt_max != null ? h.rtt_max + " ms" : "—"}</td>
      <td>${fmt(blue)}</td>
      <td>${drift == null ? "—" : (drift === 0 ? '<span class="ok-yes">leader</span>' : '-' + drift)}</td>
      <td>${fmt(blocks)}</td>
      <td><code>${tip ? trunc(tip) : "—"}</code></td>
      <td>${Array.isArray((probe.p2p || {}).peers) ? probe.p2p.peers.length : "—"}</td>
      <td>${h.fail_streak != null ? h.fail_streak : "—"}</td>
      <td>${h.last_error ? `<span style="color:var(--error)">${esc(h.last_error)}</span>` : "—"}</td>
    </tr>`);
  });
  document.querySelector("#health tbody").innerHTML = rows.join("");

  const matrix = snap.owner_matrix || {};
  const orows = SEED_NAMES.map(name => {
    const m = matrix[name] || {};
    const observed = (m.produced || 0) + (m.missed || 0);
    const rate = observed ? Math.round((m.produced / observed) * 100) : null;
    return `<tr>
      <td><code>${trunc(m.pk)}</code></td>
      <td><strong style="color:${color(name)}">${SEED_META[name].label}</strong></td>
      <td>${fmt(m.owned)}</td>
      <td class="${m.produced ? "ok-yes" : "ok-na"}">${fmt(m.produced)}</td>
      <td class="${m.missed ? "ok-no" : "ok-na"}">${fmt(m.missed)}</td>
      <td>${fmt(m.pending)}</td>
      <td>${rate == null ? "—" : `<span class="${rate === 100 ? "ok-yes" : "ok-no"}">${rate}%</span>`}</td>
    </tr>`;
  });
  document.querySelector("#ownerTable tbody").innerHTML = orows.join("");
}

// --- raw heads -------------------------------------------------------------
function renderRaw() {
  const rows = SEED_NAMES.map(name => {
    const probe = (snap.latest || {})[name] || {};
    const head = probe.head || {};
    const net = probe.network || {};
    const p2p = probe.p2p || {};
    const peers = Array.isArray(p2p.peers) ? p2p.peers : [];
    return `<tr>
      <td><strong style="color:${color(name)}">${SEED_META[name].label}</strong></td>
      <td><code>${trunc(head.genesis)}</code></td>
      <td>${fmt(net.blue_score)}</td>
      <td>${fmt(head.blocks)}</td>
      <td><code>${trunc(head.tip)}</code></td>
      <td>${fmt(net.current_slot != null ? net.current_slot : head.current_slot)}</td>
      <td>${probe.ok ? peers.length : "—"}</td>
      <td>${probe.rtt_ms != null ? probe.rtt_ms + " ms" : "—"}</td>
    </tr>`;
  });
  document.querySelector("#raw tbody").innerHTML = rows.join("");
}

// --- chart -----------------------------------------------------------------
const METRIC_LABEL = { b: "DAG blocks", bl: "blue score (chain position)", s: "slot", p: "peers", r: "latency ms" };

function drawChart() {
  const cvs = $("chart");
  const ctx = cvs.getContext("2d");
  const rect = cvs.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cvs.width = Math.max(1, Math.floor(rect.width * dpr));
  cvs.height = Math.max(1, Math.floor(rect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);

  const metric = $("metricPick").value;
  const windowSize = parseInt($("rangePick").value, 10);
  const all = (snap.history || []);
  const history = all.slice(Math.max(0, all.length - windowSize));
  $("histHint").textContent = `${METRIC_LABEL[metric]} · last ${history.length} of ${all.length} samples (${windowSize * (snap.interval_sec || 5)}s window)`;

  if (history.length < 2) {
    ctx.fillStyle = cssVar("--text-muted");
    ctx.font = "14px " + cssVar("--font-body");
    ctx.textAlign = "center";
    ctx.fillText("Collecting data…", rect.width / 2, rect.height / 2);
    $("legend").innerHTML = "";
    return;
  }

  let min = Infinity, max = -Infinity;
  for (const row of history) {
    for (const n of SEED_NAMES) {
      const v = (row[n] || {})[metric];
      if (typeof v === "number") { min = Math.min(min, v); max = Math.max(max, v); }
    }
  }
  if (!isFinite(min)) return;
  if (min === max) { min -= 1; max += 1; }

  const pad = { top: 24, right: 16, bottom: 32, left: 60 };
  const w = rect.width - pad.left - pad.right;
  const h = rect.height - pad.top - pad.bottom;
  const range = max - min;

  ctx.strokeStyle = cssVar("--border");
  ctx.lineWidth = 1;
  ctx.beginPath();
  const ySteps = 4;
  for (let i = 0; i <= ySteps; i++) {
    const y = pad.top + (h / ySteps) * i;
    ctx.moveTo(pad.left, y); ctx.lineTo(rect.width - pad.right, y);
  }
  ctx.stroke();

  ctx.fillStyle = cssVar("--text-muted");
  ctx.font = "11px " + cssVar("--font-mono");
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let i = 0; i <= ySteps; i++) {
    const value = max - (range / ySteps) * i;
    ctx.fillText(Math.round(value).toLocaleString(), pad.left - 10, pad.top + (h / ySteps) * i);
  }

  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (let i = 0; i <= 4; i++) {
    const idx = Math.round((history.length - 1) * (i / 4));
    const x = pad.left + (idx / (history.length - 1)) * w;
    const label = (history[idx].ts || "").split(" ")[1] || "";
    ctx.fillText(label, x, rect.height - pad.bottom + 8);
  }

  for (const name of SEED_NAMES) {
    const c = color(name);
    const points = [];
    for (let i = 0; i < history.length; i++) {
      const v = (history[i][name] || {})[metric];
      if (typeof v !== "number") continue;
      const x = pad.left + (i / (history.length - 1)) * w;
      const y = pad.top + h - ((v - min) / range) * h;
      points.push({ x, y, v });
    }
    if (points.length < 2) continue;

    const grad = ctx.createLinearGradient(0, pad.top, 0, rect.height - pad.bottom);
    grad.addColorStop(0, c + "33"); grad.addColorStop(1, c + "00");
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.moveTo(points[0].x, rect.height - pad.bottom);
    for (const p of points) ctx.lineTo(p.x, p.y);
    ctx.lineTo(points[points.length - 1].x, rect.height - pad.bottom);
    ctx.closePath(); ctx.fill();

    ctx.strokeStyle = c; ctx.lineWidth = 2.5; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.beginPath();
    points.forEach((p, i) => i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y));
    ctx.stroke();

    const last = points[points.length - 1];
    ctx.fillStyle = c;
    ctx.beginPath(); ctx.arc(last.x, last.y, 4, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = cssVar("--surface"); ctx.lineWidth = 2; ctx.stroke();
  }

  $("legend").innerHTML = SEED_NAMES.map(n => `<span><i style="background:${color(n)}"></i>${SEED_META[n].label}</span>`).join("")
    + `<span style="margin-left:auto">metric: ${METRIC_LABEL[metric]}</span>`;
}

// --- ops -------------------------------------------------------------------
function renderOps() {
  const enabled = snap.ops && snap.ops.enabled;
  $("opsPanel").classList.toggle("hidden", !enabled);
  $("opsWarn").classList.toggle("hidden", !enabled);
  if (!enabled) return;
  const grid = $("opsGrid");
  if (grid.childElementCount) return;
  grid.innerHTML = SEED_NAMES.map(name => `
    <div class="ops-card">
      <h3><span class="seed-dot ${((snap.latest || {})[name] || {}).ok ? "ok" : "err"}"></span>
        <span style="color:${color(name)}">${SEED_META[name].label}</span></h3>
      <div style="font-size:.6875rem;color:var(--text-muted);font-family:var(--font-mono)">${esc((snap.seeds.find(s => s.name === name) || {}).service || "")}</div>
      <div class="row">
        <button class="btn tiny" data-op="ping" data-seed="${name}">Ping</button>
        <button class="btn tiny" data-op="diagnostics" data-seed="${name}">Diagnostics</button>
        <button class="btn tiny danger" data-op="restart" data-seed="${name}">Restart</button>
      </div>
    </div>`).join("");

  const log = $("opsLog");
  const tokenRow = document.createElement("div");
  tokenRow.className = "row";
  tokenRow.style.marginBottom = ".5rem";
  tokenRow.innerHTML = `<input id="opsToken" type="password" placeholder="ops token" autocomplete="off"
      style="flex:1;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:.5rem;padding:.375rem .5rem;font-family:var(--font-mono);font-size:.75rem">
    <button class="btn tiny" id="opsTokenSave">Set</button>`;
  log.before(tokenRow);
  tokenRow.querySelector("#opsTokenSave").addEventListener("click", () => {
    opsToken = tokenRow.querySelector("#opsToken").value;
    appendOps("token set in browser memory only (not stored)");
  });
}

function appendOps(text) {
  const log = $("opsLog");
  if (log.dataset.first !== "1") { log.textContent = ""; log.dataset.first = "1"; }
  log.textContent += `\n[${new Date().toLocaleTimeString()}] ${text}`;
  log.scrollTop = log.scrollHeight;
}

async function runOp(action, seed) {
  if (!opsToken) { appendOps("set the ops token first"); return; }
  if (action === "restart" && !confirm(`Restart ${seed}'s kovanica service?\n\nThe node will go down for a few seconds and its peers will see a disconnect.`)) return;
  appendOps(`→ ${action} ${seed} …`);
  try {
    const r = await fetch("/api/ops", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Ops-Token": opsToken },
      body: JSON.stringify({ action, seed }),
    });
    const data = await r.json();
    appendOps(`← ${action} ${seed}: ${data.ok ? "ok" : "FAILED"}\n${(data.output || data.error || "").slice(0, 2000)}`);
  } catch (e) {
    appendOps(`← ${action} ${seed}: request failed — ${e}`);
  }
}

document.addEventListener("click", ev => {
  const btn = ev.target.closest("button[data-op]");
  if (btn) runOp(btn.dataset.op, btn.dataset.seed);
});

// --- render + poll ---------------------------------------------------------
function render() {
  if (!snap) return;
  $("started").textContent = snap.started || "—";
  $("last-updated").textContent = "Last update: " + new Date().toLocaleTimeString();
  $("poll-info").textContent = `poll #${snap.polls} · up ${fmt(Math.floor((snap.uptime_sec || 0) / 60))}m · every ${snap.interval_sec}s`;
  renderAlerts();
  renderCards();
  renderConsensus();
  renderHealth();
  renderRaw();
  drawChart();
  renderOps();
}

async function refresh() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    const data = await r.json();
    if (Array.isArray(data.seeds) && data.seeds.length) {
      data.seeds.forEach(s => { if (!SEED_META[s.name]) SEED_META[s.name] = { label: s.label, color: "#94a3b8", light: "#64748b" }; });
      SEED_NAMES = data.seeds.map(s => s.name);
    }
    snap = data;
    render();
  } catch (e) {
    console.error("refresh failed", e);
  }
}

$("metricPick").addEventListener("change", drawChart);
$("rangePick").addEventListener("change", drawChart);
window.addEventListener("resize", drawChart);

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "KovanicaDashboard/2"

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, code, payload):
        self._send(code, "application/json", json.dumps(payload, default=str).encode())

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode())
        elif path == "/api/state":
            self._json(200, snapshot_payload())
        elif path == "/api/healthz":
            errs = [a for a in state["alerts"] if a["level"] == "error"]
            self._json(200 if not errs else 503, {
                "ok": not errs,
                "polls": state["polls"],
                "errors": errs,
                "uptime_sec": int(time.time() - state["started_epoch"]),
            })
        elif path == "/api/history.csv":
            self._send(200, "text/csv; charset=utf-8", history_csv().encode(),
                       {"Content-Disposition": 'attachment; filename="kovanica-testnet-history.csv"'})
        elif path == "/api/ops/audit":
            # Read-only tail of the ops audit log (no secrets are stored there).
            if not ops_enabled():
                self._json(403, {"error": "ops disabled"})
                return
            try:
                with open(AUDIT_LOG) as f:
                    lines = f.read().splitlines()[-200:]
            except FileNotFoundError:
                lines = []
            out = []
            for line in lines:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
            self._json(200, {"entries": out})
        else:
            self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/api/ops":
            self._json(404, {"error": "not found", "path": path})
            return

        client_ip = self.client_address[0]
        # Defence in depth: ops are loopback-only even if the bind is changed.
        if client_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            audit({"action": "ops.reject", "client": client_ip, "reason": "non-loopback"})
            self._json(403, {"error": "ops is loopback-only"})
            return
        if not ops_enabled():
            self._json(403, {"error": "ops disabled: set DASHBOARD_OPS_TOKEN"})
            return

        header_token = self.headers.get("X-Ops-Token", "")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 4096:
            self._json(413, {"error": "payload too large"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            self._json(400, {"error": "invalid JSON body"})
            return

        if not token_ok(header_token) and not token_ok(body.get("token")):
            audit({"action": "ops.reject", "client": client_ip, "reason": "bad token"})
            self._json(401, {"error": "invalid ops token"})
            return

        action = str(body.get("action", ""))
        seed = str(body.get("seed", ""))
        result = run_ops(action, seed, client_ip)
        self._json(200 if result.get("ok") else 500, result)

    def log_message(self, fmt, *args):
        pass  # keep logs quiet


def main():
    port = int(os.environ.get("DASHBOARD_PORT", "3001"))
    bind = os.environ.get("DASHBOARD_BIND", "127.0.0.1")
    print(f"Dashboard: http://{bind}:{port}  interval={INTERVAL_SEC}s  ops={'enabled' if ops_enabled() else 'disabled'}")
    if ops_enabled():
        print("Ops panel enabled — token required for every action, audit log:", AUDIT_LOG)
    t = threading.Thread(target=poller, daemon=True)
    t.start()
    time.sleep(2)  # let the first probe land so the page is not empty
    server = HTTPServer((bind, port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
