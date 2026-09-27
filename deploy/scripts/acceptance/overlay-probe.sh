#!/bin/bash
# 在不改云网络的前提下，验证封装型 overlay 能不能跨这两个节点工作。
#
# VXLAN 走 UDP/4789，IP-in-IP 走 IP protocol 4 —— 有些 VPC 会拦这两类，
# 所以必须先实测哪个能过，再决定 CNI 怎么选。
set -uo pipefail

SELF_IP="$(hostname -I | awk '{print $1}')"
case "$SELF_IP" in
    172.18.80.94)  PEER=172.18.80.140 ; VX_A=10.244.99.1 ; IPIP_A=10.244.98.1 ;;
    172.18.80.140) PEER=172.18.80.94  ; VX_A=10.244.99.2 ; IPIP_A=10.244.98.2 ;;
    *) echo "unexpected host $SELF_IP" >&2; exit 1 ;;
esac

echo "== host $SELF_IP peer $PEER"

echo
echo "== VXLAN (UDP 4789)"
ip link del vx0 2>/dev/null || true
if ip link add vx0 type vxlan id 42 remote "$PEER" local "$SELF_IP" dstport 4789 dev eth0 2>/tmp/vxerr; then
    ip addr add "$VX_A/24" dev vx0
    ip link set vx0 up
    echo "   vx0 up ($VX_A/24)"
else
    echo "   vxlan setup failed: $(cat /tmp/vxerr)"
fi

echo
echo "== IP-in-IP (proto 4)"
ip tunnel del tun0 2>/dev/null || true
if ip tunnel add tun0 mode ipip remote "$PEER" local "$SELF_IP" 2>/tmp/iperr; then
    ip addr add "$IPIP_A/24" dev tun0
    ip link set tun0 up
    echo "   tun0 up ($IPIP_A/24)"
else
    echo "   ipip setup failed: $(cat /tmp/iperr)"
fi

echo
echo "== interfaces"
ip -4 -o addr show vx0 2>/dev/null | sed 's/^/   /'
ip -4 -o addr show tun0 2>/dev/null | sed 's/^/   /'
