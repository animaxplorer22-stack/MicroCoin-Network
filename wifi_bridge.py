#!/usr/bin/env python3
"""
MICROCORE (MCX) WIFI BRIDGE v3.0
For Arduino Uno - bridges Serial to WebSocket with auto node discovery and failover

Run: python3 wifi_bridge.py

Requirements:
  pip install pyserial websockets dnspython requests
"""

import asyncio
import serial
import serial.tools.list_ports
import json
import websockets
import socket
import time
import sys
from datetime import datetime

# ==================== CONFIGURATION ====================
DNS_SEEDS = ["seed.microcore.com", "seed1.microcore.com", "seed2.microcore.com"]
NODE_PORT = 8080
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
stats = {
    "messages_sent": 0,
    "messages_received": 0,
    "errors": 0,
    "start_time": time.time(),
    "node_switches": 0
}

# ==================== DNS SEED RESOLUTION ====================
def resolve_dns_seeds():
    nodes = []
    try:
        import dns.resolver
        for seed in DNS_SEEDS:
            try:
                answers = dns.resolver.resolve(seed, 'A')
                for answer in answers:
                    nodes.append(f"ws://{str(answer)}:{NODE_PORT}")
                print(f"[DNS] Found {len(answers)} nodes from {seed}")
            except Exception as e:
                print(f"[DNS] Failed to resolve {seed}: {e}")
    except ImportError:
        print("[DNS] dnspython not installed. Using fallback nodes.")
        nodes = [f"ws://127.0.0.1:{NODE_PORT}"]
    
    if not nodes:
        nodes = [f"ws://127.0.0.1:{NODE_PORT}"]
    
    return nodes

# ==================== SERIAL PORT DETECTION ====================
def find_arduino_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if "Arduino" in port.description or "USB" in port.description or "ttyACM" in port.device or "ttyUSB" in port.device:
            print(f"[BRIDGE] Found Arduino on {port.device}: {port.description}")
            return port.device
    return None

# ==================== WEBSOCKET CONNECTION ====================
async def connect_to_node():
    global websocket, current_node_url, current_node_index, node_urls
    
    while running:
        try:
            current_node_url = node_urls[current_node_index % len(node_urls)]
            print(f"[BRIDGE] Connecting to node: {current_node_url}")
            
            async with websockets.connect(
                current_node_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                websocket = ws
                print(f"[BRIDGE] Connected to {current_node_url}")
                
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
            current_node_index += 1
            stats["node_switches"] += 1
            print(f"[BRIDGE] Switching to next node (switch #{stats['node_switches']})")
            await asyncio.sleep(5)
        
        websocket = None

async def handle_node_message(message):
    global websocket, ser, stats
    try:
        msg = json.loads(message)
        stats["messages_sent"] += 1
        print(f"[←] {message[:100]}{'...' if len(message) > 100 else ''}")
        
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
                    print(f"[→] {line[:100]}{'...' if len(line) > 100 else ''}")
                    
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
        print(f"Serial port: {'Open' if ser and ser.is_open else 'Closed'}")
        print(f"WebSocket: {'Connected' if websocket and websocket.open else 'Disconnected'}")
        print(f"Buffer size: {len(message_buffer)}")
        print(f"{'='*50}\n")

async def main():
    print("\n" + "=" * 60)
    print("MICROCORE (MCX) WIFI BRIDGE v3.0")
    print("For Arduino Uno - Auto Node Discovery | Failover")
    print("=" * 60)
    
    global node_urls
    node_urls = resolve_dns_seeds()
    print(f"[BRIDGE] Discovered {len(node_urls)} nodes from DNS seeds")
    for i, url in enumerate(node_urls):
        print(f"  {i+1}. {url}")
    
    print(f"\n[BRIDGE] Bridge ID: {BRIDGE_ID}")
    print(f"[BRIDGE] Baud Rate: {BAUD_RATE}")
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