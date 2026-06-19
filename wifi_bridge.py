#!/usr/bin/env python3
"""
MICROCORE (MCX) WIFI BRIDGE v6.2 — COMPLETE
For Arduino Uno - bridges Serial to WebSocket
Gossip Discovery | Peer Caching | Auto Failover | No DNS Required
Full level system support | Permanent towers | Remote control
Block redistribution | Global reward pools

Run: python3 wifi_bridge.py

Requirements:
  pip install pyserial websockets
"""

import asyncio
import serial
import serial.tools.list_ports
import json
import websockets
import time
import os
import sys
from datetime import datetime

# ==================== GOSSIP DISCOVERY (NO DNS) ====================
# HARDCODED BOOTNODES - CHANGE THIS TO YOUR NODE IP
BOOTSTRAP_NODES = [
    "101.127.80.48:8080",  # ← CHANGE THIS TO YOUR NODE IP
]

PEER_CACHE_FILE = "bridge_peers.json"
NODE_PORT = 8080
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_DELAY = 5

def save_peers_to_cache(peers):
    try:
        unique = list(set(peers))
        with open(PEER_CACHE_FILE, 'w') as f:
            json.dump(unique, f, indent=2)
        print(f"[CACHE] Saved {len(unique)} peers")
    except Exception as e:
        print(f"[CACHE] Save failed: {e}")

def load_peers_from_cache():
    try:
        with open(PEER_CACHE_FILE, 'r') as f:
            peers = json.load(f)
        print(f"[CACHE] Loaded {len(peers)} peers from cache")
        return peers
    except:
        print(f"[CACHE] No cache file found")
        return []

def get_bootstrap_peers():
    peers = BOOTSTRAP_NODES.copy()
    cached = load_peers_from_cache()
    for p in cached:
        if p not in peers:
            peers.append(p)
    return peers

# ==================== CONFIGURATION ====================
BAUD_RATE = 115200
BRIDGE_ID = "arduino_bridge_1"

# ==================== GLOBAL VARIABLES ====================
running = True
ser = None
websocket = None
message_buffer = []
current_node_url = None
node_urls = []
current_node_index = 0
reconnect_attempts = 0
discovered_peers = set()

stats = {
    "messages_sent": 0,
    "messages_received": 0,
    "errors": 0,
    "start_time": time.time(),
    "node_switches": 0
}

# ==================== SERIAL PORT DETECTION ====================
def find_arduino_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if "Arduino" in port.description or "USB" in port.description or \
           "ttyACM" in port.device or "ttyUSB" in port.device or \
           "cu.usbmodem" in port.device:
            print(f"[BRIDGE] Found Arduino on {port.device}: {port.description}")
            return port.device
    return None

def add_peer_from_gossip(peer):
    if peer not in discovered_peers:
        discovered_peers.add(peer)
        node_urls.append(peer)
        save_peers_to_cache(list(discovered_peers))
        print(f"[GOSSIP] Discovered new peer: {peer}")

def switch_to_next_node():
    global current_node_index, reconnect_attempts, current_node_url
    current_node_index = (current_node_index + 1) % len(node_urls) if node_urls else 0
    current_node_url = node_urls[current_node_index] if node_urls else None
    reconnect_attempts += 1
    stats["node_switches"] += 1
    if current_node_url:
        print(f"[BRIDGE] Switching to node: {current_node_url} (switch #{stats['node_switches']})")

def get_current_node_url():
    if not node_urls:
        return None
    peer = node_urls[current_node_index]
    if "://" not in peer:
        peer = f"ws://{peer}"
    return peer

# ==================== WEBSOCKET CONNECTION ====================
async def connect_to_node():
    global websocket, current_node_url, node_urls, reconnect_attempts
    
    while running:
        if not node_urls:
            node_urls = get_bootstrap_peers()
            current_node_index = 0
        
        current_node_url = get_current_node_url()
        if not current_node_url:
            print("[BRIDGE] No nodes available. Waiting...")
            await asyncio.sleep(30)
            node_urls = get_bootstrap_peers()
            continue
        
        try:
            print(f"[BRIDGE] Connecting to node: {current_node_url}")
            async with websockets.connect(
                current_node_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                websocket = ws
                reconnect_attempts = 0
                print(f"[BRIDGE] Connected to {current_node_url}")
                
                await ws.send(json.dumps({"type": "get_peers"}))
                
                await ws.send(json.dumps({
                    "type": "bridge_register",
                    "bridge_id": BRIDGE_ID,
                    "timestamp": time.time()
                }))
                
                for msg in message_buffer:
                    await ws.send(msg)
                message_buffer.clear()
                
                try:
                    async for message in ws:
                        await handle_node_message(message)
                except websockets.exceptions.ConnectionClosed:
                    print(f"[BRIDGE] Connection to {current_node_url} closed")
                    
        except Exception as e:
            print(f"[BRIDGE] Node connection failed: {e}")
            switch_to_next_node()
            await asyncio.sleep(RECONNECT_DELAY * min(reconnect_attempts + 1, 10))
        
        websocket = None

# ==================== MESSAGE HANDLING ====================
async def handle_node_message(message):
    global websocket, ser, stats
    try:
        msg = json.loads(message)
        stats["messages_sent"] += 1
        print(f"[←] {message[:150]}{'...' if len(message) > 150 else ''}")
        
        if msg.get("type") == "peers":
            for peer in msg.get("peers", []):
                add_peer_from_gossip(peer)
            print(f"[GOSSIP] Received {len(msg.get('peers', []))} peers from node")
        
        if ser and ser.is_open:
            ser.write((message + "\n").encode())
        else:
            print("[ERROR] Serial port not open")
            stats["errors"] += 1
            
    except Exception as e:
        print(f"[ERROR] Failed to handle node message: {e}")
        stats["errors"] += 1

async def forward_arduino_to_node():
    global ser, websocket, stats
    
    while running and ser and ser.is_open:
        try:
            if ser.in_waiting:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if line:
                    stats["messages_received"] += 1
                    print(f"[→] {line[:150]}{'...' if len(line) > 150 else ''}")
                    
                    if websocket and websocket.open:
                        try:
                            await websocket.send(line)
                            stats["messages_sent"] += 1
                        except Exception as e:
                            print(f"[ERROR] Failed to send to node: {e}")
                            message_buffer.append(line)
                            stats["errors"] += 1
                    else:
                        message_buffer.append(line)
            await asyncio.sleep(0.01)
        except Exception as e:
            print(f"[ERROR] Serial read error: {e}")
            stats["errors"] += 1
            await asyncio.sleep(1)

async def manage_serial():
    global ser
    
    while running:
        if not ser or not ser.is_open:
            port = find_arduino_port()
            if port:
                try:
                    ser = serial.Serial(port, BAUD_RATE, timeout=1, write_timeout=1)
                    print(f"[BRIDGE] Serial port opened: {port} @ {BAUD_RATE} baud")
                    await asyncio.sleep(2)
                    ser.write(b'{"type":"ping"}\n')
                except Exception as e:
                    print(f"[BRIDGE] Failed to open {port}: {e}")
                    ser = None
                    await asyncio.sleep(5)
            else:
                print("[BRIDGE] No Arduino found. Waiting...")
                await asyncio.sleep(5)
        else:
            await asyncio.sleep(1)

async def status_reporter():
    while running:
        await asyncio.sleep(60)
        uptime = int(time.time() - stats["start_time"])
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        
        print(f"\n{'='*50}")
        print(f"BRIDGE STATUS REPORT")
        print(f"{'='*50}")
        print(f"Uptime: {hours}h {minutes}m")
        print(f"Messages to node: {stats['messages_sent']}")
        print(f"Messages from node: {stats['messages_received']}")
        print(f"Errors: {stats['errors']}")
        print(f"Node switches: {stats['node_switches']}")
        print(f"Current node: {current_node_url}")
        print(f"Peers in cache: {len(discovered_peers)}")
        print(f"Serial port: {'Open' if ser and ser.is_open else 'Closed'}")
        print(f"WebSocket: {'Connected' if websocket and websocket.open else 'Disconnected'}")
        print(f"Buffer size: {len(message_buffer)}")
        print(f"{'='*50}\n")

async def main():
    print("\n" + "=" * 60)
    print("MICROCORE (MCX) WIFI BRIDGE v6.2 — COMPLETE")
    print("For Arduino Uno - Gossip Discovery | No DNS Required")
    print("Full Level System | Permanent Towers | Remote Control")
    print("Block Redistribution | Global Reward Pools")
    print("=" * 60)
    
    global node_urls, discovered_peers
    node_urls = get_bootstrap_peers()
    discovered_peers = set(node_urls)
    
    print(f"[BRIDGE] Bridge ID: {BRIDGE_ID}")
    print(f"[BRIDGE] Baud Rate: {BAUD_RATE}")
    print(f"[BRIDGE] Bootnodes: {BOOTSTRAP_NODES}")
    print(f"[BRIDGE] Peers in cache: {len(discovered_peers)}")
    print("[BRIDGE] Starting...\n")
    
    await asyncio.gather(
        manage_serial(),
        connect_to_node(),
        forward_arduino_to_node(),
        status_reporter()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[BRIDGE] Stopped by user")
    finally:
        if ser and ser.is_open:
            ser.close()
        print("[BRIDGE] Goodbye!")