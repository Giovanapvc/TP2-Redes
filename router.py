#!/usr/bin/env python3

import argparse
import json
import queue
import random
import socket
import sys
import threading
import time
from typing import Dict, Any, List, Optional

UDP_PORT = 55151       
INF = 1_000_000         
AGING_FACTOR = 4        

def jdump(msg: Dict[str, Any]) -> bytes:
    return json.dumps(msg).encode()

def jload(raw: bytes) -> Dict[str, Any]:
    return json.loads(raw.decode())

def mk_data(src: str, dst: str, payload: str) -> Dict[str, Any]:
    return {"type": "data", "source": src, "destination": dst, "payload": payload}

def mk_update(src: str, dst: str, distances: Dict[str, int]) -> Dict[str, Any]:
    return {"type": "update", "source": src, "destination": dst, "distances": distances}

def mk_trace(src: str, dst: str, routers: List[str]) -> Dict[str, Any]:
    return {"type": "trace", "source": src, "destination": dst, "routers": routers}

def mk_control(src: str, dst: str, reason: str, original: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "control",
        "source": src,
        "destination": dst,
        "reason": reason,
        "original": original,
    }

class Neighbor:
    def __init__(self, weight: int):
        self.weight = weight
        self.last_seen = time.time()

class LinkTable:
    def __init__(self, aging_seconds: float):
        self._nbrs: Dict[str, Neighbor] = {}
        self._aging = aging_seconds

    def add(self, ip: str, weight: int) -> None:
        self._nbrs[ip] = Neighbor(weight)

    def remove(self, ip: str) -> None:
        self._nbrs.pop(ip, None)

    def weight(self, ip: str) -> Optional[int]:
        n = self._nbrs.get(ip)
        return n.weight if n else None

    def touch(self, ip: str) -> None:
        if ip in self._nbrs:
            self._nbrs[ip].last_seen = time.time()

    def expire(self) -> List[str]:
        now = time.time()
        dead = [ip for ip, n in self._nbrs.items() if now - n.last_seen > self._aging]
        for ip in dead:
            self.remove(ip)
        return dead

    def neighbors(self) -> List[str]:
        return list(self._nbrs.keys())

class RoutingTable:

    def __init__(self, my_ip: str):
        self._routes: Dict[str, tuple[int, set[str]]] = {my_ip: (0, {my_ip})}

    def next_hop(self, dst: str) -> Optional[str]:
        e = self._routes.get(dst)
        if not e:
            return None
        dist, hops = e
        return random.choice(tuple(hops))

    def distance(self, dst: str) -> Optional[int]:
        e = self._routes.get(dst)
        return e[0] if e else None

    def export(self, to_neighbor: str) -> Dict[str, int]:
        res: Dict[str, int] = {}
        for d, (cost, hops) in self._routes.items():
            if to_neighbor not in hops:  # Split Horizon
                res[d] = cost
        return res

    def learn_neighbor_vector(self, nbr: str, w_nbr: int, vector: Dict[str, int]) -> None:
        for dst, d_via_nbr in vector.items():
            total = w_nbr + d_via_nbr
            cur = self._routes.get(dst)
            if cur is None or total < cur[0]:
                self._routes[dst] = (total, {nbr})
            elif total == cur[0]:
                cur[1].add(nbr)
        for dst, (cost, hops) in list(self._routes.items()):
            if nbr in hops and dst in vector:
                new_cost = w_nbr + vector[dst]
                if new_cost > cost:
                    hops.discard(nbr)
                    if not hops:
                        self._routes.pop(dst)

    def purge_hop(self, broken_nh: str) -> None:
        for dst in list(self._routes.keys()):
            cost, hops = self._routes[dst]
            if broken_nh in hops:
                hops.discard(broken_nh)
                if not hops:
                    self._routes.pop(dst)

    def add_direct(self, neighbor_ip: str, weight: int) -> None:
        self._routes[neighbor_ip] = (weight, {neighbor_ip})

class Router:
    def __init__(self, my_ip: str, period: float):
        self.my_ip = my_ip
        self.period = period
        self.links = LinkTable(AGING_FACTOR * period)
        self.rt = RoutingTable(my_ip)
        self.cli_q: "queue.Queue[str]" = queue.Queue()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((my_ip, UDP_PORT))

        self._running = threading.Event()
        self._running.set()
        self._threads = [
            threading.Thread(target=self._listen_loop, daemon=True),
            threading.Thread(target=self._update_loop, daemon=True),
            threading.Thread(target=self._cli_loop,    daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _listen_loop(self):
        while self._running.is_set():
            try:
                raw, (src_ip, _) = self.sock.recvfrom(65507)
                msg = jload(raw)
                self._dispatch(msg, src_ip)
            except Exception as e:
                print("listener error:", e, file=sys.stderr)

    def _update_loop(self):
        while self._running.is_set():
            time.sleep(self.period)
            for dead in self.links.expire():
                self.rt.purge_hop(dead)

            for nbr in self.links.neighbors():
                vec = self.rt.export(nbr)
                self._send(mk_update(self.my_ip, nbr, vec), nbr)

    def _cli_loop(self):
        while self._running.is_set():
            try:
                cmd = self.cli_q.get(timeout=0.1)
            except queue.Empty:
                try:
                    cmd = input("> ").strip()
                except EOFError:
                    cmd = "quit"
            self._exec_cmd(cmd)

    def _exec_cmd(self, cmd: str):
        if not cmd:
            return
        parts = cmd.split()
        match parts[0]:
            case "add" if len(parts) == 3:
                ip, w = parts[1], int(parts[2])
                self.links.add(ip, w)
                self.rt.add_direct(ip, w)
                print(f"+ link {ip}/{w}")
            case "del" if len(parts) == 2:
                ip = parts[1]
                self.links.remove(ip)
                self.rt.purge_hop(ip)
                print(f"- link {ip}")
            case "trace" if len(parts) == 2:
                dst = parts[1]
                t = mk_trace(self.my_ip, dst, [self.my_ip])
                self._forward_or_notify(t)
            case "quit":
                self.shutdown()
            case "show":
                print(self.rt._routes)
            case _:
                print("Commands: add <ip> <w>, del <ip>, trace <ip>, quit")

    def _dispatch(self, msg: Dict[str, Any], src_ip: str):
        t = msg.get("type")
        if t == "update":
            self._handle_update(msg, src_ip)
        elif t == "data":
            self._handle_data(msg)
        elif t == "trace":
            self._handle_trace(msg)
        elif t == "control":
            self._handle_control(msg)

    def _handle_update(self, msg: Dict[str, Any], src_ip: str):
        self.links.touch(src_ip)
        w = self.links.weight(src_ip)
        if w is not None:
            self.rt.learn_neighbor_vector(src_ip, w, msg["distances"])

    def _handle_data(self, msg: Dict[str, Any]):
        if msg["destination"] == self.my_ip:
            print(msg["payload"])
        else:
            self._forward_or_notify(msg)

    def _handle_trace(self, msg: Dict[str, Any]):
        msg["routers"].append(self.my_ip)
        if msg["destination"] == self.my_ip:
            reply = mk_data(self.my_ip, msg["source"], json.dumps(msg))
            self._send(reply, msg["source"])
        else:
            self._forward_or_notify(msg)
    
    def _handle_control(self, msg: Dict[str, Any]):
        if msg["destination"] == self.my_ip:
            print(f"CONTROL {msg['reason']} -> pacote {msg['original']}")
        else:
            self._forward_or_notify(msg)

    def _forward_or_notify(self, msg: Dict[str, Any]):
        nh = self.rt.next_hop(msg["destination"])
        if nh:
            self._send(msg, nh)
        else:
            ctrl = mk_control(
                self.my_ip,
                msg["source"],
                "unreachable",
                msg,
            )
            back = self.rt.next_hop(ctrl["destination"])
            if back:
                self._send(ctrl, back)

    def _send(self, msg: Dict[str, Any], target_ip: str):
        try:
            self.sock.sendto(jdump(msg), (target_ip, UDP_PORT))
        except OSError as e:
            print(f"send failed to {target_ip}:", e, file=sys.stderr)

    def shutdown(self):
        self._running.clear()
        self.sock.close()
        print("x router closed")

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UDPRIP router (archive)")
    p.add_argument("address")
    p.add_argument("period", type=float)
    p.add_argument("startup", nargs="?", help="optional startup archive")
    return p.parse_args()

def main():
    args = parse_args()
    r = Router(args.address, args.period)
    if args.startup:
        with open(args.startup) as f:
            for line in f:
                r.cli_q.put(line.strip())
    try:
        while r._running.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        r.shutdown()

if __name__ == "__main__":
    main()
