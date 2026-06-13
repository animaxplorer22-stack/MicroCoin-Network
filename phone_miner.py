#!/usr/bin/env python3
"""
MICROCORE (MCX) PHONE MINER v3.0
Runs on iPhone (a-shell/iSH) and Android (Termux)
Real ECDSA secp256k1 | Auto node discovery | Failover

Run: python3 phone_miner.py

Requirements (auto-installed):
  pip install websockets cryptography dnspython
"""

import json
import time
import hashlib
import os
import sys
import random
import socket
import asyncio
from datetime import datetime

# ==================== DEPENDENCY CHECK ====================
try:
    import websockets
except ImportError:
    print("[SETUP] Installing websockets...")
    os.system("pip install websockets")
    import websockets

try:
    import requests
except ImportError:
    print("[SETUP] Installing requests...")
    os.system("pip install requests")
    import requests

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
    ECDSA_AVAILABLE = True
except ImportError:
    print("[SETUP] Installing cryptography...")
    os.system("pip install cryptography")
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
    ECDSA_AVAILABLE = True

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    print("[SETUP] Installing dnspython...")
    os.system("pip install dnspython")
    import dns.resolver
    DNS_AVAILABLE = True

# ==================== CONFIGURATION ====================
USERNAME = ""  # Leave empty for first-run setup
WALLET_FILE = "microcore_phone_wallet.json"

DNS_SEEDS = ["seed.microcore.com", "seed1.microcore.com", "seed2.microcore.com"]
NODE_PORT = 8080

INITIAL_STAKE = 100
LEVEL_STAKE_RANGE = 100
SIGNING_WINDOW_MS = 2500
SLASH_RATE = 0.10
UPTIME_PING_INTERVAL = 30
STATUS_INTERVAL = 60
MAX_RECONNECT_ATTEMPTS = 10
RECONNECT_DELAY = 5

# ==================== REAL CRYPTO FUNCTIONS ====================
def generate_private_key():
    private_key = ec.generate_private_key(ec.SECP256K1())
    private_key_hex = private_key.private_numbers().private_value.to_bytes(32, 'big').hex()
    return private_key_hex, private_key

def get_public_key_pem(private_key_hex):
    private_value = int(private_key_hex, 16)
    private_key = ec.derive_private_key(private_value, ec.SECP256K1())
    public_key = private_key.public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

def get_wallet_address(public_key_pem):
    addr_hash = hashlib.sha256(public_key_pem.encode()).hexdigest()
    return f"MCR_{addr_hash[:32].upper()}"

def get_validator_id(username, public_key_pem):
    return hashlib.sha256(f"{username}{public_key_pem}".encode()).hexdigest()[:32]

def sign_message(private_key_hex, message):
    private_value = int(private_key_hex, 16)
    private_key = ec.derive_private_key(private_value, ec.SECP256K1())
    signature = private_key.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    return r.to_bytes(32, 'big').hex() + s.to_bytes(32, 'big').hex()

# ==================== DNS NODE DISCOVERY ====================
def resolve_dns_seed(seed):
    try:
        import socket
        ip = socket.gethostbyname(seed)
        print(f"[DNS] Resolved {seed} -> {ip}")
        return ip
    except Exception as e:
        print(f"[DNS] Failed to resolve {seed}: {e}")
        return None

def discover_nodes():
    nodes = []
    for seed in DNS_SEEDS:
        ip = resolve_dns_seed(seed)
        if ip:
            nodes.append(f"ws://{ip}:{NODE_PORT}")
    if not nodes:
        nodes = ["ws://127.0.0.1:8080"]
        print(f"[DNS] Using fallback node: 127.0.0.1:8080")
    return nodes

# ==================== WALLET MANAGEMENT ====================
class Wallet:
    def __init__(self, username, address, public_key_pem, private_key_hex):
        self.username = username
        self.address = address
        self.public_key_pem = public_key_pem
        self.private_key_hex = private_key_hex
    
    def get_validator_id(self):
        return get_validator_id(self.username, self.public_key_pem)
    
    @classmethod
    def create_new(cls, username):
        private_key_hex, _ = generate_private_key()
        public_key_pem = get_public_key_pem(private_key_hex)
        address = get_wallet_address(public_key_pem)
        return cls(username, address, public_key_pem, private_key_hex)
    
    @classmethod
    def load(cls, filename):
        if not os.path.exists(filename):
            return None
        with open(filename, 'r') as f:
            data = json.load(f)
        return cls(data['username'], data['address'], data['public_key_pem'], data['private_key_hex'])
    
    def save(self, filename):
        with open(filename, 'w') as f:
            json.dump({
                'username': self.username,
                'address': self.address,
                'public_key_pem': self.public_key_pem,
                'private_key_hex': self.private_key_hex
            }, f, indent=2)

# ==================== PHONE MINER ====================
class PhoneMiner:
    def __init__(self, wallet):
        self.wallet = wallet
        self.validator_id = wallet.get_validator_id()
        self.node_urls = discover_nodes()
        self.current_node_index = 0
        self.current_node_url = self.node_urls[0]
        
        self.ws = None
        self.is_validator = False
        self.current_challenge = ""
        self.current_block_id = 0
        self.last_challenge_time = 0
        self.start_time = time.time()
        self.last_uptime_ping = 0
        self.last_status_report = 0
        self.reconnect_attempts = 0
        self.node_switch_count = 0
        self.connected = False
        self.mining = True
        self.running = True
        
        self.total_rewards = 0
        self.blocks_signed = 0
        self.consecutive_misses = 0
        self.slash_count = 0
        self.current_stake = INITIAL_STAKE
        self.today_uptime = 0
        self.last_uptime_reset = time.time()
        self.current_level = self.calculate_level()
        
        self.load_stats()
    
    def calculate_level(self):
        level = ((self.current_stake - 1) // LEVEL_STAKE_RANGE) + 1
        return max(1, min(level, 100))
    
    def update_today_uptime(self):
        now = time.time()
        if now - self.last_uptime_reset > 86400:
            self.today_uptime = 0
            self.last_uptime_reset = now
        self.today_uptime += UPTIME_PING_INTERVAL
        if self.today_uptime > 86400:
            self.today_uptime = 86400
    
    def load_stats(self):
        stats_file = "phone_miner_stats.json"
        if os.path.exists(stats_file):
            try:
                with open(stats_file, 'r') as f:
                    data = json.load(f)
                    self.total_rewards = data.get('rewards', 0)
                    self.blocks_signed = data.get('blocks', 0)
                    self.slash_count = data.get('slashes', 0)
                    self.current_stake = data.get('stake', INITIAL_STAKE)
                    self.today_uptime = data.get('today_uptime', 0)
                    self.current_level = self.calculate_level()
            except:
                pass
    
    def save_stats(self):
        with open("phone_miner_stats.json", 'w') as f:
            json.dump({
                'rewards': self.total_rewards,
                'blocks': self.blocks_signed,
                'slashes': self.slash_count,
                'stake': self.current_stake,
                'today_uptime': self.today_uptime
            }, f, indent=2)
    
    def switch_to_next_node(self):
        self.current_node_index = (self.current_node_index + 1) % len(self.node_urls)
        self.current_node_url = self.node_urls[self.current_node_index]
        self.node_switch_count += 1
        print(f"\n[FAILOVER] Switching to node: {self.current_node_url} (#{self.node_switch_count})\n")
    
    def add_reward(self, reward):
        self.total_rewards += reward
        self.current_stake += reward
        self.blocks_signed += 1
        self.consecutive_misses = 0
        self.current_level = self.calculate_level()
        self.save_stats()
        print(f"\n💰 REWARD: +{reward} MCX | Total: {self.total_rewards} | Stake: {self.current_stake} | Level: {self.current_level}")
    
    def handle_slash(self):
        slash_amount = max(int(self.current_stake * SLASH_RATE), LEVEL_STAKE_RANGE)
        self.current_stake -= slash_amount
        if self.current_stake < LEVEL_STAKE_RANGE:
            self.current_stake = LEVEL_STAKE_RANGE
        self.consecutive_misses += 1
        self.slash_count += 1
        self.current_level = self.calculate_level()
        self.save_stats()
        print(f"\n⚠️ SLASHED: -{slash_amount} MCX | Stake: {self.current_stake} | Level: {self.current_level} | Misses: {self.consecutive_misses}")
        return self.slash_count < 5
    
    async def register(self):
        timestamp = time.time()
        reg_message = f"{self.validator_id}{self.wallet.username}{self.current_stake}{timestamp}"
        signature = sign_message(self.wallet.private_key_hex, reg_message)
        
        msg = {
            "type": "register",
            "validator_id": self.validator_id,
            "username": self.wallet.username,
            "public_key": self.wallet.public_key_pem,
            "wallet": self.wallet.address,
            "stake": self.current_stake,
            "level": self.current_level,
            "rewards": self.total_rewards,
            "blocks": self.blocks_signed,
            "uptime": int(time.time() - self.start_time),
            "today_uptime": self.today_uptime,
            "miner_type": "phone",
            "timestamp": timestamp,
            "signature": signature
        }
        
        if self.ws:
            await self.ws.send(json.dumps(msg))
            print(f"📡 Registered as '{self.wallet.username}'")
    
    async def send_uptime(self):
        uptime = int(time.time() - self.start_time)
        self.update_today_uptime()
        msg = {
            "type": "uptime_ping",
            "validator_id": self.validator_id,
            "username": self.wallet.username,
            "uptime_seconds": uptime,
            "today_uptime": self.today_uptime,
            "stake": self.current_stake,
            "level": self.current_level
        }
        if self.ws:
            await self.ws.send(json.dumps(msg))
    
    async def sign_block(self):
        message = f"{self.current_challenge}{self.validator_id}{self.current_block_id}"
        signature = sign_message(self.wallet.private_key_hex, message)
        
        msg = {
            "type": "block_signature",
            "validator_id": self.validator_id,
            "username": self.wallet.username,
            "challenge": self.current_challenge,
            "signature": signature,
            "level": self.current_level,
            "stake": self.current_stake,
            "block_id": self.current_block_id,
            "timestamp": time.time()
        }
        
        if self.ws:
            await self.ws.send(json.dumps(msg))
            print(f"✍️ Signed block {self.current_block_id}")
    
    async def handle_message(self, data):
        try:
            msg = json.loads(data)
            msg_type = msg.get("type")
            
            if msg_type == "registered":
                print(f"✅ Registration confirmed | Level: {msg.get('level')}")
                print(f"💰 Current reward: {msg.get('current_reward')} MCX per block")
                self.reconnect_attempts = 0
            
            elif msg_type == "challenge":
                self.current_challenge = msg.get("challenge", "")
                self.current_block_id = msg.get("block_id", 0)
                self.last_challenge_time = time.time()
                self.is_validator = True
                await self.sign_block()
                
                if hasattr(self, '_timeout_task'):
                    self._timeout_task.cancel()
                
                async def timeout_handler():
                    await asyncio.sleep(SIGNING_WINDOW_MS / 1000)
                    if self.is_validator:
                        print(f"⏰ TIMEOUT: Missed block {self.current_block_id}")
                        self.handle_slash()
                        self.is_validator = False
                
                self._timeout_task = asyncio.create_task(timeout_handler())
            
            elif msg_type == "block_accepted":
                if hasattr(self, '_timeout_task'):
                    self._timeout_task.cancel()
                reward = msg.get("reward", 0)
                self.add_reward(reward)
                self.is_validator = False
                print(f"✅ Block {msg.get('block_id')} ACCEPTED! +{reward} MCX")
            
            elif msg_type == "block_rejected":
                if hasattr(self, '_timeout_task'):
                    self._timeout_task.cancel()
                self.is_validator = False
                print(f"❌ Block {msg.get('block_id')} REJECTED")
            
            elif msg_type == "slash":
                print(f"⚠️ SLASH command received")
                self.handle_slash()
                self.is_validator = False
            
            elif msg_type == "level_update":
                new_stake = msg.get("stake", self.current_stake)
                if new_stake != self.current_stake:
                    self.current_stake = new_stake
                    self.current_level = self.calculate_level()
                    self.save_stats()
                    print(f"📊 Level update: Level {self.current_level}")
            
            elif msg_type == "miner_control":
                action = msg.get("action")
                if action == "stop":
                    print("🛑 Stop command received - stopping mining")
                    self.mining = False
                    self.is_validator = False
                elif action == "start":
                    print("▶️ Start command received - resuming mining")
                    self.mining = True
                elif action == "restart":
                    print("🔄 Restart command received")
                    self.mining = False
                    self.is_validator = False
                    # Will reconnect
        
        except Exception as e:
            print(f"[ERROR] Message handling: {e}")
    
    async def connect_and_run(self):
        self.reconnect_attempts = 0
        
        while self.running:
            try:
                print(f"[CONN] Connecting to {self.current_node_url}...")
                async with websockets.connect(
                    self.current_node_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    self.reconnect_attempts = 0
                    print(f"🔌 Connected to {self.current_node_url}")
                    await self.register()
                    
                    while self.running and self.mining:
                        if time.time() - self.last_uptime_ping > UPTIME_PING_INTERVAL:
                            await self.send_uptime()
                            self.last_uptime_ping = time.time()
                        
                        if time.time() - self.last_status_report > STATUS_INTERVAL:
                            self.print_status()
                            self.last_status_report = time.time()
                        
                        try:
                            raw_message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            await self.handle_message(raw_message)
                        except asyncio.TimeoutError:
                            pass
                        
                        if self.is_validator and (time.time() - self.last_challenge_time) > (SIGNING_WINDOW_MS / 1000 + 0.5):
                            print(f"⏰ Fallback timeout! Missed block {self.current_block_id}")
                            self.handle_slash()
                            self.is_validator = False
                        
                        await asyncio.sleep(0.05)
            
            except Exception as e:
                print(f"[ERROR] {e}")
                self.connected = False
                self.reconnect_attempts += 1
                if self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                    self.switch_to_next_node()
                    self.reconnect_attempts = 0
                delay = RECONNECT_DELAY * min(self.reconnect_attempts + 1, 10)
                print(f"[CONN] Reconnecting in {delay}s...")
                await asyncio.sleep(delay)
            
            finally:
                self.ws = None
    
    def print_status(self):
        uptime = int(time.time() - self.start_time)
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        today_hours = self.today_uptime / 3600
        success_rate = 0
        total = self.blocks_signed + self.consecutive_misses
        if total > 0:
            success_rate = (self.blocks_signed / total) * 100
        
        print(f"\n{'='*50}")
        print(f"📱 PHONE MINER STATUS")
        print(f"{'='*50}")
        print(f"Username: {self.wallet.username}")
        print(f"Wallet: {self.wallet.address[:24]}...")
        print(f"{'-'*40}")
        print(f"Level: {self.current_level} / 100")
        print(f"Stake: {self.current_stake:,} MCX")
        print(f"Rewards: {self.total_rewards:,} MCX")
        print(f"Blocks Signed: {self.blocks_signed}")
        print(f"Missed: {self.consecutive_misses}")
        print(f"Success Rate: {success_rate:.1f}%")
        print(f"Slashes: {self.slash_count} / 5")
        print(f"{'-'*40}")
        print(f"Uptime: {hours}h {minutes}m")
        print(f"Today's Uptime: {today_hours:.1f}h / 24h")
        print(f"Current Node: {self.current_node_url}")
        print(f"Node Switches: {self.node_switch_count}")
        print(f"Status: {'🟢 Mining' if self.mining else '🔴 Stopped'}")
        print(f"Connected: {'✅ Yes' if self.connected else '❌ No'}")
        print(f"{'='*50}\n")
    
    async def run(self):
        print(f"\n{'='*60}")
        print(f"📱 MICROCORE (MCX) PHONE MINER v3.0")
        print(f"Real ECDSA | Auto Node Discovery | Failover")
        print(f"{'='*60}")
        print(f"Username: {self.wallet.username}")
        print(f"Wallet: {self.wallet.address}")
        print(f"Validator ID: {self.validator_id[:20]}...")
        print(f"{'-'*40}")
        print(f"Initial Stake: {self.current_stake} MCX")
        print(f"Initial Level: {self.current_level}")
        print(f"Signing Window: {SIGNING_WINDOW_MS} ms")
        print(f"{'-'*40}")
        print(f"Discovered Nodes: {len(self.node_urls)}")
        for i, url in enumerate(self.node_urls):
            print(f"  {i+1}. {url}")
        print(f"{'='*60}\n")
        
        await self.connect_and_run()

# ==================== MAIN ====================
async def main():
    print("\n" + "=" * 60)
    print("🔷 MICROCORE (MCX) PHONE MINER 🔷")
    print("Mine from your iPhone or Android - Real ECDSA")
    print("=" * 60)
    
    wallet = Wallet.load(WALLET_FILE)
    if not wallet:
        print("\n[FIRST RUN] No wallet found.")
        if USERNAME:
            username = USERNAME
        else:
            username = input("Enter your username: ").strip()
            if not username:
                username = f"phone_miner_{int(time.time())}"
        
        wallet = Wallet.create_new(username)
        wallet.save(WALLET_FILE)
        print(f"\n✅ Wallet created!")
        print(f"   Username: {wallet.username}")
        print(f"   Address: {wallet.address}")
        print(f"\n⚠️ SAVE THESE CREDENTIALS!")
        print(f"   Wallet file: {os.path.abspath(WALLET_FILE)}")
    else:
        print(f"\n✅ Wallet loaded: {wallet.username}")
        print(f"   Address: {wallet.address[:32]}...")
    
    miner = PhoneMiner(wallet)
    
    try:
        await miner.run()
    except KeyboardInterrupt:
        print("\n[STOP] Miner stopped")
        miner.save_stats()
        print(f"\n📊 FINAL STATS")
        print(f"   Rewards: {miner.total_rewards} MCX")
        print(f"   Blocks: {miner.blocks_signed}")
        print(f"   Final Stake: {miner.current_stake} MCX")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[EXIT] Goodbye!")