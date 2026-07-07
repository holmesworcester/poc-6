#!/usr/bin/env bash
# NAT hole-punch test using Linux network namespaces.
#
# Topology:
#
#   peer_a (10.1.0.2)           peer_b (10.2.0.2)
#       |                           |
#   [veth 10.1.0.0/24]         [veth 10.2.0.0/24]
#       |                           |
#   nat_a (10.1.0.1 / 10.10.0.2)   nat_b (10.2.0.1 / 10.20.0.2)
#       |                           |
#   [veth 10.10.0.0/24]        [veth 10.20.0.0/24]
#       |                           |
#   router (10.10.0.1)         router (10.20.0.1)
#
# NAT modes:
#   cone      - Endpoint-Independent Mapping (nftables masquerade persistent)
#               Same external port for all destinations from same internal port.
#               Hole-punchable.
#   symmetric - Endpoint-Dependent Mapping (nftables masquerade random,fully-random)
#               Different external port per destination.
#               NOT hole-punchable (needs relay).
#
# Usage:
#   sudo ./test_netns.sh              Run with cone NAT (default, should succeed)
#   sudo ./test_netns.sh --symmetric  Run with symmetric NAT (should fail)
#   sudo ./test_netns.sh --cleanup    Remove namespaces

set -euo pipefail

BINARY_DIR="${BINARY_DIR:-./target/debug}"
MODE="${1:-cone}"
CLEANUP_ONLY=false

if [[ "$MODE" == "--cleanup" ]]; then
    CLEANUP_ONLY=true
fi

# Temp files for peer output
PEER_A_LOG=$(mktemp /tmp/peer_a.XXXXXX.log)
PEER_B_LOG=$(mktemp /tmp/peer_b.XXXXXX.log)
INTRO_LOG=$(mktemp /tmp/intro.XXXXXX.log)

cleanup() {
    [[ -n "${CHARLIE_PID:-}" ]] && kill "$CHARLIE_PID" 2>/dev/null || true
    [[ -n "${PEER_A_PID:-}" ]] && kill "$PEER_A_PID" 2>/dev/null || true
    [[ -n "${PEER_B_PID:-}" ]] && kill "$PEER_B_PID" 2>/dev/null || true
    # Kill any socat processes we spawned in the router namespace
    [[ -n "${SOCAT1_PID:-}" ]] && kill "$SOCAT1_PID" 2>/dev/null || true
    [[ -n "${SOCAT2_PID:-}" ]] && kill "$SOCAT2_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    for ns in peer_a peer_b nat_a nat_b router; do
        ip netns del "$ns" 2>/dev/null || true
    done
    rm -f "$PEER_A_LOG" "$PEER_B_LOG" "$INTRO_LOG" 2>/dev/null || true
}

trap cleanup EXIT

if $CLEANUP_ONLY; then
    cleanup
    exit 0
fi

# ---- Install optional tools (best-effort) ----
for tool in conntrack socat; do
    if ! command -v "$tool" &>/dev/null; then
        echo "Installing $tool..."
        apt-get install -y "${tool/conntrack/conntrack}" 2>/dev/null || true
    fi
done
HAS_CONNTRACK=$(command -v conntrack &>/dev/null && echo true || echo false)
HAS_SOCAT=$(command -v socat &>/dev/null && echo true || echo false)

echo "=== Creating network namespaces ==="
cleanup 2>/dev/null || true

for ns in peer_a peer_b nat_a nat_b router; do
    ip netns add "$ns"
    ip netns exec "$ns" ip link set lo up
done

# ---- peer_a <-> nat_a (private side, 10.1.0.0/24) ----
ip link add pa-eth0 type veth peer name na-priv
ip link set pa-eth0 netns peer_a
ip link set na-priv netns nat_a

ip netns exec peer_a ip addr add 10.1.0.2/24 dev pa-eth0
ip netns exec peer_a ip link set pa-eth0 up
ip netns exec peer_a ip route add default via 10.1.0.1

ip netns exec nat_a ip addr add 10.1.0.1/24 dev na-priv
ip netns exec nat_a ip link set na-priv up

# ---- nat_a <-> router (public side, 10.10.0.0/24) ----
ip link add na-pub type veth peer name rtr-a
ip link set na-pub netns nat_a
ip link set rtr-a netns router

ip netns exec nat_a ip addr add 10.10.0.2/24 dev na-pub
ip netns exec nat_a ip link set na-pub up
ip netns exec nat_a ip route add default via 10.10.0.1

ip netns exec router ip addr add 10.10.0.1/24 dev rtr-a
ip netns exec router ip link set rtr-a up

# ---- peer_b <-> nat_b (private side, 10.2.0.0/24) ----
ip link add pb-eth0 type veth peer name nb-priv
ip link set pb-eth0 netns peer_b
ip link set nb-priv netns nat_b

ip netns exec peer_b ip addr add 10.2.0.2/24 dev pb-eth0
ip netns exec peer_b ip link set pb-eth0 up
ip netns exec peer_b ip route add default via 10.2.0.1

ip netns exec nat_b ip addr add 10.2.0.1/24 dev nb-priv
ip netns exec nat_b ip link set nb-priv up

# ---- nat_b <-> router (public side, 10.20.0.0/24) ----
ip link add nb-pub type veth peer name rtr-b
ip link set nb-pub netns nat_b
ip link set rtr-b netns router

ip netns exec nat_b ip addr add 10.20.0.2/24 dev nb-pub
ip netns exec nat_b ip link set nb-pub up
ip netns exec nat_b ip route add default via 10.20.0.1

ip netns exec router ip addr add 10.20.0.1/24 dev rtr-b
ip netns exec router ip link set rtr-b up

# ---- Enable forwarding ----
for ns in nat_a nat_b router; do
    ip netns exec "$ns" sysctl -qw net.ipv4.ip_forward=1
done

# ---- Disable rp_filter (reverse-path filtering) ----
# rp_filter silently drops packets whose source address doesn't match the
# expected interface. In a multi-homed NAT namespace this causes mysterious
# packet loss. Real NAT routers typically have this disabled or in loose mode.
for ns in nat_a nat_b router; do
    ip netns exec "$ns" sysctl -qw net.ipv4.conf.all.rp_filter=0
    ip netns exec "$ns" sysctl -qw net.ipv4.conf.default.rp_filter=0
    # Also set per-interface (takes effect from max of all/interface)
    for iface in $(ip netns exec "$ns" ls /proc/sys/net/ipv4/conf/ 2>/dev/null); do
        ip netns exec "$ns" sysctl -qw "net.ipv4.conf.$iface.rp_filter=0" 2>/dev/null || true
    done
done

# ---- Configure NAT (nftables) ----
if [[ "$MODE" == "--symmetric" ]]; then
    echo "Configuring SYMMETRIC NAT (endpoint-dependent mapping)"
    echo "  Hole-punching should FAIL (different external port per destination)"
    # masquerade random,fully-random = skip port preservation, random port per flow.
    # Plain masquerade preserves source ports when possible (giving EIM/cone on
    # modern kernels). The random flags force the kernel to pick a new random
    # port for each conntrack entry, producing true EDM/symmetric behavior.
    for nat_ns in nat_a nat_b; do
        if [[ "$nat_ns" == "nat_a" ]]; then PUB=na-pub; else PUB=nb-pub; fi
        ip netns exec "$nat_ns" nft add table inet nat
        ip netns exec "$nat_ns" nft add chain inet nat postrouting \
            '{ type nat hook postrouting priority 100 ; }'
        ip netns exec "$nat_ns" nft add rule inet nat postrouting \
            oifname "$PUB" masquerade random,fully-random
    done
else
    echo "Configuring CONE NAT (endpoint-independent mapping)"
    echo "  Hole-punching should SUCCEED (same external port for all destinations)"
    # masquerade persistent = kernel's built-in EIM support (since 3.18)
    # Same external port for all destinations from the same internal source port.
    for nat_ns in nat_a nat_b; do
        if [[ "$nat_ns" == "nat_a" ]]; then PUB=na-pub; else PUB=nb-pub; fi
        ip netns exec "$nat_ns" nft add table inet nat
        ip netns exec "$nat_ns" nft add chain inet nat postrouting \
            '{ type nat hook postrouting priority 100 ; }'
        ip netns exec "$nat_ns" nft add rule inet nat postrouting \
            oifname "$PUB" masquerade persistent
    done
fi

# ---- NAT firewall (nftables) ----
# Only allow outbound-initiated traffic; inbound must be established/related.
for nat_ns in nat_a nat_b; do
    if [[ "$nat_ns" == "nat_a" ]]; then
        PUB=na-pub; PRIV=na-priv
    else
        PUB=nb-pub; PRIV=nb-priv
    fi
    ip netns exec "$nat_ns" nft add table inet filter
    ip netns exec "$nat_ns" nft add chain inet filter forward \
        '{ type filter hook forward priority 0 ; policy drop ; }'
    ip netns exec "$nat_ns" nft add rule inet filter forward \
        iifname "$PRIV" oifname "$PUB" accept
    ip netns exec "$nat_ns" nft add rule inet filter forward \
        iifname "$PUB" oifname "$PRIV" ct state established,related accept
done

# ---- Add WAN latency simulation ----
# 25ms delay each direction = 50ms RTT (typical broadband WAN)
# 5ms jitter adds realism and stresses timing assumptions.
ip netns exec nat_a tc qdisc add dev na-pub root netem delay 25ms 5ms
ip netns exec nat_b tc qdisc add dev nb-pub root netem delay 25ms 5ms

echo ""
echo "=== Verifying connectivity ==="
ip netns exec peer_a ping -c1 -W2 10.10.0.1 > /dev/null 2>&1 \
    && echo "  peer_a -> router: OK" || echo "  peer_a -> router: FAIL"
ip netns exec peer_b ping -c1 -W2 10.20.0.1 > /dev/null 2>&1 \
    && echo "  peer_b -> router: OK" || echo "  peer_b -> router: FAIL"
if ip netns exec peer_a ping -c1 -W1 10.2.0.2 > /dev/null 2>&1; then
    echo "  peer_a -> peer_b (cross-NAT): REACHABLE (NAT NOT WORKING!)"
    exit 1
else
    echo "  peer_a -> peer_b (cross-NAT): BLOCKED (NAT working)"
fi

# ---- NAT type verification (STUN-like probe) ----
# Send UDP from the same source port to two different destinations on the router.
# If the NAT assigns the same external port for both → EIM (cone).
# If different external ports → EDM (symmetric).
echo ""
echo "=== Verifying NAT type ==="

NAT_TYPE_OK=true

if $HAS_SOCAT && $HAS_CONNTRACK; then
    # Start two UDP echo listeners in the router namespace
    ip netns exec router socat UDP-LISTEN:5001,fork,reuseaddr PIPE &
    SOCAT1_PID=$!
    ip netns exec router socat UDP-LISTEN:5002,fork,reuseaddr PIPE &
    SOCAT2_PID=$!
    sleep 0.3

    for peer_info in "peer_a:nat_a:10.10.0.1:9000" "peer_b:nat_b:10.20.0.1:9000"; do
        IFS=: read -r PEER_NS NAT_NS ROUTER_IP SPORT <<< "$peer_info"
        PEER_LABEL=$(echo "$PEER_NS" | tr _ ' ')

        # Send a UDP packet from the peer to router:5001 and router:5002
        # Use source port $SPORT to match what the holepunch endpoint will use
        ip netns exec "$PEER_NS" bash -c \
            "echo probe1 | socat - UDP:${ROUTER_IP}:5001,sourceport=${SPORT},reuseaddr" 2>/dev/null &
        sleep 0.3
        ip netns exec "$PEER_NS" bash -c \
            "echo probe2 | socat - UDP:${ROUTER_IP}:5002,sourceport=${SPORT},reuseaddr" 2>/dev/null &
        sleep 0.5

        # Read conntrack table in the NAT namespace
        CT_OUTPUT=$(ip netns exec "$NAT_NS" conntrack -L -p udp 2>/dev/null || true)

        # Extract the mapped external port from the conntrack reply direction.
        # Conntrack format: <orig> src=A dst=B sport=X dport=Y <reply> src=B dst=C sport=Y dport=MAPPED
        # The reply dport is the NAT-mapped external port. We match on original dport (5001/5002)
        # and extract the reply dport by looking at the second occurrence of dport= in the line.
        PORT_TO_5001=$(echo "$CT_OUTPUT" | grep "dport=5001" | grep -oP 'dport=\K[0-9]+' | tail -1)
        PORT_TO_5002=$(echo "$CT_OUTPUT" | grep "dport=5002" | grep -oP 'dport=\K[0-9]+' | tail -1)

        if [[ -n "$PORT_TO_5001" && -n "$PORT_TO_5002" ]]; then
            if [[ "$PORT_TO_5001" == "$PORT_TO_5002" ]]; then
                echo "  $PEER_LABEL: EIM confirmed (port $PORT_TO_5001 for both destinations)"
            else
                echo "  $PEER_LABEL: EDM confirmed (port $PORT_TO_5001 -> :5001, port $PORT_TO_5002 -> :5002)"
            fi

            # Validate against expected mode
            if [[ "$MODE" != "--symmetric" && "$PORT_TO_5001" != "$PORT_TO_5002" ]]; then
                echo "  WARNING: Expected EIM (cone) but got EDM! NAT config may be wrong."
                NAT_TYPE_OK=false
            elif [[ "$MODE" == "--symmetric" && "$PORT_TO_5001" == "$PORT_TO_5002" ]]; then
                echo "  WARNING: Expected EDM (symmetric) but got EIM! NAT config may be wrong."
                NAT_TYPE_OK=false
            fi
        else
            echo "  $PEER_LABEL: Could not read conntrack entries (port_5001='$PORT_TO_5001' port_5002='$PORT_TO_5002')"
            echo "  Conntrack dump:"
            echo "$CT_OUTPUT" | head -20 | sed 's/^/    /'
        fi
    done

    # Clean up verification listeners and flush conntrack
    kill "$SOCAT1_PID" "$SOCAT2_PID" 2>/dev/null || true
    wait "$SOCAT1_PID" "$SOCAT2_PID" 2>/dev/null || true
    unset SOCAT1_PID SOCAT2_PID
    # Flush conntrack entries so the holepunch starts clean
    for nat_ns in nat_a nat_b; do
        ip netns exec "$nat_ns" conntrack -F 2>/dev/null || true
    done

    if ! $NAT_TYPE_OK; then
        echo ""
        echo "NAT type verification FAILED — test preconditions not met."
        echo "RESULT: INVALID"
        exit 1
    fi
else
    echo "  (skipping: conntrack and/or socat not installed)"
    echo "  Install with: apt-get install -y conntrack socat"
fi

echo ""
echo "=== Starting QUIC hole-punch test ==="

# Charlie (introducer) — runs in router namespace, no NAT, reachable by both
ip netns exec router env RUST_LOG=info "$BINARY_DIR/quic-holepunch" \
    --peer-id charlie --bind 0.0.0.0:4433 \
    > "$INTRO_LOG" 2>&1 &
CHARLIE_PID=$!
sleep 1

# Dump conntrack state during the test (background, every 2s)
if $HAS_CONNTRACK; then
    (
        for i in $(seq 1 8); do
            sleep 2
            echo "--- conntrack snapshot at +${i}*2s ---"
            for nat_ns in nat_a nat_b; do
                echo "  [$nat_ns]:"
                ip netns exec "$nat_ns" conntrack -L -p udp 2>/dev/null | sed 's/^/    /' || true
            done
        done
    ) &
    CT_DUMP_PID=$!
fi

# Alice — behind nat_a, connects to charlie
ip netns exec peer_a env RUST_LOG=info "$BINARY_DIR/quic-holepunch" \
    --peer-id alice --bind 0.0.0.0:9000 --connect 10.10.0.1:4433 \
    > "$PEER_A_LOG" 2>&1 &
PEER_A_PID=$!
sleep 0.3

# Bob — behind nat_b, connects to charlie
ip netns exec peer_b env RUST_LOG=info "$BINARY_DIR/quic-holepunch" \
    --peer-id bob --bind 0.0.0.0:9000 --connect 10.20.0.1:4433 \
    > "$PEER_B_LOG" 2>&1 &
PEER_B_PID=$!

echo "Waiting for hole-punch attempt (20s, with 50ms simulated RTT)..."
sleep 20

echo ""
echo "=== Charlie (introducer) output ==="
cat "$INTRO_LOG"

echo ""
echo "=== Peer A (alice) output ==="
cat "$PEER_A_LOG"

echo ""
echo "=== Peer B (bob) output ==="
cat "$PEER_B_LOG"

# Final conntrack dump
if $HAS_CONNTRACK; then
    kill "$CT_DUMP_PID" 2>/dev/null || true
    echo ""
    echo "=== Final conntrack state ==="
    for nat_ns in nat_a nat_b; do
        echo "  [$nat_ns]:"
        ip netns exec "$nat_ns" conntrack -L -p udp 2>/dev/null | sed 's/^/    /' || true
    done
fi

echo ""
echo "=== Test complete ==="

# ---- PASS/FAIL determination ----
if grep -q "HOLE PUNCH SUCCESS" "$PEER_A_LOG" || grep -q "HOLE PUNCH SUCCESS" "$PEER_B_LOG"; then
    RESULT="PASS"
    echo "RESULT: PASS - hole punch succeeded through NAT"
    if grep -q "connection verified" "$PEER_A_LOG" || grep -q "connection verified" "$PEER_B_LOG"; then
        echo "  Data exchange verified."
    fi
else
    RESULT="FAIL"
    echo "RESULT: FAIL - hole punch did not succeed"
    if grep -q "HOLE PUNCH FAILED" "$PEER_A_LOG" || grep -q "HOLE PUNCH FAILED" "$PEER_B_LOG"; then
        echo "  Peers reported explicit failure."
    fi
fi

# ---- Expected result check ----
if [[ "$MODE" == "--symmetric" ]]; then
    if [[ "$RESULT" == "FAIL" ]]; then
        echo "  (Expected: FAIL for symmetric NAT — this is correct behavior)"
    else
        echo "  (Expected: FAIL for symmetric NAT — unexpected SUCCESS!)"
    fi
else
    if [[ "$RESULT" == "PASS" ]]; then
        echo "  (Expected: PASS for cone NAT — this is correct behavior)"
    else
        echo "  (Expected: PASS for cone NAT — unexpected FAIL!)"
    fi
fi
