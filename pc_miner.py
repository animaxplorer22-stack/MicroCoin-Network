#!/usr/bin/env python3
"""
MICROCORE (MCX) PC MINER v3.0
Real ECDSA secp256k1 | Username-based | Auto node discovery | Failover | Level System

Run: python3 pc_miner.py

Requirements:
  pip install websockets cryptography dnspython requests
"""

import asyncio
import json
import time
import hashlib
import os
import sys
import random
import signal
from datetime import datetime
from typing import Optional, List
import traceback

try:
    import websockets
except ImportError:
    print("ERROR: Install websockets: pip install websockets")
    sys.exit(1)

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    print("WARNING: dnspython not installed. DNS discovery disabled.")
    print("Install: pip install dnspython")
    DNS_AVAILABLE = False

try:
    import requests
except ImportError:
    print("WARNING: requests not installed. IP detection disabled.")
    requests = None

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
    ECDSA_AVAILABLE = True
except ImportError:
    print("ERROR: Install cryptography: pip install cryptography")
    sys.exit(1)

# ==================== CONFIGURATION ====================
USERNAME = ""  # Leave empty for first-run setup
WALLET_FILE = "microcore_pc_wallet.json"

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
def generate_private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256K1())

def private_key_to_hex(private_key: ec.EllipticCurvePrivateKey) -> str:
    return private_key.private_numbers().private_value.to_bytes(32, 'big').hex()

def hex_to_private_key(hex_key: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int(hex_key, 16), ec.SECP256K1())

def get_public_key_pem(private_key: ec.EllipticCurvePrivateKey) -> str:
    public_key = private_key.public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

def get_wallet_address(public_key_pem: str) -> str:
    addr_hash = hashlib.sha256(public_key_pem.encode()).hexdigest()
    return f"MCR_{addr_hash[:32].upper()}"

def get_validator_id(username: str, public_key_pem: str) -> str:
    return hashlib.sha256(f"{username}{public_key_pem}".encode()).hexdigest()[:32]

def sign_message(private_key: ec.EllipticCurvePrivateKey, message: str) -> str:
    signature = private_key.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    return r.to_bytes(32, 'big').hex() + s.to_bytes(32, 'big').hex()

# ==================== DNS NODE DISCOVERY ====================
def resolve_dns_seeds() -> List[str]:
    if not DNS_AVAILABLE:
        return ["ws://127.0.0.1:8080"]
    nodes = []
    for seed in DNS_SEEDS:
        try:
            answers = dns.resolver.resolve(seed, 'A')
            for answer in answers:
                nodes.append(f"ws://{str(answer)}:{NODE_PORT}")
            print(f"[DNS] Found {len(answers)} nodes from {seed}")
        except Exception as e:
            print(f"[DNS] Failed to resolve {seed}: {e}")
    if not nodes:
        nodes = ["ws://127.0.0.1:8080"]
    return nodes

def get_public_ip() -> str:
    if requests is None:
        return "unknown"
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=5)
        return response.json()['ip']
    except:
        return "unknown"

# ==================== WALLET MANAGEMENT ====================
class Wallet:
    def __init__(self, username: str, address: str, public_key_pem: str, private_key_hex: str):
        self.username = username
        self.address = address
        self.public_key_pem = public_key_pem
        self.private_key_hex = private_key_hex
        self._private_key = None
    
    def get_private_key(self) -> ec.EllipticCurvePrivateKey:
        if self._private_key is None:
            self._private_key = hex_to_private_key(self.private_key_hex)
        return self._private_key
    
    def get_validator_id(self) -> str:
        return get_validator_id(self.username, self.public_key_pem)
    
    @classmethod
    def create_new(cls, username: str) -> 'Wallet':
        private_key = generate_private_key()
        private_key_hex = private_key_to_hex(private_key)
        public_key_pem = get_public_key_pem(private_key)
        address = get_wallet_address(public_key_pem)
        return cls(username, address, public_key_pem, private_key_hex)
    
    @classmethod
    def load(cls, filename: str) -> Optional['Wallet']:
        if not os.path.exists(filename):
            return None
        with open(filename, 'r') as f:
            data = json.load(f)
        return cls(
            username=data.get('username', ''),
            address=data['address'],
            public_key_pem=data['public_key_pem'],
            private_key_hex=data['private_key_hex']
        )
    
    def save(self, filename: str):
        with open(filename, 'w') as f:
            json.dump({
                'username': self.username,
                'address': self.address,
                'public_key_pem': self.public_key_pem,
                'private_key_hex': self.private_key_hex,
                'created_at': time.time()
            }, f, indent=2)

# ==================== PC MINER ====================
class PCMiner:
    def __init__(self, wallet: Wallet):
        self.wallet = wallet
        self.validator_id = wallet.get_validator_id()
        self.node_urls = resolve_dns_seeds()
        self.current_node_index = 0
        self.current_node_url = self.node_urls[0]
        
        self.websocket = None
        self.is_validator = False
        self.current_challenge = ""
        self.current_block_id = 0
        self.last_challenge_time = 0
        self.last_uptime_ping = 0
        self.last_status_report = 0
        self.start_time = time.time()
        self.reconnect_attempts = 0
        self.node_switch_count = 0
        self.uptime_seconds = 0
        self.today_uptime = 0
        self.last_uptime_reset = time.time()
        self.connected = False
        self.mining = True
        self.running = True
        
        self.total_rewards = 0
        self.blocks_signed = 0
        self.consecutive_misses = 0
        self.slash_count = 0
        self.current_stake = INITIAL_STAKE
        self.current_level = self.calculate_level()
        
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        self.load_stats()
    
    def signal_handler(self, signum, frame):
        print("\n[SHUTDOWN] Stopping miner...")
        self.running = False
        self.mining = False
        self.save_stats()
        sys.exit(0)
    
    def calculate_level(self) -> int:
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
    
    def init_database(self):
        import sqlite3
        self.conn = sqlite3.connect('pc_miner_stats.db')
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS miner_stats
                     (key TEXT PRIMARY KEY, value REAL, updated_at REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS blocks_mined
                     (block_id INTEGER PRIMARY KEY, timestamp REAL, reward INTEGER, node TEXT, challenge TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS missed_blocks
                     (block_id INTEGER PRIMARY KEY, timestamp REAL, reason TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS node_switches
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, from_node TEXT, to_node TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS slash_events
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, amount INTEGER, reason TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS daily_uptime
                     (date TEXT PRIMARY KEY, uptime_seconds INTEGER)''')
        self.conn.commit()
    
    def load_stats(self):
        import sqlite3
        self.init_database()
        c = self.conn.cursor()
        c.execute("SELECT value FROM miner_stats WHERE key = 'total_rewards'")
        row = c.fetchone()
        if row:
            self.total_rewards = int(row[0])
        c.execute("SELECT value FROM miner_stats WHERE key = 'blocks_signed'")
        row = c.fetchone()
        if row:
            self.blocks_signed = int(row[0])
        c.execute("SELECT value FROM miner_stats WHERE key = 'slash_count'")
        row = c.fetchone()
        if row:
            self.slash_count = int(row[0])
        c.execute("SELECT value FROM miner_stats WHERE key = 'stake'")
        row = c.fetchone()
        if row:
            self.current_stake = int(row[0])
            self.current_level = self.calculate_level()
        c.execute("SELECT value FROM miner_stats WHERE key = 'consecutive_misses'")
        row = c.fetchone()
        if row:
            self.consecutive_misses = int(row[0])
        c.execute("SELECT value FROM miner_stats WHERE key = 'today_uptime'")
        row = c.fetchone()
        if row:
            self.today_uptime = int(row[0])
    
    def save_stats(self):
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO miner_stats VALUES (?, ?, ?)",
                 ('total_rewards', self.total_rewards, time.time()))
        c.execute("INSERT OR REPLACE INTO miner_stats VALUES (?, ?, ?)",
                 ('blocks_signed', self.blocks_signed, time.time()))
        c.execute("INSERT OR REPLACE INTO miner_stats VALUES (?, ?, ?)",
                 ('slash_count', self.slash_count, time.time()))
        c.execute("INSERT OR REPLACE INTO miner_stats VALUES (?, ?, ?)",
                 ('stake', self.current_stake, time.time()))
        c.execute("INSERT OR REPLACE INTO miner_stats VALUES (?, ?, ?)",
                 ('consecutive_misses', self.consecutive_misses, time.time()))
        c.execute("INSERT OR REPLACE INTO miner_stats VALUES (?, ?, ?)",
                 ('today_uptime', self.today_uptime, time.time()))
        self.conn.commit()
    
    def record_node_switch(self, from_node: str, to_node: str):
        c = self.conn.cursor()
        c.execute("INSERT INTO node_switches (timestamp, from_node, to_node) VALUES (?, ?, ?)",
                 (time.time(), from_node, to_node))
        self.conn.commit()
    
    def record_miss(self, block_id: int, reason: str = "Timeout"):
        self.consecutive_misses += 1
        print(f"⚠️ Missed block {block_id} | Consecutive misses: {self.consecutive_misses}")
        c = self.conn.cursor()
        c.execute("INSERT INTO missed_blocks VALUES (?, ?, ?)",
                 (block_id, time.time(), reason))
        self.conn.commit()
    
    def switch_to_next_node(self):
        old_node = self.current_node_url
        self.current_node_index = (self.current_node_index + 1) % len(self.node_urls)
        self.current_node_url = self.node_urls[self.current_node_index]
        self.node_switch_count += 1
        self.record_node_switch(old_node, self.current_node_url)
        print(f"\n[FAILOVER] Switching to node: {self.current_node_url} (switch #{self.node_switch_count})\n")
    
    def add_reward(self, reward: int, block_id: int = 0, challenge: str = ""):
        self.total_rewards += reward
        self.current_stake += reward
        self.blocks_signed += 1
        self.consecutive_misses = 0
        self.current_level = self.calculate_level()
        self.save_stats()
        
        c = self.conn.cursor()
        c.execute("INSERT INTO blocks_mined VALUES (?, ?, ?, ?, ?)",
                 (block_id, time.time(), reward, self.current_node_url, challenge[:32]))
        self.conn.commit()
        
        print(f"\n💰 REWARD: +{reward} MCX | Total: {self.total_rewards} | Stake: {self.current_stake} | Level: {self.current_level} | Blocks: {self.blocks_signed}\n")
    
    def handle_slash(self, amount: int = 0, reason: str = "Missed signing"):
        if amount == 0:
            amount = max(int(self.current_stake * SLASH_RATE), LEVEL_STAKE_RANGE)
        
        self.current_stake -= amount
        if self.current_stake < LEVEL_STAKE_RANGE:
            self.current_stake = LEVEL_STAKE_RANGE
        
        self.slash_count += 1
        self.consecutive_misses += 1
        self.current_level = self.calculate_level()
        self.save_stats()
        
        c = self.conn.cursor()
        c.execute("INSERT INTO slash_events (timestamp, amount, reason) VALUES (?, ?, ?)",
                 (time.time(), amount, reason))
        self.conn.commit()
        
        print(f"\n⚠️ SLASHED: -{amount} MCX | New stake: {self.current_stake} | Level: {self.current_level} | Slashes: {self.slash_count}\n")
        return self.slash_count < 5
    
    async def send_message(self, msg_type: str, **kwargs):
        message = {"type": msg_type, **kwargs}
        if self.websocket:
            try:
                await self.websocket.send(json.dumps(message))
                return True
            except Exception as e:
                print(f"[SEND] Error: {e}")
                return False
        return False
    
    async def register(self):
        timestamp = time.time()
        reg_message = f"{self.validator_id}{self.wallet.username}{self.current_stake}{timestamp}"
        signature = sign_message(self.wallet.get_private_key(), reg_message)
        
        await self.send_message(
            "register",
            validator_id=self.validator_id,
            username=self.wallet.username,
            public_key=self.wallet.public_key_pem,
            wallet=self.wallet.address,
            stake=self.current_stake,
            level=self.current_level,
            rewards=self.total_rewards,
            blocks=self.blocks_signed,
            uptime=int(self.uptime_seconds),
            today_uptime=self.today_uptime,
            miner_type="pc",
            timestamp=timestamp,
            signature=signature
        )
        print(f"📡 Registered with node as '{self.wallet.username}' (ID: {self.validator_id[:16]}...)")
    
    async def send_uptime_ping(self):
        self.uptime_seconds = int(time.time() - self.start_time)
        self.update_today_uptime()
        await self.send_message(
            "uptime_ping",
            validator_id=self.validator_id,
            username=self.wallet.username,
            uptime_seconds=self.uptime_seconds,
            today_uptime=self.today_uptime,
            stake=self.current_stake,
            level=self.current_level
        )
    
    async def sign_block(self):
        message_to_sign = f"{self.current_challenge}{self.validator_id}{self.current_block_id}"
        signature = sign_message(self.wallet.get_private_key(), message_to_sign)
        
        await self.send_message(
            "block_signature",
            validator_id=self.validator_id,
            username=self.wallet.username,
            challenge=self.current_challenge,
            signature=signature,
            level=self.current_level,
            stake=self.current_stake,
            block_id=self.current_block_id,
            timestamp=time.time()
        )
        print(f"✍️ Signed block {self.current_block_id}")
    
    async def handle_message(self, message: dict):
        msg_type = message.get("type")
        
        if msg_type == "registered":
            self.connected = True
            print(f"✅ Registration confirmed | Level: {message.get('level')} | Max Level: {message.get('max_level')}")
            print(f"💰 Current reward: {message.get('current_reward')} MCX per block")
            print(f"📊 Remaining supply: {message.get('remaining_supply', 0):,} MCX\n")
        
        elif msg_type == "challenge":
            self.current_challenge = message.get("challenge", "")
            self.current_block_id = message.get("block_id", 0)
            self.last_challenge_time = time.time()
            self.is_validator = True
            await self.sign_block()
            
            if hasattr(self, 'challenge_timeout_task'):
                self.challenge_timeout_task.cancel()
            
            async def timeout_handler():
                await asyncio.sleep(SIGNING_WINDOW_MS / 1000)
                if self.is_validator:
                    print(f"⏰ TIMEOUT: Failed to sign block {self.current_block_id}")
                    self.record_miss(self.current_block_id, "Timeout")
                    if not self.handle_slash():
                        await self.websocket.close()
                    self.is_validator = False
            
            self.challenge_timeout_task = asyncio.create_task(timeout_handler())
        
        elif msg_type == "block_accepted":
            if hasattr(self, 'challenge_timeout_task'):
                self.challenge_timeout_task.cancel()
            reward = message.get("reward", 0)
            self.add_reward(reward, self.current_block_id, self.current_challenge)
            self.is_validator = False
            print(f"✅ Block {message.get('block_id')} ACCEPTED | Reward: {reward} MCX")
        
        elif msg_type == "block_rejected":
            if hasattr(self, 'challenge_timeout_task'):
                self.challenge_timeout_task.cancel()
            reason = message.get("reason", "Unknown")
            print(f"❌ Block {message.get('block_id')} REJECTED: {reason}")
            self.is_validator = False
        
        elif msg_type == "slash":
            print(f"⚠️ SLASH command received from node")
            amount = message.get("amount", 0)
            reason = message.get("reason", "Node slashing")
            if not self.handle_slash(amount, reason):
                await self.websocket.close()
            self.is_validator = False
        
        elif msg_type == "level_update":
            new_stake = message.get("stake", self.current_stake)
            if new_stake != self.current_stake:
                self.current_stake = new_stake
                self.current_level = self.calculate_level()
                self.save_stats()
                print(f"📊 Level update: Stake {self.current_stake} MCX | Level {self.current_level}")
        
        elif msg_type == "miner_control":
            action = message.get("action")
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
        
        elif msg_type == "status":
            data = message.get("data", {})
            print(f"\n📊 NETWORK STATUS")
            print(f"   Block: {data.get('block_id')} | Reward: {data.get('current_reward')} MCX")
            print(f"   Miners: {data.get('total_miners')} | Active: {data.get('active_miners')}")
            print(f"   Supply: {data.get('total_minted', 0):,} / {data.get('remaining_supply', 0):,} MCX")
            print(f"   Price: ${data.get('mcx_price', 0.01):.4f}\n")
    
    async def listen(self):
        self.reconnect_attempts = 0
        self.connected = False
        
        while self.running:
            try:
                print(f"[CONN] Connecting to {self.current_node_url}...")
                async with websockets.connect(
                    self.current_node_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as ws:
                    self.websocket = ws
                    self.connected = True
                    self.reconnect_attempts = 0
                    
                    print(f"🔌 Connected to node at {self.current_node_url}")
                    await self.register()
                    
                    async for raw_message in ws:
                        try:
                            message = json.loads(raw_message)
                            await self.handle_message(message)
                        except json.JSONDecodeError as e:
                            print(f"[ERROR] Invalid JSON: {e}")
                        except Exception as e:
                            print(f"[ERROR] Message handling: {e}")
                            traceback.print_exc()
                        
                        if time.time() - self.last_uptime_ping > UPTIME_PING_INTERVAL:
                            await self.send_uptime_ping()
                            self.last_uptime_ping = time.time()
                        
                        if time.time() - self.last_status_report > STATUS_INTERVAL:
                            self.print_status()
                            self.last_status_report = time.time()
                        
                        if self.is_validator and (time.time() - self.last_challenge_time) > (SIGNING_WINDOW_MS / 1000 + 0.5):
                            print(f"⏰ Fallback timeout! Missed block {self.current_block_id}")
                            self.record_miss(self.current_block_id, "Fallback timeout")
                            if not self.handle_slash():
                                await self.websocket.close()
                                break
                            self.is_validator = False
                    
            except websockets.exceptions.ConnectionClosed as e:
                print(f"Connection closed: {e}")
                self.connected = False
            except Exception as e:
                print(f"Connection error: {e}")
                traceback.print_exc()
            
            if not self.running or not self.mining:
                break
            
            self.reconnect_attempts += 1
            if self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                self.switch_to_next_node()
                self.reconnect_attempts = 0
            
            delay = RECONNECT_DELAY * min(self.reconnect_attempts + 1, 10)
            print(f"Reconnecting in {delay} seconds... (Attempt {self.reconnect_attempts})")
            await asyncio.sleep(delay)
    
    def print_status(self):
        uptime_hours = self.uptime_seconds / 3600
        today_hours = self.today_uptime / 3600
        success_rate = 0
        total_attempts = self.blocks_signed + self.consecutive_misses
        if total_attempts > 0:
            success_rate = (self.blocks_signed / total_attempts) * 100
        
        print(f"\n{'='*60}")
        print(f"MICROCORE (MCX) PC MINER STATUS")
        print(f"{'='*60}")
        print(f"Username: {self.wallet.username}")
        print(f"Wallet: {self.wallet.address[:24]}...")
        print(f"Validator ID: {self.validator_id[:20]}...")
        print(f"{'-'*40}")
        print(f"Level: {self.current_level} / 100")
        print(f"Stake: {self.current_stake:,} MCX")
        print(f"Rewards: {self.total_rewards:,} MCX")
        print(f"Blocks Signed: {self.blocks_signed}")
        print(f"Missed Blocks: {self.consecutive_misses}")
        print(f"Success Rate: {success_rate:.1f}%")
        print(f"Slash Count: {self.slash_count} / 5")
        print(f"{'-'*40}")
        print(f"Uptime: {uptime_hours:.1f} hours")
        print(f"Today's Uptime: {today_hours:.1f}h / 24h")
        print(f"Current Node: {self.current_node_url}")
        print(f"Node Switches: {self.node_switch_count}")
        print(f"Status: {'🟢 Mining' if self.mining else '🔴 Stopped'}")
        print(f"Connected: {'✅ Yes' if self.connected else '❌ No'}")
        print(f"{'='*60}\n")
    
    async def run(self):
        print(f"\n{'='*60}")
        print(f"MICROCORE (MCX) PC MINER v3.0")
        print(f"{'='*60}")
        print(f"Username: {self.wallet.username}")
        print(f"Wallet: {self.wallet.address}")
        print(f"Validator ID: {self.validator_id[:20]}...")
        print(f"{'-'*40}")
        print(f"Initial Stake: {self.current_stake} MCX")
        print(f"Initial Level: {self.current_level}")
        print(f"Stake Range per Level: {LEVEL_STAKE_RANGE} MCX")
        print(f"Signing Window: {SIGNING_WINDOW_MS} ms")
        print(f"Slash Rate: {SLASH_RATE * 100}%")
        print(f"{'-'*40}")
        print(f"Discovered Nodes: {len(self.node_urls)}")
        for i, url in enumerate(self.node_urls):
            print(f"  {i+1}. {url}")
        print(f"{'='*60}\n")
        print("Starting miner... Press Ctrl+C to stop\n")
        
        await self.listen()

# ==================== SYSTEM INFO ====================
def print_system_info():
    import platform
    print(f"System: {platform.system()} {platform.release()}")
    print(f"Python: {platform.python_version()}")

# ==================== MAIN ====================
async def main():
    print("\n" + "=" * 60)
    print("🔷 MICROCORE (MCX) PC MINER v3.0 🔷")
    print("Real ECDSA | Auto Node Discovery | Failover | Level System")
    print("=" * 60)
    print_system_info()
    print("=" * 60)
    
    global USERNAME
    wallet = Wallet.load(WALLET_FILE)
    if not wallet:
        print(f"\n[WALLET] No wallet found.")
        if USERNAME:
            username = USERNAME
        else:
            username = input("Enter your username: ").strip()
            if not username:
                username = f"pc_miner_{int(time.time())}"
        
        wallet = Wallet.create_new(username)
        wallet.save(WALLET_FILE)
        print(f"\n✅ New wallet created!")
        print(f"   Username: {wallet.username}")
        print(f"   Address: {wallet.address}")
        print(f"\n⚠️ BACKUP THIS FILE: {os.path.abspath(WALLET_FILE)}")
        print(f"⚠️ NEVER share your private key or wallet file!\n")
    else:
        print(f"\n[WALLET] Wallet loaded")
        print(f"   Username: {wallet.username}")
        print(f"   Address: {wallet.address[:32]}...")
    
    if USERNAME and USERNAME != wallet.username:
        print(f"\n⚠️ Updating username from '{wallet.username}' to '{USERNAME}'")
        wallet.username = USERNAME
        wallet.save(WALLET_FILE)
    
    miner = PCMiner(wallet)
    
    try:
        await miner.run()
    except asyncio.CancelledError:
        print("\n[SHUTDOWN] Miner cancelled")
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Miner stopped by user")
    finally:
        miner.save_stats()
        print(f"\n📊 FINAL STATS")
        print(f"   Rewards: {miner.total_rewards} MCX")
        print(f"   Blocks: {miner.blocks_signed}")
        print(f"   Slashes: {miner.slash_count}")
        print(f"   Node Switches: {miner.node_switch_count}")
        print(f"   Final Stake: {miner.current_stake} MCX")
        print(f"   Final Level: {miner.current_level}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[EXIT] Goodbye!")
        sys.exit(0)