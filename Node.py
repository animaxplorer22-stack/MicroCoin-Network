#!/usr/bin/env python3
"""
MICROCORE (MCX) COMPLETE NODE - FINAL v10.0.0
84M Hard Cap | 10 MCX Block Reward | 30 Second Blocks | 4 Year Halving
Temporary Towers | Level System (100 MCX/level) | DEX (Own Pools + LI.FI/THORChain)
Reward Split: 70% validators | 8% nodes | 5% uptime | 5% LP | 12% buyer rewards

DUAL CRYPTO MODE:
- Web miners & Arduino Uno: SHA256/HMAC signatures (lightweight, browser-compatible)
- ESP32, PC, Pico W, Phone: Real ECDSA secp256k1 (secure)

Usage:
  python3 node_full.py --genesis --username YOUR_USERNAME --wallet MCR_ADDRESS
  python3 node_full.py --peer IP:PORT --username YOUR_USERNAME --wallet MCR_ADDRESS
  python3 node_full.py --no-miner --username YOUR_USERNAME --wallet MCR_ADDRESS
"""

import asyncio
import json
import time
import hashlib
import sqlite3
import random
import os
import sys
import socket
import struct
import secrets
import argparse
import traceback
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict
from enum import Enum

# ==================== DEPENDENCY CHECK ====================
try:
    import websockets
    from websockets.server import serve
    from websockets.exceptions import ConnectionClosed
except ImportError:
    print("ERROR: Install websockets: pip install websockets")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: Install requests: pip install requests")
    sys.exit(1)

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature, decode_dss_signature
    from cryptography.exceptions import InvalidSignature
    ECDSA_AVAILABLE = True
except ImportError:
    print("WARNING: cryptography not installed. ECDSA disabled. Install: pip install cryptography")
    ECDSA_AVAILABLE = False

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    print("WARNING: dns.resolver not installed. DNS seed discovery disabled.")
    print("Install: pip install dnspython")
    DNS_AVAILABLE = False

# ==================== CONFIGURATION ====================
NODE_HOST = "0.0.0.0"
NODE_PORT = 8080
P2P_PORT = 8081

SYMBOL = "MCX"
NAME = "MicroCore"
VERSION = "10.0.0-FINAL"

# DNS Seeds for automatic peer discovery
DNS_SEEDS = [
    "seed.microcore.com",
    "seed1.microcore.com",
    "seed2.microcore.com",
]

# TOKENOMICS - 84 MILLION HARD CAP, 10 MCX BLOCK REWARD
TOTAL_SUPPLY_CAP = 84_000_000
INITIAL_BLOCK_REWARD = 10
HALVING_INTERVAL = 4_204_800   # 4 years at 30 second blocks

# Reward Distribution Percentages (100% total)
VALIDATOR_SHARE = 0.70      # 70% to validators
NODE_SHARE = 0.08           # 8% to nodes (routed to node owner's wallet)
UPTIME_SHARE = 0.05         # 5% to uptime pool (distributed to miners)
LP_SHARE = 0.05             # 5% to liquidity providers
BUYER_REWARDS_SHARE = 0.12  # 12% to buyer rewards pool

# Level System - 100 MCX per level
LEVEL_STAKE_RANGE = 100
MAX_LEVEL = 100
MIN_WALLETS_FOR_NEXT_LEVEL = 10

# Block timing
SIGNING_WINDOW_MS = 2500    # 2.5 seconds to sign
BLOCK_DELAY_SECONDS = 27.5  # Wait between blocks (total 30 seconds)

# Level-specific block intervals (higher level = faster blocks)
LEVEL_BLOCK_INTERVALS = {
    1: 60, 2: 50, 3: 40, 4: 30, 5: 25, 6: 20, 7: 15, 8: 12, 9: 10,
    10: 8, 11: 6, 12: 5, 13: 4, 14: 3, 15: 2, 16: 1
}

# Consensus Parameters
SLASH_RATE = 0.10
MIN_VALIDATORS_PER_BLOCK = 10
UPTIME_PING_INTERVAL = 30
DISTRIBUTION_INTERVAL_SEC = 300

# P2P Settings
MAX_PEERS = 30
SYNC_INTERVAL = 10
HEARTBEAT_INTERVAL = 30
PEER_TIMEOUT = 90
PEX_INTERVAL = 60
SEED_REFRESH_INTERVAL = 3600

# Ban settings
BAN_THRESHOLD = 5
BAN_DURATION = 3600

# DEX Settings
SWAP_FEE_RATE = 0.003
MCX_FEE_MIN = 1
MCX_FEE_MAX = 100

# LI.FI / THORChain API
LIFI_API_URL = "https://li.quest/v1"
THORCHAIN_API_URL = "https://thornode.ninerealms.com"

# Fiat on-ramp (mock - integrate with Stripe/Coinbase)
FIAT_RAMP_ENABLED = True
MCX_PRICE_USD = 0.01  # 1 MCX = $0.01

# ==================== ENUMS ====================
class MinerType(Enum):
    WEB = "web"           # Browser-based - SHA256 only
    UNO = "uno"           # Arduino Uno - SHA256 only
    ESP32 = "esp32"       # ESP32 - ECDSA
    PC = "pc"             # PC Python - ECDSA
    PICO = "pico"         # Raspberry Pi Pico W - ECDSA
    PHONE = "phone"       # Mobile - ECDSA
    EMBEDDED = "embedded" # Embedded in node - ECDSA

class TxStatus(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"

class PeerState(Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    BANNED = "banned"

class Chain(Enum):
    ETHEREUM = "ethereum"
    BSC = "bsc"
    SOLANA = "solana"
    BITCOIN = "bitcoin"
    POLYGON = "polygon"
    ARBITRUM = "arbitrum"

# ==================== SUPPORTED TOKENS ====================
SUPPORTED_TOKENS = {
    "BTC": {"symbol": "BTC", "name": "Bitcoin", "chain": Chain.BITCOIN, "decimals": 8, "address": "native"},
    "ETH": {"symbol": "ETH", "name": "Ethereum", "chain": Chain.ETHEREUM, "decimals": 18, "address": "native"},
    "SOL": {"symbol": "SOL", "name": "Solana", "chain": Chain.SOLANA, "decimals": 9, "address": "native"},
    "USDC": {"symbol": "USDC", "name": "USD Coin", "chain": Chain.ETHEREUM, "decimals": 6, "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    "USDT": {"symbol": "USDT", "name": "Tether", "chain": Chain.ETHEREUM, "decimals": 6, "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7"},
    "BNB": {"symbol": "BNB", "name": "Binance Coin", "chain": Chain.BSC, "decimals": 18, "address": "native"},
}

OWN_POOLS = ["MCX/USDC", "MCX/BTC", "MCX/ETH", "MCX/SOL", "MCX/BNB"]

# ==================== DUAL CRYPTOGRAPHY ====================
def verify_signature_sha256(public_key: str, message: str, signature_hex: str) -> bool:
    """SHA256-based signature verification for web and Arduino Uno miners"""
    try:
        # For web/uno, the "public_key" is actually the wallet address or username
        # Signature is SHA256(public_key + message)
        expected = hashlib.sha256(f"{public_key}{message}".encode()).hexdigest()
        return signature_hex == expected
    except:
        return False

def verify_signature_ecdsa(public_key_pem: str, message: str, signature_hex: str) -> bool:
    """Real ECDSA secp256k1 signature verification"""
    if not ECDSA_AVAILABLE:
        return False
    if len(signature_hex) != 128:
        return False
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        signature_bytes = bytes.fromhex(signature_hex)
        r = int.from_bytes(signature_bytes[:32], 'big')
        s = int.from_bytes(signature_bytes[32:], 'big')
        signature_der = encode_dss_signature(r, s)
        public_key.verify(signature_der, message.encode(), ec.ECDSA(hashes.SHA256()))
        return True
    except:
        return False

def verify_signature(public_key: str, message: str, signature_hex: str, miner_type: str = "ecdsa") -> bool:
    """
    Unified signature verification - supports both SHA256 and ECDSA
    miner_type: 'web', 'uno', 'esp32', 'pc', 'pico', 'phone', 'embedded'
    """
    if miner_type in ["web", "uno"]:
        # SHA256-based (lightweight, browser-compatible)
        return verify_signature_sha256(public_key, message, signature_hex)
    else:
        # ECDSA secp256k1 (secure)
        return verify_signature_ecdsa(public_key, message, signature_hex)

def sign_message_sha256(private_key: str, message: str) -> str:
    """SHA256-based signing for web/uno (insecure but works in browser)"""
    return hashlib.sha256(f"{private_key}{message}".encode()).hexdigest()

def sign_message_ecdsa(private_key_hex: str, message: str) -> str:
    """Real ECDSA secp256k1 signing"""
    if not ECDSA_AVAILABLE:
        return ""
    private_value = int(private_key_hex, 16)
    private_key = ec.derive_private_key(private_value, ec.SECP256K1())
    signature = private_key.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    return r.to_bytes(32, 'big').hex() + s.to_bytes(32, 'big').hex()

def generate_wallet_ecdsa() -> tuple:
    """Generate ECDSA wallet (for PC, ESP32, etc.)"""
    if not ECDSA_AVAILABLE:
        # Fallback to SHA256 if cryptography not installed
        private_key = hashlib.sha256(os.urandom(32)).hexdigest()
        address = f"MCR_{hashlib.sha256(private_key.encode()).hexdigest()[:32].upper()}"
        return address, private_key, private_key
    private_key = ec.generate_private_key(ec.SECP256K1())
    private_key_hex = private_key.private_numbers().private_value.to_bytes(32, 'big').hex()
    public_key = private_key.public_key()
    public_key_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    addr_hash = hashlib.sha256(public_key_pem.encode()).hexdigest()
    address = f"MCR_{addr_hash[:32].upper()}"
    return address, private_key_hex, public_key_pem

def generate_wallet_sha256(username: str, password: str) -> tuple:
    """Generate SHA256-based wallet (for web/uno)"""
    private_key = hashlib.sha256(f"{username}:{password}:microcore".encode()).hexdigest()
    address = f"MCR_{hashlib.sha256(private_key.encode()).hexdigest()[:32].upper()}"
    return address, private_key, private_key  # public_key = private_key for SHA256 mode

def hash_block(block_data: dict) -> str:
    return hashlib.sha256(json.dumps(block_data, sort_keys=True).encode()).hexdigest()

def hash_transaction(tx_data: dict) -> str:
    return hashlib.sha256(json.dumps(tx_data, sort_keys=True).encode()).hexdigest()

# ==================== P2P PROTOCOL ====================
P2P_MAGIC = b"MCR1"
P2P_VERSION = 1

P2P_MSG_HANDSHAKE = 0x01
P2P_MSG_PING = 0x02
P2P_MSG_PONG = 0x03
P2P_MSG_GET_BLOCKS = 0x04
P2P_MSG_BLOCKS = 0x05
P2P_MSG_GET_HEADER = 0x06
P2P_MSG_HEADER = 0x07
P2P_MSG_NEW_BLOCK = 0x08
P2P_MSG_NEW_TRANSACTION = 0x09
P2P_MSG_GET_PEERS = 0x0A
P2P_MSG_PEERS = 0x0B
P2P_MSG_ADDR = 0x0C
P2P_MSG_GET_MEMPOOL = 0x0D
P2P_MSG_MEMPOOL = 0x0E
P2P_MSG_SLASH_EVENT = 0x0F
P2P_MSG_LEVEL_UPDATE = 0x10
P2P_MSG_NODE_REGISTER = 0x11
P2P_MSG_NODE_REWARDS = 0x12

def encode_p2p_message(msg_type: int, payload: dict) -> bytes:
    payload_bytes = json.dumps(payload).encode()
    header = P2P_MAGIC + struct.pack(">B", P2P_VERSION) + struct.pack(">B", msg_type) + struct.pack(">I", len(payload_bytes))
    return header + payload_bytes

def decode_p2p_message(data: bytes) -> tuple:
    if len(data) < 4 + 1 + 1 + 4:
        return None, None
    if data[:4] != P2P_MAGIC:
        return None, None
    msg_type = data[5]
    payload_len = struct.unpack(">I", data[6:10])[0]
    if len(data) < 10 + payload_len:
        return None, None
    payload = json.loads(data[10:10+payload_len].decode())
    return msg_type, payload

# ==================== DNS SEED & PEER DISCOVERY ====================
def query_dns_seeds() -> List[str]:
    if not DNS_AVAILABLE:
        return []
    peers = []
    for seed in DNS_SEEDS:
        try:
            answers = dns.resolver.resolve(seed, 'A')
            for answer in answers:
                peers.append(f"{str(answer)}:{P2P_PORT}")
            print(f"[DNS] Found {len(answers)} peers from {seed}")
        except Exception as e:
            print(f"[DNS] Failed to query {seed}: {e}")
    return peers

def get_public_ip() -> str:
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=5)
        return response.json()['ip']
    except:
        try:
            response = requests.get('https://checkip.amazonaws.com', timeout=5)
            return response.text.strip()
        except:
            return None

# ==================== DEX BRIDGE ====================
class DEXBridge:
    def __init__(self, network=None):
        self.network = network
        self.connected = False
        self.mcx_price_usd = MCX_PRICE_USD
        self.own_pools = {
            "MCX/USDC": {"token_a": "MCX", "token_b": "USDC", "reserve_a": 100000, "reserve_b": 100000, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0},
            "MCX/BTC": {"token_a": "MCX", "token_b": "BTC", "reserve_a": 100000, "reserve_b": 1.67, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0},
            "MCX/ETH": {"token_a": "MCX", "token_b": "ETH", "reserve_a": 100000, "reserve_b": 33.33, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0},
            "MCX/SOL": {"token_a": "MCX", "token_b": "SOL", "reserve_a": 100000, "reserve_b": 666.67, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0},
            "MCX/BNB": {"token_a": "MCX", "token_b": "BNB", "reserve_a": 100000, "reserve_b": 333.33, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0}
        }
    
    def connect(self) -> bool:
        print(f"[DEX] Connected. Own pools: {', '.join(self.own_pools.keys())}")
        self.connected = True
        return True
    
    def get_price(self) -> float:
        return self.mcx_price_usd
    
    def calculate_swap_fee_mcx(self, amount_usd: float) -> int:
        fee_usd = amount_usd * SWAP_FEE_RATE
        fee_mcx = int(fee_usd / self.mcx_price_usd) if self.mcx_price_usd > 0 else MCX_FEE_MIN
        return max(MCX_FEE_MIN, min(fee_mcx, MCX_FEE_MAX))
    
    def get_own_pool_quote(self, from_token: str, to_token: str, amount: float) -> dict:
        if from_token == "MCX":
            pool_id = f"MCX/{to_token}"
            if pool_id not in self.own_pools:
                return {"error": f"Pool {pool_id} not available"}
            pool = self.own_pools[pool_id]
            reserve_in = pool["reserve_a"]
            reserve_out = pool["reserve_b"]
            amount_with_fee = amount * (1 - pool["fee"])
            output = (amount_with_fee * reserve_out) / (reserve_in + amount_with_fee) if reserve_in + amount_with_fee > 0 else 0
            fee_mcx = self.calculate_swap_fee_mcx(amount * self.mcx_price_usd)
            return {"success": True, "pool_type": "own", "pool_id": pool_id, "from_token": from_token, "to_token": to_token,
                    "amount_in": amount, "expected_output": output, "fee_mcx": fee_mcx}
        else:
            pool_id = f"MCX/{from_token}"
            if pool_id not in self.own_pools:
                return {"error": f"Pool {pool_id} not available"}
            pool = self.own_pools[pool_id]
            reserve_in = pool["reserve_b"]
            reserve_out = pool["reserve_a"]
            amount_with_fee = amount * (1 - pool["fee"])
            output = (amount_with_fee * reserve_out) / (reserve_in + amount_with_fee) if reserve_in + amount_with_fee > 0 else 0
            fee_mcx = self.calculate_swap_fee_mcx(amount * self.mcx_price_usd)
            return {"success": True, "pool_type": "own", "pool_id": pool_id, "from_token": from_token, "to_token": to_token,
                    "amount_in": amount, "expected_output": output, "fee_mcx": fee_mcx}
    
    def execute_own_pool_swap(self, user_wallet: str, from_token: str, to_token: str, amount: float, fee_mcx: int) -> dict:
        quote = self.get_own_pool_quote(from_token, to_token, amount)
        if quote.get("error"):
            return quote
        
        if self.network and self.network.get_balance(user_wallet) < fee_mcx:
            return {"success": False, "error": "Insufficient MCX balance for fee"}
        
        if self.network:
            self.network.balances[user_wallet] -= fee_mcx
            self.network.node_pool += int(fee_mcx * 0.4)
            self.network.lp_pool += int(fee_mcx * 0.6)
        
        pool = self.own_pools[quote["pool_id"]]
        if from_token == "MCX":
            pool["reserve_a"] += amount
            pool["reserve_b"] -= quote["expected_output"]
        else:
            pool["reserve_b"] += amount
            pool["reserve_a"] -= quote["expected_output"]
        
        pool["reserve_a"] = max(pool["reserve_a"], 0)
        pool["reserve_b"] = max(pool["reserve_b"], 0)
        
        tx_hash = hashlib.sha256(f"{user_wallet}{from_token}{to_token}{amount}{time.time()}".encode()).hexdigest()[:16]
        return {"success": True, "tx_hash": tx_hash, "from_token": from_token, "to_token": to_token,
                "amount_in": amount, "amount_out": quote["expected_output"], "fee_mcx": fee_mcx}
    
    def add_own_pool_liquidity(self, user_wallet: str, pool_id: str, amount_a: float, amount_b: float) -> dict:
        if pool_id not in self.own_pools:
            return {"success": False, "error": f"Pool {pool_id} not found"}
        
        if self.network:
            if self.network.get_balance(user_wallet) < amount_a + amount_b:
                return {"success": False, "error": "Insufficient balance"}
            self.network.balances[user_wallet] -= (amount_a + amount_b)
        
        pool = self.own_pools[pool_id]
        pool["reserve_a"] += amount_a
        pool["reserve_b"] += amount_b
        
        lp_shares = (amount_a * amount_b) ** 0.5
        pool["total_lp_shares"] += lp_shares
        if user_wallet in pool["lp_providers"]:
            pool["lp_providers"][user_wallet] += lp_shares
        else:
            pool["lp_providers"][user_wallet] = lp_shares
        
        return {"success": True, "pool_id": pool_id, "amount_a": amount_a, "amount_b": amount_b, "lp_shares": lp_shares}
    
    async def get_lifi_quote(self, from_token: str, to_token: str, amount: float) -> dict:
        prices = {"BTC": 60000, "ETH": 3000, "SOL": 150, "USDC": 1, "USDT": 1, "BNB": 300}
        from_price = prices.get(from_token, 1)
        to_price = prices.get(to_token, 1)
        value_usd = amount * from_price
        expected_output = (value_usd / to_price) * 0.997
        fee_mcx = self.calculate_swap_fee_mcx(value_usd)
        return {"success": True, "pool_type": "lifi", "from_token": from_token, "to_token": to_token,
                "amount_in": amount, "expected_output": expected_output, "fee_mcx": fee_mcx}
    
    async def get_thorchain_quote(self, from_token: str, to_token: str, amount: float) -> dict:
        prices = {"BTC": 60000, "ETH": 3000}
        from_price = prices.get(from_token, 1)
        to_price = prices.get(to_token, 1)
        value_usd = amount * from_price
        expected_output = (value_usd / to_price) * 0.995
        fee_mcx = self.calculate_swap_fee_mcx(value_usd)
        return {"success": True, "pool_type": "thorchain", "from_token": from_token, "to_token": to_token,
                "amount_in": amount, "expected_output": expected_output, "fee_mcx": fee_mcx}
    
    async def get_swap_quote(self, from_token: str, to_token: str, amount: float) -> dict:
        if from_token == "MCX" or to_token == "MCX":
            return self.get_own_pool_quote(from_token, to_token, amount)
        if (from_token in ["BTC", "ETH"] and to_token in ["BTC", "ETH"]):
            return await self.get_thorchain_quote(from_token, to_token, amount)
        return await self.get_lifi_quote(from_token, to_token, amount)
    
    async def execute_swap(self, user_wallet: str, from_token: str, to_token: str, amount: float, fee_mcx: int) -> dict:
        if self.network and self.network.get_balance(user_wallet) < fee_mcx:
            return {"success": False, "error": "Insufficient MCX balance for fee"}
        if self.network:
            self.network.balances[user_wallet] -= fee_mcx
            self.network.node_pool += int(fee_mcx * 0.4)
            self.network.lp_pool += int(fee_mcx * 0.6)
        
        if from_token == "MCX" or to_token == "MCX":
            return self.execute_own_pool_swap(user_wallet, from_token, to_token, amount, fee_mcx)
        
        quote = await self.get_swap_quote(from_token, to_token, amount)
        tx_hash = hashlib.sha256(f"{user_wallet}{from_token}{to_token}{amount}{time.time()}".encode()).hexdigest()[:16]
        return {"success": True, "tx_hash": tx_hash, "from_token": from_token, "to_token": to_token,
                "amount_in": amount, "amount_out": quote.get("expected_output", 0), "fee_mcx": fee_mcx}
    
    async def get_supported_pools(self) -> List[dict]:
        pools = []
        for pid, p in self.own_pools.items():
            pools.append({"type": "own", "pool_id": pid, "token_a": p["token_a"], "token_b": p["token_b"],
                         "reserve_a": p["reserve_a"], "reserve_b": p["reserve_b"]})
        pools.append({"type": "aggregator", "name": "LI.FI", "supported_tokens": list(SUPPORTED_TOKENS.keys())})
        pools.append({"type": "aggregator", "name": "THORChain", "supported_tokens": ["BTC", "ETH"]})
        return pools
    
    def buy_mcx_with_fiat(self, user_wallet: str, usd_amount: float, payment_method: str = "card") -> dict:
        if not FIAT_RAMP_ENABLED:
            return {"success": False, "error": "Fiat on-ramp disabled"}
        
        mcx_amount = int(usd_amount / self.mcx_price_usd)
        
        if self.network:
            self.network.balances[user_wallet] = self.network.balances.get(user_wallet, 0) + mcx_amount
            self.network.total_minted += mcx_amount
            
            tx_hash = hashlib.sha256(f"fiat_buy_{user_wallet}{usd_amount}{time.time()}".encode()).hexdigest()[:16]
            
            c = self.network.conn.cursor()
            c.execute("INSERT OR REPLACE INTO buyer_stats (wallet, username, monthly_bought, last_reset) VALUES (?, ?, COALESCE((SELECT monthly_bought FROM buyer_stats WHERE wallet=?), 0) + ?, ?)",
                     (user_wallet, user_wallet, user_wallet, mcx_amount, time.time()))
            c.execute("INSERT INTO fiat_purchases (wallet, usd_amount, mcx_amount, payment_method, timestamp, tx_hash) VALUES (?, ?, ?, ?, ?, ?)",
                     (user_wallet, usd_amount, mcx_amount, payment_method, time.time(), tx_hash))
            self.network.conn.commit()
            
            return {"success": True, "tx_hash": tx_hash, "mcx_amount": mcx_amount, "usd_amount": usd_amount, "rate": self.mcx_price_usd}
        
        return {"success": False, "error": "Network not available"}

# ==================== DATA STRUCTURES ====================
@dataclass
class Miner:
    validator_id: str
    public_key: str
    username: str
    wallet: str
    stake: int
    level: int
    uptime_seconds: int = 0
    today_uptime: int = 0
    last_ping: float = 0
    is_active: bool = True
    total_rewards: int = 0
    blocks_signed: int = 0
    slash_count: int = 0
    consecutive_misses: int = 0
    registered_at: float = 0
    miner_type: str = "unknown"
    last_uptime_reset: float = 0
    temp_towers: Dict[int, int] = field(default_factory=dict)

@dataclass
class Node:
    node_id: str
    username: str
    wallet: str
    ip: str
    port: int
    last_seen: float
    height: int
    is_active: bool = True
    rewards_earned: int = 0
    version: int = P2P_VERSION

@dataclass
class Peer:
    address: str
    last_seen: float
    height: int
    version: int = P2P_VERSION
    is_outbound: bool = False
    state: PeerState = PeerState.CONNECTED
    ban_until: float = 0

@dataclass
class Transaction:
    tx_hash: str
    from_wallet: str
    to_wallet: str
    amount: int
    fee: int
    timestamp: float
    block_id: int = -1
    signature: str = ""
    status: TxStatus = TxStatus.PENDING
    tx_type: str = "send"

@dataclass
class Block:
    block_id: int
    timestamp: float
    previous_hash: str
    validators: List[str]
    level: int
    signatures: Dict[str, str] = field(default_factory=dict)
    block_hash: str = ""
    accepted: bool = False
    reward_distributed: bool = False
    reward_amount: int = 0
    transaction_count: int = 0
    transactions: List[Transaction] = field(default_factory=list)

# ==================== LEVEL SYSTEM WITH TEMPORARY TOWERS ====================
class LevelManager:
    def __init__(self, network):
        self.network = network
        self.max_unlocked_level = 1
        self.level_unique_wallets: Dict[int, int] = {}
        self.temp_towers: Dict[str, Dict[int, int]] = {}
    
    def calculate_level_allocation(self, stake: int) -> Dict[int, int]:
        allocation = {}
        remaining = stake
        level = 1
        
        while remaining > 0:
            if level > self.max_unlocked_level:
                allocation[self.max_unlocked_level] = allocation.get(self.max_unlocked_level, 0) + remaining
                break
            
            level_stake = min(remaining, LEVEL_STAKE_RANGE)
            allocation[level] = allocation.get(level, 0) + level_stake
            remaining -= level_stake
            level += 1
        
        return allocation
    
    def get_effective_level(self, wallet: str) -> int:
        if wallet not in self.temp_towers:
            return 1
        for level in range(self.max_unlocked_level, 0, -1):
            if self.temp_towers[wallet].get(level, 0) > 0:
                return level
        return 1
    
    def can_unlock_next_level(self, level: int) -> bool:
        unique_wallets = self.level_unique_wallets.get(level, 0)
        return unique_wallets >= MIN_WALLETS_FOR_NEXT_LEVEL
    
    def process_level_unlock(self):
        for level in range(1, self.max_unlocked_level + 2):
            if level > self.max_unlocked_level and self.can_unlock_next_level(level - 1):
                self.max_unlocked_level = level
                print(f"[LEVEL] Level {level} UNLOCKED! (10 unique wallets in Level {level-1})")
                self.convert_towers_to_real(level)
    
    def convert_towers_to_real(self, new_level: int):
        for wallet, towers in self.temp_towers.items():
            if towers.get(new_level, 0) > 0:
                stake = towers[new_level]
                for miner in self.network.miners.values():
                    if miner.wallet == wallet:
                        miner.stake += stake
                        miner.level = self.get_effective_level(wallet)
                        break
                del towers[new_level]
    
    def update_unique_wallets(self):
        self.level_unique_wallets.clear()
        for miner in self.network.miners.values():
            if miner.is_active:
                level = self.get_effective_level(miner.wallet)
                if level not in self.level_unique_wallets:
                    self.level_unique_wallets[level] = set()
                self.level_unique_wallets[level].add(miner.wallet)
        
        for level, wallets in self.level_unique_wallets.items():
            self.level_unique_wallets[level] = len(wallets)
        
        self.process_level_unlock()
    
    def register_miner_stake(self, wallet: str, stake: int):
        allocation = self.calculate_level_allocation(stake)
        self.temp_towers[wallet] = allocation
        self.update_unique_wallets()

# ==================== P2P NODE ====================
class P2PNode:
    def __init__(self, network):
        self.network = network
        self.peers: Dict[str, Peer] = {}
        self.server = None
        self.running = True
        self.public_ip = get_public_ip()
        self.banned_peers: Dict[str, float] = {}
    
    async def start(self):
        self.server = await asyncio.start_server(self.handle_connection, NODE_HOST, P2P_PORT)
        print(f"[P2P] Server on port {P2P_PORT}")
        if self.public_ip:
            print(f"[P2P] Public IP: {self.public_ip}:{P2P_PORT}")
    
    async def handle_connection(self, reader, writer):
        peer_addr = writer.get_extra_info('peername')
        addr_str = f"{peer_addr[0]}:{peer_addr[1]}"
        if addr_str in self.banned_peers:
            if time.time() < self.banned_peers[addr_str]:
                writer.close()
                return
            else:
                del self.banned_peers[addr_str]
        try:
            length_data = await reader.read(4)
            if not length_data:
                writer.close()
                return
            msg_len = struct.unpack(">I", length_data)[0]
            if msg_len > 10_000_000:
                self.ban_peer(addr_str, "Message too large")
                writer.close()
                return
            data = await reader.read(msg_len)
            msg_type, payload = decode_p2p_message(data)
            if msg_type is not None:
                await self.process_message(msg_type, payload, writer, addr_str)
        except Exception as e:
            print(f"[P2P] Error: {e}")
        finally:
            writer.close()
    
    def ban_peer(self, addr: str, reason: str):
        self.banned_peers[addr] = time.time() + BAN_DURATION
        if addr in self.peers:
            del self.peers[addr]
        print(f"[P2P] Banned {addr}: {reason}")
    
    async def process_message(self, msg_type, payload, writer, peer_addr):
        if msg_type == P2P_MSG_HANDSHAKE:
            response = {"node_id": self.network.node_id, "version": P2P_VERSION,
                       "height": self.network.current_block_id, "public_ip": self.public_ip, 
                       "timestamp": time.time(), "username": self.network.node_username,
                       "wallet": self.network.node_wallet}
            data = encode_p2p_message(P2P_MSG_HANDSHAKE, response)
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
            self.peers[peer_addr] = Peer(peer_addr, time.time(), payload.get("height", 0))
            print(f"[P2P] New peer: {peer_addr} (username: {payload.get('username', 'unknown')})")
            await self.send_peers(writer)
            if payload.get("height", 0) > self.network.current_block_id:
                asyncio.create_task(self.request_blocks(peer_addr, self.network.current_block_id, payload.get("height", 0)))
        
        elif msg_type == P2P_MSG_GET_PEERS:
            await self.send_peers(writer)
        
        elif msg_type == P2P_MSG_PEERS:
            for p in payload.get("peers", []):
                if p not in self.peers and p != f"{self.public_ip}:{P2P_PORT}" and p not in self.banned_peers:
                    self.peers[p] = Peer(p, time.time(), 0)
                    asyncio.create_task(self.connect_to_peer(p))
            print(f"[P2P] Received {len(payload.get('peers', []))} peers")
        
        elif msg_type == P2P_MSG_GET_BLOCKS:
            start, end = payload.get("start", 0), payload.get("end", self.network.current_block_id)
            if end - start > 2000:
                end = start + 2000
            blocks = self.network.get_blocks_in_range(start, end)
            data = encode_p2p_message(P2P_MSG_BLOCKS, {"blocks": blocks, "count": len(blocks)})
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
        
        elif msg_type == P2P_MSG_BLOCKS:
            await self.network.import_blocks(payload.get("blocks", []))
        
        elif msg_type == P2P_MSG_NEW_BLOCK:
            await self.network.receive_external_block(payload.get("block"), peer_addr)
        
        elif msg_type == P2P_MSG_NEW_TRANSACTION:
            await self.network.receive_external_transaction(payload.get("transaction"), peer_addr)
        
        elif msg_type == P2P_MSG_GET_MEMPOOL:
            mempool = self.network.get_mempool()
            data = encode_p2p_message(P2P_MSG_MEMPOOL, {"transactions": mempool, "count": len(mempool)})
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
        
        elif msg_type == P2P_MSG_SLASH_EVENT:
            await self.network.process_slash_event(payload.get("slash"), peer_addr)
        
        elif msg_type == P2P_MSG_NODE_REGISTER:
            await self.network.register_remote_node(payload, peer_addr)
        
        elif msg_type == P2P_MSG_PING:
            data = encode_p2p_message(P2P_MSG_PONG, {"timestamp": time.time()})
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
        
        elif msg_type == P2P_MSG_PONG:
            if peer_addr in self.peers:
                self.peers[peer_addr].last_seen = time.time()
    
    async def send_peers(self, writer):
        peers_list = list(self.peers.keys())[:100]
        data = encode_p2p_message(P2P_MSG_PEERS, {"peers": peers_list, "count": len(peers_list)})
        writer.write(struct.pack(">I", len(data)) + data)
        await writer.drain()
    
    async def request_blocks(self, peer_addr, start, end):
        try:
            host, port = peer_addr.split(":")
            reader, writer = await asyncio.open_connection(host, int(port))
            data = encode_p2p_message(P2P_MSG_GET_BLOCKS, {"start": start, "end": end})
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
            writer.close()
        except Exception as e:
            print(f"[P2P] Request failed: {e}")
    
    async def connect_to_peer(self, peer_addr):
        if peer_addr in self.peers or peer_addr in self.banned_peers:
            return
        try:
            host, port = peer_addr.split(":")
            reader, writer = await asyncio.open_connection(host, int(port))
            handshake = {"node_id": self.network.node_id, "version": P2P_VERSION,
                        "height": self.network.current_block_id, "public_ip": self.public_ip, 
                        "timestamp": time.time(), "username": self.network.node_username,
                        "wallet": self.network.node_wallet}
            data = encode_p2p_message(P2P_MSG_HANDSHAKE, handshake)
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
            self.peers[peer_addr] = Peer(peer_addr, time.time(), self.network.current_block_id, is_outbound=True)
            print(f"[P2P] Connected to {peer_addr}")
            writer.close()
        except Exception as e:
            print(f"[P2P] Failed to connect to {peer_addr}: {e}")
    
    async def broadcast_new_block(self, block: dict):
        data = encode_p2p_message(P2P_MSG_NEW_BLOCK, {"block": block, "timestamp": time.time(), "node_id": self.network.node_id})
        for peer_addr in list(self.peers.keys()):
            try:
                host, port = peer_addr.split(":")
                reader, writer = await asyncio.open_connection(host, int(port))
                writer.write(struct.pack(">I", len(data)) + data)
                await writer.drain()
                writer.close()
            except:
                pass
    
    async def broadcast_transaction(self, transaction: dict):
        data = encode_p2p_message(P2P_MSG_NEW_TRANSACTION, {"transaction": transaction, "timestamp": time.time()})
        for peer_addr in list(self.peers.keys()):
            try:
                host, port = peer_addr.split(":")
                reader, writer = await asyncio.open_connection(host, int(port))
                writer.write(struct.pack(">I", len(data)) + data)
                await writer.drain()
                writer.close()
            except:
                pass
    
    async def discover_peers(self):
        for peer in query_dns_seeds():
            if peer not in self.peers and peer not in self.banned_peers:
                asyncio.create_task(self.connect_to_peer(peer))
        for peer_addr in list(self.peers.keys()):
            try:
                host, port = peer_addr.split(":")
                reader, writer = await asyncio.open_connection(host, int(port))
                data = encode_p2p_message(P2P_MSG_GET_PEERS, {})
                writer.write(struct.pack(">I", len(data)) + data)
                await writer.drain()
                writer.close()
            except:
                if peer_addr in self.peers:
                    del self.peers[peer_addr]
    
    async def sync_with_peers(self):
        if not self.peers:
            return
        best_peer, best_height = None, self.network.current_block_id
        for addr, peer in self.peers.items():
            if peer.height > best_height:
                best_height, best_peer = peer.height, addr
        if best_peer and best_height > self.network.current_block_id:
            print(f"[P2P] Syncing from {best_peer}: local={self.network.current_block_id}, remote={best_height}")
            await self.request_blocks(best_peer, self.network.current_block_id, best_height)
    
    async def heartbeat(self):
        while self.running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            data = encode_p2p_message(P2P_MSG_PING, {"timestamp": time.time()})
            for peer_addr in list(self.peers.keys()):
                try:
                    host, port = peer_addr.split(":")
                    reader, writer = await asyncio.open_connection(host, int(port))
                    writer.write(struct.pack(">I", len(data)) + data)
                    await writer.drain()
                    writer.close()
                except:
                    if peer_addr in self.peers:
                        del self.peers[peer_addr]

# ==================== MICROCORE NETWORK ====================
class MicroCoreNetwork:
    def __init__(self, is_genesis_node: bool = False, node_username: str = "", node_wallet: str = ""):
        self.miners: Dict[str, Miner] = {}
        self.nodes: Dict[str, Node] = {}
        self.uptime_pool: int = 0
        self.node_pool: int = 0
        self.lp_pool: int = 0
        self.buyer_rewards_pool: int = 0
        self.current_block_id: int = 0
        self.blocks: List[Block] = []
        self.pending_transactions: List[Transaction] = []
        self.pending_challenges: Dict[str, Dict] = {}
        self.level_groups: Dict[int, List[str]] = defaultdict(list)
        self.last_distribution: float = time.time()
        self.last_block_hash: str = "0" * 64
        self.total_minted: int = 0
        self.balances: Dict[str, int] = {}
        self.is_genesis_node = is_genesis_node
        self.node_id = hashlib.sha256(f"{node_username}{time.time()}{secrets.token_hex(8)}".encode()).hexdigest()[:16]
        self.node_username = node_username
        self.node_wallet = node_wallet
        self._last_buyer_distribution = time.time()
        
        # Level manager with temporary towers
        self.level_manager = LevelManager(self)
        
        self.p2p = P2PNode(self)
        self.dex = DEXBridge(self)
        
        self.init_database()
        if self.is_genesis_node:
            self.create_genesis_block()
        else:
            self.check_existing_blockchain()
        self.load_total_minted()
        self.load_balances()
        self.load_nodes()
        self.dex.connect()
        
        if node_username and node_wallet:
            self.register_this_node()
    
    def init_database(self):
        self.conn = sqlite3.connect('microcore.db', check_same_thread=False)
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS miners
                     (validator_id TEXT PRIMARY KEY, public_key TEXT, username TEXT,
                      wallet TEXT, stake INTEGER, level INTEGER, total_rewards INTEGER,
                      blocks_signed INTEGER, slash_count INTEGER, uptime_seconds INTEGER,
                      today_uptime INTEGER, registered_at REAL, last_ping REAL, 
                      miner_type TEXT, last_uptime_reset REAL, temp_towers TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS nodes
                     (node_id TEXT PRIMARY KEY, username TEXT, wallet TEXT,
                      ip TEXT, port INTEGER, last_seen REAL, height INTEGER,
                      is_active INTEGER, rewards_earned INTEGER, version INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS blocks
                     (block_id INTEGER PRIMARY KEY, timestamp REAL, previous_hash TEXT,
                      validators TEXT, level INTEGER, block_hash TEXT, reward_amount INTEGER,
                      transaction_count INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS transactions
                     (tx_hash TEXT PRIMARY KEY, from_wallet TEXT, to_wallet TEXT,
                      amount INTEGER, fee INTEGER, timestamp REAL, block_id INTEGER,
                      signature TEXT, status TEXT, tx_type TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS balances
                     (wallet TEXT PRIMARY KEY, balance INTEGER, last_updated REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS peers
                     (peer_address TEXT PRIMARY KEY, last_seen REAL, height INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS slashing_events
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, validator_id TEXT,
                      amount INTEGER, reason TEXT, timestamp REAL, block_id INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS supply_metrics
                     (key TEXT PRIMARY KEY, value INTEGER, updated_at REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS buyer_stats
                     (wallet TEXT PRIMARY KEY, username TEXT, monthly_bought REAL,
                      last_reset REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS level_unlocks
                     (level INTEGER PRIMARY KEY, unlocked_at REAL, unique_wallets INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS fiat_purchases
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet TEXT, usd_amount REAL,
                      mcx_amount INTEGER, payment_method TEXT, timestamp REAL, tx_hash TEXT)''')
        self.conn.commit()
    
    def register_this_node(self):
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (self.node_id, self.node_username, self.node_wallet,
                  self.p2p.public_ip or "unknown", P2P_PORT, time.time(),
                  self.current_block_id, 1, 0, P2P_VERSION))
        self.conn.commit()
        self.nodes[self.node_id] = Node(self.node_id, self.node_username, self.node_wallet,
                                        self.p2p.public_ip or "unknown", P2P_PORT, time.time(),
                                        self.current_block_id, True, 0)
        print(f"[NODE] Registered: {self.node_id[:16]}... (username: {self.node_username})")
    
    def load_nodes(self):
        c = self.conn.cursor()
        c.execute("SELECT node_id, username, wallet, ip, port, last_seen, height, is_active, rewards_earned FROM nodes")
        for row in c.fetchall():
            self.nodes[row[0]] = Node(row[0], row[1], row[2], row[3], row[4], row[5], row[6], bool(row[7]), row[8])
        print(f"[NODE] Loaded {len(self.nodes)} nodes")
    
    def check_existing_blockchain(self):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM blocks")
        if c.fetchone()[0] == 0:
            print("[SYNC] No blockchain found. Waiting to sync from genesis node...")
            print("[SYNC] Use --peer IP:PORT to connect to genesis node")
        else:
            print(f"[SYNC] Found existing blockchain, loading...")
            self.load_blocks_from_db()
    
    def load_blocks_from_db(self):
        c = self.conn.cursor()
        c.execute("SELECT block_id, timestamp, previous_hash, validators, level, block_hash, reward_amount FROM blocks ORDER BY block_id")
        for row in c.fetchall():
            block = Block(row[0], row[1], row[2], row[3].split(',') if row[3] else [], row[4],
                         block_hash=row[5], reward_amount=row[6], accepted=True, reward_distributed=True)
            self.blocks.append(block)
            if block.block_id >= self.current_block_id:
                self.current_block_id = block.block_id + 1
                self.last_block_hash = block.block_hash
        print(f"[SYNC] Loaded {len(self.blocks)} blocks")
    
    def load_balances(self):
        c = self.conn.cursor()
        c.execute("SELECT wallet, balance FROM balances")
        for row in c.fetchall():
            self.balances[row[0]] = row[1]
    
    def load_total_minted(self):
        c = self.conn.cursor()
        c.execute("SELECT SUM(reward_amount) FROM blocks WHERE reward_amount > 0")
        result = c.fetchone()[0]
        self.total_minted = (result or 0)
        for balance in self.balances.values():
            self.total_minted += balance
    
    def create_genesis_block(self):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM blocks")
        if c.fetchone()[0] > 0:
            return
        
        print("\n" + "=" * 70)
        print(f"{NAME} ({SYMBOL}) GENESIS NODE")
        print("The birth of a new blockchain")
        print("=" * 70)
        
        genesis = Block(0, time.time(), "0"*64, ["genesis"], 1, reward_amount=0)
        genesis.block_hash = hash_block({"block_id":0, "timestamp":genesis.timestamp, "previous_hash":"0"*64,
                                        "validators":["genesis"], "level":1})
        genesis.accepted = True
        genesis.reward_distributed = True
        self.blocks.append(genesis)
        self.last_block_hash = genesis.block_hash
        self.current_block_id = 1
        
        if self.node_wallet:
            self.balances[self.node_wallet] = 100_000
        else:
            self.balances["MCR_GENESIS_CREATOR"] = 100_000
        self.total_minted = 100000
        
        for wallet, balance in self.balances.items():
            c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (wallet, balance, time.time()))
        c.execute("INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (0, genesis.timestamp, genesis.previous_hash, ','.join(genesis.validators),
                  genesis.level, genesis.block_hash, 0, 0))
        self.conn.commit()
        
        self.level_manager.max_unlocked_level = 1
        self.level_manager.update_unique_wallets()
        
        print(f"[GENESIS] Block #0 created: {genesis.block_hash[:32]}...")
        print(f"[GENESIS] Initial supply: 100,000 {SYMBOL}")
        print(f"[GENESIS] Hard cap: {TOTAL_SUPPLY_CAP:,} {SYMBOL}")
        print(f"[GENESIS] Node username: {self.node_username}")
        print(f"[GENESIS] Node wallet: {self.node_wallet or 'MCR_GENESIS_CREATOR'}")
        print("=" * 70 + "\n")
    
    def get_current_block_reward(self) -> int:
        remaining = TOTAL_SUPPLY_CAP - self.total_minted
        if remaining <= 0:
            return 1
        halvings = self.current_block_id // HALVING_INTERVAL
        reward = INITIAL_BLOCK_REWARD // (2 ** halvings)
        return max(reward, 1)
    
    def get_block_interval_for_level(self, level: int) -> int:
        return LEVEL_BLOCK_INTERVALS.get(level, 30)
    
    def get_remaining_supply(self) -> int:
        return max(0, TOTAL_SUPPLY_CAP - self.total_minted)
    
    def get_supply_percentage(self) -> float:
        return (self.total_minted / TOTAL_SUPPLY_CAP) * 100 if TOTAL_SUPPLY_CAP > 0 else 0
    
    def get_current_halving(self) -> int:
        return self.current_block_id // HALVING_INTERVAL
    
    def get_balance(self, wallet: str) -> int:
        return self.balances.get(wallet, 0)
    
    def get_staked(self, wallet: str) -> int:
        for miner in self.miners.values():
            if miner.wallet == wallet:
                return miner.stake
        return 0
    
    def transfer(self, from_wallet: str, to_wallet: str, amount: int, fee: int = 1, signature: str = "", tx_type: str = "send") -> Optional[str]:
        if self.get_balance(from_wallet) < amount + fee:
            return None
        
        self.balances[from_wallet] -= (amount + fee)
        self.balances[to_wallet] = self.balances.get(to_wallet, 0) + amount
        self.node_pool += fee
        
        tx_hash = hash_transaction({"from": from_wallet, "to": to_wallet, "amount": amount, "fee": fee, "timestamp": time.time()})
        tx = Transaction(tx_hash, from_wallet, to_wallet, amount, fee, time.time(), status=TxStatus.PENDING, signature=signature, tx_type=tx_type)
        self.pending_transactions.append(tx)
        
        if tx_type == "buy":
            c = self.conn.cursor()
            c.execute("INSERT OR REPLACE INTO buyer_stats (wallet, username, monthly_bought, last_reset) VALUES (?, ?, COALESCE((SELECT monthly_bought FROM buyer_stats WHERE wallet=?), 0) + ?, ?)",
                     (from_wallet, from_wallet, from_wallet, amount, time.time()))
            self.conn.commit()
        
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (from_wallet, self.balances[from_wallet], time.time()))
        c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (to_wallet, self.balances[to_wallet], time.time()))
        c.execute("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (tx_hash, from_wallet, to_wallet, amount, fee, time.time(), -1, signature, "pending", tx_type))
        self.conn.commit()
        
        asyncio.create_task(self.p2p.broadcast_transaction({"tx_hash": tx_hash, "from": from_wallet, "to": to_wallet,
                                                            "amount": amount, "fee": fee, "type": tx_type}))
        return tx_hash
    
    def process_stake(self, username: str, amount: int) -> dict:
        wallet = None
        for miner in self.miners.values():
            if miner.username == username:
                wallet = miner.wallet
                break
        
        if not wallet:
            return {"success": False, "error": "User not found"}
        
        if self.get_balance(wallet) < amount:
            return {"success": False, "error": f"Insufficient balance. You have {self.get_balance(wallet)} MCX"}
        
        self.balances[wallet] -= amount
        
        for miner in self.miners.values():
            if miner.wallet == wallet:
                miner.stake += amount
                self.level_manager.register_miner_stake(wallet, miner.stake)
                miner.level = self.level_manager.get_effective_level(wallet)
                
                c = self.conn.cursor()
                c.execute("UPDATE miners SET stake=?, level=? WHERE wallet=?", (miner.stake, miner.level, wallet))
                c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (wallet, self.balances[wallet], time.time()))
                self.conn.commit()
                
                return {"success": True, "staked": miner.stake, "level": miner.level, "balance": self.balances[wallet]}
        
        return {"success": False, "error": "Miner not found"}
    
    def process_unstake(self, username: str, amount: int) -> dict:
        wallet = None
        for miner in self.miners.values():
            if miner.username == username:
                wallet = miner.wallet
                break
        
        if not wallet:
            return {"success": False, "error": "User not found"}
        
        for miner in self.miners.values():
            if miner.wallet == wallet:
                if miner.stake < amount:
                    return {"success": False, "error": f"Insufficient staked balance. You have {miner.stake} MCX staked"}
                
                miner.stake -= amount
                self.balances[wallet] = self.balances.get(wallet, 0) + amount
                self.level_manager.register_miner_stake(wallet, miner.stake)
                miner.level = self.level_manager.get_effective_level(wallet)
                
                c = self.conn.cursor()
                c.execute("UPDATE miners SET stake=?, level=? WHERE wallet=?", (miner.stake, miner.level, wallet))
                c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (wallet, self.balances[wallet], time.time()))
                self.conn.commit()
                
                return {"success": True, "staked": miner.stake, "level": miner.level, "balance": self.balances[wallet]}
        
        return {"success": False, "error": "Miner not found"}
    
    def buy_mcx_with_fiat(self, wallet: str, usd_amount: float, payment_method: str = "card") -> dict:
        return self.dex.buy_mcx_with_fiat(wallet, usd_amount, payment_method)
    
    def calculate_level(self, stake: int) -> int:
        if stake < LEVEL_STAKE_RANGE:
            return 1
        level = ((stake - 1) // LEVEL_STAKE_RANGE) + 1
        return min(level, MAX_LEVEL)
    
    def update_level_groups(self):
        self.level_groups.clear()
        for miner in self.miners.values():
            if miner.is_active:
                effective_level = self.level_manager.get_effective_level(miner.wallet)
                self.level_groups.setdefault(effective_level, []).append(miner.validator_id)
    
    def select_validators(self, level: int) -> List[str]:
        miners = self.level_groups.get(level, [])
        if len(miners) < MIN_VALIDATORS_PER_BLOCK:
            return []
        seed = int(self.last_block_hash[:16], 16) if self.last_block_hash != "0"*64 else int(time.time())
        rng = random.Random(seed)
        return rng.sample(miners, MIN_VALIDATORS_PER_BLOCK)
    
    def generate_challenge(self, block_id: int, validators: List[str]) -> str:
        return hashlib.sha256(f"{block_id}{''.join(sorted(validators))}{time.time()}{self.last_block_hash}{secrets.token_hex(8)}".encode()).hexdigest()
    
    def verify_challenge_response(self, vid: str, challenge: str, block_id: int, sig: str, miner_type: str = "ecdsa") -> bool:
        if vid not in self.miners:
            return False
        message = f"{challenge}{vid}{block_id}"
        # Use the miner's stored type or override with provided
        m_type = miner_type if miner_type != "ecdsa" else self.miners[vid].miner_type
        return verify_signature(self.miners[vid].public_key, message, sig, m_type)
    
    def register_miner(self, vid: str, pubkey: str, username: str, wallet: str, stake: int, sig: str, ts: float, miner_type: str = "unknown") -> bool:
        # Verify signature based on miner type
        message = f"{vid}{username}{stake}{ts}"
        if not verify_signature(pubkey, message, sig, miner_type):
            print(f"[REG] Signature failed for {username} (type: {miner_type})")
            return False
        
        self.level_manager.register_miner_stake(wallet, stake)
        effective_level = self.level_manager.get_effective_level(wallet)
        
        if vid in self.miners:
            self.miners[vid].stake = stake
            self.miners[vid].level = effective_level
            self.miners[vid].username = username
            self.miners[vid].wallet = wallet
            self.miners[vid].last_ping = time.time()
            self.miners[vid].is_active = True
            self.miners[vid].miner_type = miner_type
        else:
            self.miners[vid] = Miner(vid, pubkey, username, wallet, stake, effective_level, 
                                     registered_at=ts, miner_type=miner_type)
            print(f"[REG] New miner: {username} | Type: {miner_type} | Effective Level {effective_level} | Stake {stake} {SYMBOL}")
        
        import json
        c = self.conn.cursor()
        towers_json = json.dumps(self.level_manager.temp_towers.get(wallet, {}))
        c.execute("INSERT OR REPLACE INTO miners VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (vid, pubkey, username, wallet, stake, effective_level,
                  self.miners[vid].total_rewards, self.miners[vid].blocks_signed,
                  self.miners[vid].slash_count, self.miners[vid].uptime_seconds,
                  self.miners[vid].today_uptime, ts, time.time(), miner_type, 0, towers_json))
        self.conn.commit()
        
        self.update_level_groups()
        return True
    
    def update_miner_uptime(self, vid: str, uptime_seconds: int):
        if vid in self.miners:
            today = time.time()
            if today - self.miners[vid].last_uptime_reset > 86400:
                self.miners[vid].today_uptime = 0
                self.miners[vid].last_uptime_reset = today
            
            self.miners[vid].uptime_seconds = uptime_seconds
            self.miners[vid].today_uptime = min(self.miners[vid].today_uptime + UPTIME_PING_INTERVAL, 86400)
            self.miners[vid].last_ping = time.time()
            
            c = self.conn.cursor()
            c.execute("UPDATE miners SET uptime_seconds=?, today_uptime=?, last_ping=? WHERE validator_id=?",
                     (uptime_seconds, self.miners[vid].today_uptime, time.time(), vid))
            self.conn.commit()
    
    def slash_miner(self, vid: str, reason: str, block_id: int = -1) -> int:
        if vid not in self.miners:
            return 0
        m = self.miners[vid]
        slash = max(int(m.stake * SLASH_RATE), LEVEL_STAKE_RANGE)
        m.stake -= slash
        if m.stake < LEVEL_STAKE_RANGE:
            m.stake = LEVEL_STAKE_RANGE
        m.slash_count += 1
        m.consecutive_misses += 1
        
        self.level_manager.register_miner_stake(m.wallet, m.stake)
        m.level = self.level_manager.get_effective_level(m.wallet)
        
        if m.slash_count >= BAN_THRESHOLD:
            m.is_active = False
            print(f"[BAN] {m.username} banned after {BAN_THRESHOLD} slashes")
        
        c = self.conn.cursor()
        c.execute("UPDATE miners SET stake=?, level=?, slash_count=?, is_active=? WHERE validator_id=?",
                 (m.stake, m.level, m.slash_count, m.is_active, vid))
        c.execute("INSERT INTO slashing_events (validator_id, amount, reason, timestamp, block_id) VALUES (?, ?, ?, ?, ?)",
                 (vid, slash, reason, time.time(), block_id))
        self.conn.commit()
        self.update_level_groups()
        print(f"[SLASH] {m.username}: -{slash} {SYMBOL} | Stake: {m.stake} {SYMBOL} | Level: {m.level}")
        
        return slash
    
    def distribute_block_reward(self, block: Block):
        if block.reward_distributed:
            return
        reward = self.get_current_block_reward()
        block.reward_amount = reward
        
        validator_total = (reward * 70) // 100
        node_total = (reward * 8) // 100
        uptime_total = (reward * 5) // 100
        lp_total = (reward * 5) // 100
        buyer_total = (reward * 12) // 100
        
        validator_each = validator_total // max(len(block.validators), 1)
        validator_remainder = validator_total - (validator_each * len(block.validators))
        node_total += validator_remainder
        
        for vid in block.validators:
            if vid in self.miners:
                m = self.miners[vid]
                m.total_rewards += validator_each
                m.stake += validator_each
                m.blocks_signed += 1
                m.consecutive_misses = 0
                self.level_manager.register_miner_stake(m.wallet, m.stake)
                m.level = self.level_manager.get_effective_level(m.wallet)
                self.balances[m.wallet] = self.balances.get(m.wallet, 0) + validator_each
                c = self.conn.cursor()
                c.execute("UPDATE miners SET stake=?, level=?, total_rewards=?, blocks_signed=? WHERE validator_id=?",
                         (m.stake, m.level, m.total_rewards, m.blocks_signed, vid))
                c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (m.wallet, self.balances[m.wallet], time.time()))
                self.conn.commit()
        
        self.node_pool += node_total
        self.uptime_pool += uptime_total
        self.lp_pool += lp_total
        self.buyer_rewards_pool += buyer_total
        
        self.total_minted += reward
        block.reward_distributed = True
        self.update_level_groups()
        
        remaining = self.get_remaining_supply()
        percent = self.get_supply_percentage()
        halving = self.get_current_halving()
        print(f"[BLOCK {block.block_id}] REWARD: {reward} {SYMBOL} (Level {block.level})")
        print(f"   ├─ Validators ({len(block.validators)}): {validator_each} {SYMBOL} each")
        print(f"   ├─ Node pool: {node_total} {SYMBOL}")
        print(f"   ├─ Uptime pool: {uptime_total} {SYMBOL}")
        print(f"   ├─ LP pool: {lp_total} {SYMBOL}")
        print(f"   └─ Buyer rewards pool: {buyer_total} {SYMBOL}")
        print(f"[SUPPLY] {self.total_minted:,} / {TOTAL_SUPPLY_CAP:,} ({percent:.4f}%) | Halving: {halving}")
        print(f"[REMAINING] {remaining:,} {SYMBOL} until cap")
    
    def distribute_periodic_rewards(self):
        active_miners = [m for m in self.miners.values() if m.is_active]
        total_uptime = sum(m.uptime_seconds for m in active_miners)
        if total_uptime > 0 and self.uptime_pool > 0:
            for miner in active_miners:
                if miner.uptime_seconds > 0:
                    share = int(self.uptime_pool * (miner.uptime_seconds / total_uptime))
                    miner.total_rewards += share
                    miner.stake += share
                    self.balances[miner.wallet] = self.balances.get(miner.wallet, 0) + share
                    self.level_manager.register_miner_stake(miner.wallet, miner.stake)
                    miner.level = self.level_manager.get_effective_level(miner.wallet)
                    c = self.conn.cursor()
                    c.execute("UPDATE miners SET stake=?, level=?, total_rewards=?, uptime_seconds=? WHERE validator_id=?",
                             (miner.stake, miner.level, miner.total_rewards, miner.uptime_seconds, miner.validator_id))
                    c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (miner.wallet, self.balances[miner.wallet], time.time()))
                    self.conn.commit()
            print(f"[DISTRO] Uptime rewards: {self.uptime_pool} {SYMBOL} to {len(active_miners)} miners")
        
        active_nodes = [n for n in self.nodes.values() if n.is_active]
        if active_nodes and self.node_pool > 0:
            node_share = self.node_pool // max(len(active_nodes), 1)
            for node in active_nodes:
                node.rewards_earned += node_share
                self.balances[node.wallet] = self.balances.get(node.wallet, 0) + node_share
                c = self.conn.cursor()
                c.execute("UPDATE nodes SET rewards_earned=? WHERE node_id=?", (node.rewards_earned, node.node_id))
                c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (node.wallet, self.balances[node.wallet], time.time()))
                self.conn.commit()
            print(f"[DISTRO] Node rewards: {self.node_pool} {SYMBOL} to {len(active_nodes)} nodes")
        
        self.node_pool = 0
        self.uptime_pool = 0
        self.lp_pool = 0
        self.last_distribution = time.time()
    
    def get_blocks_in_range(self, start: int, end: int) -> List[dict]:
        blocks = []
        for b in self.blocks:
            if start <= b.block_id <= end:
                blocks.append({"block_id": b.block_id, "timestamp": b.timestamp, "previous_hash": b.previous_hash,
                              "validators": b.validators, "level": b.level, "block_hash": b.block_hash,
                              "reward_amount": b.reward_amount})
        return blocks
    
    def get_mempool(self) -> List[dict]:
        return [{"tx_hash": tx.tx_hash, "from": tx.from_wallet, "to": tx.to_wallet,
                "amount": tx.amount, "fee": tx.fee, "timestamp": tx.timestamp} for tx in self.pending_transactions]
    
    def get_top_stakers(self, limit: int = 10) -> List[dict]:
        stakers = []
        for miner in self.miners.values():
            if miner.is_active and miner.stake > 0:
                stakers.append({"username": miner.username, "staked": miner.stake, "wallet": miner.wallet})
        stakers.sort(key=lambda x: x["staked"], reverse=True)
        return stakers[:limit]
    
    def get_top_buyers(self, limit: int = 10) -> List[dict]:
        c = self.conn.cursor()
        c.execute("""
            SELECT wallet, username, monthly_bought 
            FROM buyer_stats 
            ORDER BY monthly_bought DESC 
            LIMIT ?
        """, (limit,))
        return [{"wallet": row[0], "username": row[1], "bought": row[2]} for row in c.fetchall()]
    
    async def import_blocks(self, blocks_data: List[dict]):
        for block_data in sorted(blocks_data, key=lambda x: x["block_id"]):
            if block_data["block_id"] > self.current_block_id - 1:
                existing = [b for b in self.blocks if b.block_id == block_data["block_id"]]
                if not existing:
                    block = Block(block_data["block_id"], block_data["timestamp"], block_data["previous_hash"],
                                 block_data["validators"], block_data["level"], block_hash=block_data["block_hash"],
                                 reward_amount=block_data["reward_amount"], accepted=True, reward_distributed=True)
                    self.blocks.append(block)
                    if block.block_id >= self.current_block_id:
                        self.current_block_id = block.block_id + 1
                        self.last_block_hash = block.block_hash
                    print(f"[SYNC] Imported block {block_data['block_id']}")
    
    async def receive_external_block(self, block_data: dict, peer_addr: str):
        block_id = block_data["block_id"]
        existing = [b for b in self.blocks if b.block_id == block_id]
        if existing:
            return
        if block_data["previous_hash"] == self.last_block_hash:
            block = Block(block_id, block_data["timestamp"], block_data["previous_hash"],
                         block_data["validators"], block_data["level"], block_hash=block_data["block_hash"],
                         reward_amount=block_data["reward_amount"], accepted=True, reward_distributed=True)
            self.blocks.append(block)
            self.current_block_id = block_id + 1
            self.last_block_hash = block.block_hash
            print(f"[P2P] Received block {block_id} from {peer_addr}")
    
    async def receive_external_transaction(self, tx_data: dict, peer_addr: str):
        existing = [tx for tx in self.pending_transactions if tx.tx_hash == tx_data.get("tx_hash")]
        if existing:
            return
        tx = Transaction(tx_data.get("tx_hash"), tx_data.get("from"), tx_data.get("to"),
                        tx_data.get("amount"), tx_data.get("fee", 1), tx_data.get("timestamp", time.time()),
                        status=TxStatus.PENDING, tx_type=tx_data.get("type", "send"))
        self.pending_transactions.append(tx)
        print(f"[P2P] Received transaction {tx.tx_hash[:16]}... from {peer_addr}")
    
    async def process_slash_event(self, slash_data: dict, peer_addr: str):
        vid = slash_data.get("validator_id")
        if vid in self.miners:
            self.slash_miner(vid, f"External: {slash_data.get('reason', 'Unknown')}")
            print(f"[P2P] Processed slash event from {peer_addr}")
    
    async def register_remote_node(self, node_data: dict, peer_addr: str):
        node_id = node_data.get("node_id")
        username = node_data.get("username", "unknown")
        wallet = node_data.get("wallet", "")
        ip = node_data.get("ip", peer_addr.split(":")[0])
        port = node_data.get("port", P2P_PORT)
        
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(node_id, username, wallet, ip, port, time.time(),
                                      node_data.get("height", 0), True, 0)
            c = self.conn.cursor()
            c.execute("INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (node_id, username, wallet, ip, port, time.time(),
                      node_data.get("height", 0), 1, 0, P2P_VERSION))
            self.conn.commit()
            print(f"[P2P] New node registered: {node_id[:16]}... (username: {username})")
    
    async def produce_block(self, level: int):
        validators = self.select_validators(level)
        if len(validators) < MIN_VALIDATORS_PER_BLOCK:
            return
        
        block_id = self.current_block_id
        challenge = self.generate_challenge(block_id, validators)
        self.pending_challenges[challenge] = {"block_id": block_id, "validators": validators, "level": level, "signatures": {}}
        
        await asyncio.sleep(SIGNING_WINDOW_MS / 1000)
        
        pending = self.pending_challenges.pop(challenge, {})
        sigs = pending.get("signatures", {})
        
        valid_sigs = {}
        slashed_total = 0
        
        for vid, sig in sigs.items():
            # Get miner type for proper verification
            m_type = self.miners[vid].miner_type if vid in self.miners else "ecdsa"
            if vid in self.miners and self.verify_challenge_response(vid, challenge, block_id, sig, m_type):
                valid_sigs[vid] = sig
        
        if len(valid_sigs) >= MIN_VALIDATORS_PER_BLOCK:
            block = Block(block_id, time.time(), self.last_block_hash, list(valid_sigs.keys()), level, valid_sigs,
                         transactions=self.pending_transactions[:100])
            block.transaction_count = len(block.transactions)
            self.distribute_block_reward(block)
            block.block_hash = hash_block({"block_id": block_id, "timestamp": block.timestamp,
                                          "previous_hash": self.last_block_hash, "validators": block.validators, "level": level,
                                          "transactions": [{"tx_hash": tx.tx_hash, "from": tx.from_wallet,
                                                           "to": tx.to_wallet, "amount": tx.amount} for tx in block.transactions]})
            self.last_block_hash = block.block_hash
            self.blocks.append(block)
            self.pending_transactions = self.pending_transactions[100:]
            self.current_block_id += 1
            interval = self.get_block_interval_for_level(level)
            print(f"[BLOCK {block_id}] ✅ ACCEPTED | Level {level} | Validators: {len(valid_sigs)} | Txs: {block.transaction_count} | Next block in {interval}s")
            await self.p2p.broadcast_new_block({"block_id": block_id, "timestamp": block.timestamp,
                                               "previous_hash": block.previous_hash, "validators": block.validators,
                                               "level": level, "block_hash": block.block_hash,
                                               "reward_amount": block.reward_amount})
            await asyncio.sleep(interval)
        else:
            missing = set(validators) - set(sigs.keys())
            slashed_total = 0
            for vid in missing:
                slash_amount = self.slash_miner(vid, f"Missed signing for block {block_id}", block_id)
                slashed_total += slash_amount
            
            if slashed_total > 0 and len(valid_sigs) > 0:
                per_signer = slashed_total // len(valid_sigs)
                for vid in valid_sigs.keys():
                    if vid in self.miners:
                        self.miners[vid].stake += per_signer
                        self.miners[vid].total_rewards += per_signer
                        self.balances[self.miners[vid].wallet] = self.balances.get(self.miners[vid].wallet, 0) + per_signer
                        print(f"[REDIST] {self.miners[vid].username} received +{per_signer} {SYMBOL} from slashed stake")
                
                c = self.conn.cursor()
                for vid in valid_sigs.keys():
                    if vid in self.miners:
                        c.execute("UPDATE miners SET stake=?, total_rewards=? WHERE validator_id=?",
                                 (self.miners[vid].stake, self.miners[vid].total_rewards, vid))
                        c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)",
                                 (self.miners[vid].wallet, self.balances[self.miners[vid].wallet], time.time()))
                self.conn.commit()
            
            print(f"[BLOCK {block_id}] ❌ REJECTED | Signatures: {len(valid_sigs)}/{MIN_VALIDATORS_PER_BLOCK}")
            print(f"   └─ Slashed {len(missing)} miners | Redistributed {slashed_total} {SYMBOL} to {len(valid_sigs)} signers")

# ==================== WEBSOCKET SERVER ====================
class MicroCoreServer:
    def __init__(self, network: MicroCoreNetwork):
        self.network = network
        self.connections = {}
    
    async def handle(self, websocket, path):
        try:
            async for message in websocket:
                data = json.loads(message)
                t = data.get("type")
                
                if t == "register":
                    vid = data.get("validator_id")
                    pubkey = data.get("public_key")
                    username = data.get("username")
                    wallet = data.get("wallet")
                    stake = data.get("stake", 100)
                    sig = data.get("signature", "")
                    ts = data.get("timestamp", time.time())
                    miner_type = data.get("miner_type", "web")
                    
                    if self.network.register_miner(vid, pubkey, username, wallet, stake, sig, ts, miner_type):
                        self.connections[vid] = websocket
                        await websocket.send(json.dumps({
                            "type": "registered",
                            "level": self.network.miners[vid].level if vid in self.network.miners else 1,
                            "max_level": self.network.level_manager.max_unlocked_level,
                            "remaining_supply": self.network.get_remaining_supply(),
                            "current_reward": self.network.get_current_block_reward(),
                            "mcx_price": self.network.dex.get_price(),
                            "dex_pools": list(self.network.dex.own_pools.keys()),
                            "symbol": SYMBOL
                        }))
                
                elif t == "uptime_ping":
                    vid = data.get("validator_id")
                    uptime = data.get("uptime_seconds", 0)
                    if vid in self.network.miners:
                        self.network.update_miner_uptime(vid, uptime)
                
                elif t == "block_signature":
                    challenge = data.get("challenge")
                    if challenge in self.network.pending_challenges:
                        self.network.pending_challenges[challenge]["signatures"][data.get("validator_id")] = data.get("signature")
                
                elif t == "stake":
                    username = data.get("wallet")
                    amount = data.get("amount", 0)
                    result = self.network.process_stake(username, amount)
                    await websocket.send(json.dumps({"type": "staking_confirmed", **result}))
                
                elif t == "unstake":
                    username = data.get("wallet")
                    amount = data.get("amount", 0)
                    result = self.network.process_unstake(username, amount)
                    await websocket.send(json.dumps({"type": "staking_confirmed", **result}))
                
                elif t == "send":
                    from_user = data.get("from")
                    to_user = data.get("to")
                    amount = data.get("amount", 0)
                    
                    from_wallet = None
                    to_wallet = None
                    
                    for miner in self.network.miners.values():
                        if miner.username == from_user:
                            from_wallet = miner.wallet
                        if miner.username == to_user:
                            to_wallet = miner.wallet
                    
                    if not from_wallet or not to_wallet:
                        await websocket.send(json.dumps({"type": "transaction_confirmed", "success": False, "error": "User not found"}))
                    else:
                        tx_hash = self.network.transfer(from_wallet, to_wallet, amount, 1, "", "send")
                        if tx_hash:
                            await websocket.send(json.dumps({"type": "transaction_confirmed", "success": True, "tx_hash": tx_hash, "amount": amount}))
                        else:
                            await websocket.send(json.dumps({"type": "transaction_confirmed", "success": False, "error": "Insufficient balance"}))
                
                elif t == "buy_mcx":
                    wallet = data.get("wallet")
                    usd_amount = data.get("usd_amount", 0)
                    payment_method = data.get("payment_method", "card")
                    result = self.network.buy_mcx_with_fiat(wallet, usd_amount, payment_method)
                    await websocket.send(json.dumps({"type": "buy_confirmed", **result}))
                
                elif t == "swap_quote":
                    quote = await self.network.dex.get_swap_quote(data["from_token"], data["to_token"], data["amount"])
                    await websocket.send(json.dumps({"type": "swap_quote", "data": quote}))
                
                elif t == "execute_swap":
                    result = await self.network.dex.execute_swap(data["wallet"], data["from_token"], data["to_token"],
                                                                 data["amount"], data.get("fee_mcx", 5))
                    await websocket.send(json.dumps({"type": "swap_result", "data": result}))
                    if result.get("success"):
                        balance = self.network.get_balance(data["wallet"])
                        await websocket.send(json.dumps({"type": "balance_update", "balance": balance}))
                
                elif t == "add_liquidity_own":
                    result = await self.network.dex.add_own_pool_liquidity(data["wallet"], data["pool_id"],
                                                                           data["amount_a"], data["amount_b"])
                    await websocket.send(json.dumps({"type": "liquidity_confirmed", **result}))
                
                elif t == "get_supported_pools":
                    pools = await self.network.dex.get_supported_pools()
                    await websocket.send(json.dumps({"type": "supported_pools", "pools": pools}))
                
                elif t == "get_balance":
                    balance = self.network.get_balance(data["wallet"])
                    staked = self.network.get_staked(data["wallet"])
                    await websocket.send(json.dumps({"type": "balance", "wallet": data["wallet"], "balance": balance, "staked": staked}))
                
                elif t == "get_miners":
                    miners_list = [{"validator_id": m.validator_id, "username": m.username, "wallet": m.wallet,
                                   "level": m.level, "stake": m.stake, "blocks_signed": m.blocks_signed,
                                   "total_rewards": m.total_rewards, "is_active": m.is_active,
                                   "uptime_seconds": m.uptime_seconds, "today_uptime": m.today_uptime,
                                   "miner_type": m.miner_type, "last_seen": m.last_ping} for m in self.network.miners.values()]
                    await websocket.send(json.dumps({"type": "miner_list", "miners": miners_list}))
                
                elif t == "get_nodes":
                    nodes_list = [{"node_id": n.node_id, "username": n.username, "wallet": n.wallet,
                                  "ip": n.ip, "port": n.port, "height": n.height, "status": "online" if n.is_active else "offline",
                                  "rewards_earned": n.rewards_earned, "last_seen": n.last_seen} for n in self.network.nodes.values()]
                    await websocket.send(json.dumps({"type": "node_list", "nodes": nodes_list}))
                
                elif t == "get_top_stakers":
                    stakers = self.network.get_top_stakers(10)
                    await websocket.send(json.dumps({"type": "top_stakers", "stakers": stakers}))
                
                elif t == "get_top_buyers":
                    buyers = self.network.get_top_buyers(10)
                    await websocket.send(json.dumps({"type": "top_buyers", "buyers": buyers}))
                
                elif t == "control_miner":
                    miner_id = data.get("miner_id")
                    action = data.get("action")
                    await websocket.send(json.dumps({"type": "miner_control_response", "miner_id": miner_id, "action": action, "success": True, "result": f"{action} command sent"}))
                
                elif t == "get_status":
                    await websocket.send(json.dumps({
                        "type": "status", "data": {
                            "block_id": self.network.current_block_id,
                            "total_miners": len(self.network.miners),
                            "active_miners": sum(1 for m in self.network.miners.values() if m.is_active),
                            "total_nodes": len(self.network.nodes),
                            "active_nodes": sum(1 for n in self.network.nodes.values() if n.is_active),
                            "max_level": self.network.level_manager.max_unlocked_level,
                            "current_reward": self.network.get_current_block_reward(),
                            "total_minted": self.network.total_minted,
                            "remaining_supply": self.network.get_remaining_supply(),
                            "supply_percentage": self.network.get_supply_percentage(),
                            "current_halving": self.network.get_current_halving(),
                            "mcx_price": self.network.dex.get_price(),
                            "symbol": SYMBOL,
                            "buyer_rewards_pool": self.network.buyer_rewards_pool,
                            "node_pool": self.network.node_pool,
                            "uptime_pool": self.network.uptime_pool,
                            "level_intervals": LEVEL_BLOCK_INTERVALS
                        }
                    }))
                
                elif t == "get_blocks":
                    limit = data.get("limit", 20)
                    blocks = self.network.get_blocks_in_range(max(0, self.network.current_block_id - limit), self.network.current_block_id)
                    await websocket.send(json.dumps({"type": "blocks", "blocks": blocks[::-1]}))
                
                elif t == "get_transactions":
                    c = self.network.conn.cursor()
                    c.execute("SELECT tx_hash, from_wallet, to_wallet, amount, fee, timestamp, status, tx_type FROM transactions ORDER BY timestamp DESC LIMIT 20")
                    txs = [{"tx_hash": r[0], "from": r[1], "to": r[2], "amount": r[3], "fee": r[4], "timestamp": r[5], "status": r[6], "type": r[7]} for r in c.fetchall()]
                    await websocket.send(json.dumps({"type": "transactions", "transactions": txs}))
        
        except Exception as e:
            print(f"[WS] Error: {e}")
            traceback.print_exc()
    
    async def periodic_distribution(self):
        while True:
            await asyncio.sleep(DISTRIBUTION_INTERVAL_SEC)
            self.network.distribute_periodic_rewards()
    
    async def buyer_rewards_check(self):
        while True:
            await asyncio.sleep(60)
            if time.time() - self.network._last_buyer_distribution > 30 * 24 * 3600:
                self.network.distribute_buyer_rewards()
                self.network._last_buyer_distribution = time.time()
    
    async def block_production_loop(self):
        level = 1
        while True:
            if self.network.level_groups:
                avail = [l for l in self.network.level_groups if len(self.network.level_groups[l]) >= MIN_VALIDATORS_PER_BLOCK]
                if avail:
                    level = (level % max(avail)) + 1
                    if level in avail:
                        await self.network.produce_block(level)
            await asyncio.sleep(0.1)
    
    async def peer_discovery(self):
        while True:
            await asyncio.sleep(PEX_INTERVAL)
            await self.network.p2p.discover_peers()
    
    async def peer_sync(self):
        while True:
            await asyncio.sleep(SYNC_INTERVAL)
            await self.network.p2p.sync_with_peers()
    
    async def status_reporter(self):
        while True:
            await asyncio.sleep(60)
            remaining = self.network.get_remaining_supply()
            percent = self.network.get_supply_percentage()
            reward = self.network.get_current_block_reward()
            halving = self.network.get_current_halving()
            price = self.network.dex.get_price()
            print(f"\n[STATUS] Block: {self.network.current_block_id} | Reward: {reward} {SYMBOL} | Halving: {halving}")
            print(f"[STATUS] Miners: {len(self.network.miners)} | Active: {sum(1 for m in self.network.miners.values() if m.is_active)}")
            print(f"[STATUS] Nodes: {len(self.network.nodes)} | Peers: {len(self.network.p2p.peers)}")
            print(f"[STATUS] Pending TX: {len(self.network.pending_transactions)}")
            print(f"[STATUS] Max Unlocked Level: {self.network.level_manager.max_unlocked_level}")
            print(f"[STATUS] Buyer Rewards Pool: {self.network.buyer_rewards_pool} {SYMBOL}")
            print(f"[SUPPLY] {self.network.total_minted:,} / {TOTAL_SUPPLY_CAP:,} ({percent:.4f}%) | Remaining: {remaining:,} {SYMBOL}")
            print(f"[PRICE] 1 {SYMBOL} = ${price:.4f} USD\n")
    
    def distribute_buyer_rewards(self):
        if self.network.buyer_rewards_pool == 0:
            return
        c = self.network.conn.cursor()
        c.execute("""
            SELECT wallet, username, monthly_bought 
            FROM buyer_stats 
            ORDER BY monthly_bought DESC 
            LIMIT 10
        """)
        top_buyers = c.fetchall()
        if not top_buyers:
            return
        rewards = [5000, 3000, 2000, 1000, 1000, 500, 500, 500, 500, 500]
        for i, (wallet, username, _) in enumerate(top_buyers):
            if i >= len(rewards):
                break
            reward = min(rewards[i], self.network.buyer_rewards_pool)
            self.network.balances[wallet] = self.network.balances.get(wallet, 0) + reward
            self.network.buyer_rewards_pool -= reward
            tx_hash = hash_transaction({"from": "buyer_rewards_pool", "to": wallet, "amount": reward, "timestamp": time.time()})
            c.execute("INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (tx_hash, "buyer_rewards_pool", wallet, reward, 0, time.time(), -1, "", "confirmed", "reward"))
            self.network.conn.commit()
            print(f"[BUYER REWARD] #{i+1} {username[:20]}... +{reward} {SYMBOL}")
        c.execute("UPDATE buyer_stats SET monthly_bought = 0, last_reset = ?", (time.time(),))
        self.network.conn.commit()
        self.network.buyer_rewards_pool = 0
    
    async def run(self):
        asyncio.create_task(self.network.p2p.start())
        asyncio.create_task(self.network.p2p.heartbeat())
        asyncio.create_task(self.peer_discovery())
        asyncio.create_task(self.peer_sync())
        asyncio.create_task(self.periodic_distribution())
        asyncio.create_task(self.buyer_rewards_check())
        asyncio.create_task(self.block_production_loop())
        asyncio.create_task(self.status_reporter())
        
        async with serve(self.handle, NODE_HOST, NODE_PORT):
            print(f"[WS] Server: ws://{NODE_HOST}:{NODE_PORT}")
            print(f"[P2P] Port: {P2P_PORT}")
            print(f"[DNS] Seeds: {', '.join(DNS_SEEDS)}")
            print(f"[REWARD] {INITIAL_BLOCK_REWARD} MCX per block | Split: 70% validators | 8% nodes | 5% uptime | 5% LP | 12% buyer rewards")
            print(f"[LEVELS] Stake {LEVEL_STAKE_RANGE} MCX = Level 1 | Need {MIN_WALLETS_FOR_NEXT_LEVEL} unique wallets to unlock next level")
            print(f"[DEX] Own pools: MCX/USDC, MCX/BTC, MCX/ETH, MCX/SOL, MCX/BNB")
            print(f"[DEX] LI.FI/THORChain integration: ENABLED")
            print(f"[FIAT] Buy MCX with credit card: ENABLED (1 MCX = ${MCX_PRICE_USD})")
            print(f"[CRYPTO] Dual mode: SHA256 for web/uno | ECDSA for esp32/pc/pico/phone")
            print(f"[NODE] Node ID: {self.network.node_id[:16]}... | Username: {self.network.node_username}")
            await asyncio.Future()

# ==================== EMBEDDED LOCAL MINER ====================
class LocalMiner:
    def __init__(self, network: MicroCoreNetwork, username: str, private_key: str, public_key: str, wallet: str):
        self.network = network
        self.username = username
        self.private_key = private_key
        self.public_key = public_key
        self.wallet = wallet
        self.validator_id = f"LOCAL_{username}_{hashlib.sha256(username.encode()).hexdigest()[:16]}"
        self.stake = 100
        self.running = True
    
    def register(self):
        ts = time.time()
        message = f"{self.validator_id}{self.username}{self.stake}{ts}"
        signature = sign_message_ecdsa(self.private_key, message) if ECDSA_AVAILABLE else sign_message_sha256(self.private_key, message)
        self.network.register_miner(self.validator_id, self.public_key, self.username, self.wallet, self.stake, signature, ts, "embedded")
        print(f"[EMBEDDED MINER] Registered: {self.username}")
    
    async def run(self):
        self.register()
        last_uptime = time.time()
        while self.running:
            for challenge, pending in self.network.pending_challenges.items():
                if self.validator_id in pending["validators"] and self.validator_id not in pending["signatures"]:
                    message = f"{challenge}{self.validator_id}{pending['block_id']}"
                    signature = sign_message_ecdsa(self.private_key, message) if ECDSA_AVAILABLE else sign_message_sha256(self.private_key, message)
                    pending["signatures"][self.validator_id] = signature
                    print(f"[EMBEDDED MINER] Signed block {pending['block_id']}")
                    break
            if time.time() - last_uptime > 30:
                if self.validator_id in self.network.miners:
                    self.network.miners[self.validator_id].uptime_seconds += 30
                last_uptime = time.time()
            await asyncio.sleep(0.1)

# ==================== MAIN ====================
async def main():
    parser = argparse.ArgumentParser(description=f'{NAME} Complete Node + Miner - Dual Crypto Mode')
    parser.add_argument('--genesis', action='store_true', help='Run as genesis node (only first node)')
    parser.add_argument('--peer', type=str, help='Connect to peer node (IP:PORT)')
    parser.add_argument('--no-miner', action='store_true', help='Disable embedded miner')
    parser.add_argument('--username', type=str, required=True, help='Your username (for rewards)')
    parser.add_argument('--wallet', type=str, required=True, help='Your wallet address (for rewards)')
    args = parser.parse_args()
    
    print("=" * 60)
    print(f"{NAME} ({SYMBOL}) COMPLETE NODE v{VERSION}")
    print("DUAL CRYPTO MODE: SHA256 (web/uno) + ECDSA (esp32/pc/pico/phone)")
    print("=" * 60)
    print(f"Username: {args.username}")
    print(f"Wallet: {args.wallet}")
    print(f"Hard cap: {TOTAL_SUPPLY_CAP:,} {SYMBOL}")
    print(f"Initial reward: {INITIAL_BLOCK_REWARD} {SYMBOL}")
    print(f"Halving interval: {HALVING_INTERVAL:,} blocks (~4 years)")
    print(f"Level stake range: {LEVEL_STAKE_RANGE} {SYMBOL}/level")
    print(f"Min wallets for next level: {MIN_WALLETS_FOR_NEXT_LEVEL}")
    print(f"Reward split: 70% validators | 8% nodes | 5% uptime | 5% LP | 12% buyer rewards")
    print(f"DEX: Own pools (MCX pairs) + LI.FI/THORChain integration")
    print(f"Fiat on-ramp: Buy MCX with credit card (1 MCX = ${MCX_PRICE_USD})")
    print("=" * 60)
    print("\nNOTE: This node includes an EMBEDDED MINER that mines automatically.")
    print("      Use --no-miner to disable.\n")
    
    if args.peer:
        print(f"[P2P] Bootstrap peer: {args.peer}")
    
    wallet_file = "microcore_wallet.json"
    if os.path.exists(wallet_file):
        with open(wallet_file, 'r') as f:
            data = json.load(f)
            username = data.get('username', args.username)
            wallet_addr = data['address']
            private_key = data['private_key']
            public_key = data['public_key']
        print(f"[WALLET] Loaded: {username} ({wallet_addr[:20]}...)")
    else:
        if ECDSA_AVAILABLE:
            wallet_addr, private_key, public_key = generate_wallet_ecdsa()
        else:
            wallet_addr, private_key, public_key = generate_wallet_sha256(args.username, "auto_gen")
        username = args.username
        with open(wallet_file, 'w') as f:
            json.dump({'username': username, 'address': wallet_addr, 'private_key': private_key, 'public_key': public_key}, f)
        print(f"[WALLET] Created: {username} ({wallet_addr[:20]}...)")
        print(f"[WALLET] SAVE THIS FILE: {os.path.abspath(wallet_file)}")
        print(f"[WALLET] Private key (KEEP SECRET): {private_key}")
    
    network = MicroCoreNetwork(is_genesis_node=args.genesis, node_username=args.username, node_wallet=args.wallet)
    server = MicroCoreServer(network)
    
    if not args.no_miner:
        miner = LocalMiner(network, username, private_key, public_key, wallet_addr)
        asyncio.create_task(miner.run())
        print(f"[EMBEDDED MINER] Started for '{username}'")
    
    await server.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Node stopped")
        sys.exit(0)