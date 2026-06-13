#!/usr/bin/env python3
"""
MICROCORE (MCX) PHONE NODE v1.0
Runs on iPhone (a-shell) or Android (Termux)
Full node with embedded miner, P2P, DEX, WebSocket server

Run: python3 phone_node.py --genesis
     python3 phone_node.py --peer IP:8081

Requirements (auto-installed):
  pip install websockets cryptography dnspython requests
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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from enum import Enum

# ==================== DEPENDENCY CHECK ====================
try:
    import websockets
    from websockets.server import serve
except ImportError:
    os.system("pip install websockets")
    import websockets
    from websockets.server import serve

try:
    import requests
except ImportError:
    os.system("pip install requests")
    import requests

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature, decode_dss_signature
except ImportError:
    os.system("pip install cryptography")
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature, decode_dss_signature

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    os.system("pip install dnspython")
    import dns.resolver
    DNS_AVAILABLE = True

# ==================== CONFIGURATION ====================
NODE_HOST = "0.0.0.0"
NODE_PORT = 8080
P2P_PORT = 8081

SYMBOL = "MCX"
NAME = "MicroCore"
VERSION = "PHONE-1.0"

DNS_SEEDS = ["seed.microcore.com", "seed1.microcore.com", "seed2.microcore.com"]

TOTAL_SUPPLY_CAP = 84_000_000
INITIAL_BLOCK_REWARD = 10
HALVING_INTERVAL = 4_204_800

VALIDATOR_SHARE = 0.70
NODE_SHARE = 0.08
UPTIME_SHARE = 0.05
LP_SHARE = 0.05
BUYER_REWARDS_SHARE = 0.12

LEVEL_STAKE_RANGE = 100
MAX_LEVEL = 100
MIN_WALLETS_FOR_NEXT_LEVEL = 10

SIGNING_WINDOW_MS = 2500
SLASH_RATE = 0.10
MIN_VALIDATORS_PER_BLOCK = 10
UPTIME_PING_INTERVAL = 30
DISTRIBUTION_INTERVAL_SEC = 300

MAX_PEERS = 30
SYNC_INTERVAL = 10
HEARTBEAT_INTERVAL = 30
PEX_INTERVAL = 60

BAN_THRESHOLD = 5
BAN_DURATION = 3600

SWAP_FEE_RATE = 0.003
MCX_FEE_MIN = 1
MCX_FEE_MAX = 100
MCX_PRICE_USD = 0.01

OWN_POOLS = ["MCX/USDC", "MCX/BTC", "MCX/ETH", "MCX/SOL", "MCX/BNB"]

# ==================== ENUMS ====================
class TxStatus(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"

class PeerState(Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    BANNED = "banned"

# ==================== CRYPTOGRAPHY ====================
def verify_signature(public_key_pem: str, message: str, signature_hex: str, miner_type: str = "ecdsa") -> bool:
    if miner_type in ["web", "uno"]:
        expected = hashlib.sha256(f"{public_key_pem}{message}".encode()).hexdigest()
        return signature_hex == expected
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

def sign_message(private_key_hex: str, message: str) -> str:
    private_value = int(private_key_hex, 16)
    private_key = ec.derive_private_key(private_value, ec.SECP256K1())
    signature = private_key.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature)
    return r.to_bytes(32, 'big').hex() + s.to_bytes(32, 'big').hex()

def generate_wallet() -> tuple:
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
P2P_MSG_NEW_BLOCK = 0x08
P2P_MSG_NEW_TRANSACTION = 0x09
P2P_MSG_GET_PEERS = 0x0A
P2P_MSG_PEERS = 0x0B
P2P_MSG_SLASH_EVENT = 0x0F
P2P_MSG_NODE_REGISTER = 0x11

def encode_p2p_message(msg_type: int, payload: dict) -> bytes:
    payload_bytes = json.dumps(payload).encode()
    header = P2P_MAGIC + struct.pack(">B", P2P_VERSION) + struct.pack(">B", msg_type) + struct.pack(">I", len(payload_bytes))
    return header + payload_bytes

def decode_p2p_message(data: bytes) -> tuple:
    if len(data) < 10 or data[:4] != P2P_MAGIC:
        return None, None
    msg_type = data[5]
    payload_len = struct.unpack(">I", data[6:10])[0]
    if len(data) < 10 + payload_len:
        return None, None
    payload = json.loads(data[10:10+payload_len].decode())
    return msg_type, payload

# ==================== DNS SEED DISCOVERY ====================
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
            print(f"[DNS] Failed to resolve {seed}: {e}")
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

# ==================== DEX ====================
class DEXBridge:
    def __init__(self, network=None):
        self.network = network
        self.mcx_price_usd = MCX_PRICE_USD
        self.own_pools = {
            "MCX/USDC": {"token_a": "MCX", "token_b": "USDC", "reserve_a": 100000, "reserve_b": 100000, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0},
            "MCX/BTC": {"token_a": "MCX", "token_b": "BTC", "reserve_a": 100000, "reserve_b": 1.67, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0},
            "MCX/ETH": {"token_a": "MCX", "token_b": "ETH", "reserve_a": 100000, "reserve_b": 33.33, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0},
            "MCX/SOL": {"token_a": "MCX", "token_b": "SOL", "reserve_a": 100000, "reserve_b": 666.67, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0},
            "MCX/BNB": {"token_a": "MCX", "token_b": "BNB", "reserve_a": 100000, "reserve_b": 333.33, "fee": SWAP_FEE_RATE, "lp_providers": {}, "total_lp_shares": 0}
        }
    
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
            return {"success": True, "pool_type": "own", "expected_output": output, "fee_mcx": fee_mcx}
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
            return {"success": True, "pool_type": "own", "expected_output": output, "fee_mcx": fee_mcx}
    
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
        pool = self.own_pools[quote["pool_id"]] if "pool_id" in quote else self.own_pools.get(f"MCX/{to_token}", self.own_pools.get(f"MCX/{from_token}"))
        if pool:
            if from_token == "MCX":
                pool["reserve_a"] += amount
                pool["reserve_b"] -= quote["expected_output"]
            else:
                pool["reserve_b"] += amount
                pool["reserve_a"] -= quote["expected_output"]
            pool["reserve_a"] = max(pool["reserve_a"], 0)
            pool["reserve_b"] = max(pool["reserve_b"], 0)
        tx_hash = hashlib.sha256(f"{user_wallet}{from_token}{to_token}{amount}{time.time()}".encode()).hexdigest()[:16]
        return {"success": True, "tx_hash": tx_hash, "amount_out": quote["expected_output"], "fee_mcx": fee_mcx}
    
    def add_own_pool_liquidity(self, user_wallet: str, pool_id: str, amount_a: float, amount_b: float) -> dict:
        if pool_id not in self.own_pools:
            return {"success": False, "error": f"Pool {pool_id} not found"}
        if self.network and self.network.get_balance(user_wallet) < amount_a + amount_b:
            return {"success": False, "error": "Insufficient balance"}
        if self.network:
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
        return {"success": True, "lp_shares": lp_shares}
    
    async def get_swap_quote(self, from_token: str, to_token: str, amount: float) -> dict:
        if from_token == "MCX" or to_token == "MCX":
            return self.get_own_pool_quote(from_token, to_token, amount)
        prices = {"BTC": 60000, "ETH": 3000, "SOL": 150, "USDC": 1, "USDT": 1, "BNB": 300}
        from_price = prices.get(from_token, 1)
        to_price = prices.get(to_token, 1)
        value_usd = amount * from_price
        expected_output = (value_usd / to_price) * 0.997
        fee_mcx = self.calculate_swap_fee_mcx(value_usd)
        return {"success": True, "pool_type": "lifi", "expected_output": expected_output, "fee_mcx": fee_mcx}
    
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
        return {"success": True, "tx_hash": tx_hash, "amount_out": quote.get("expected_output", 0), "fee_mcx": fee_mcx}
    
    def buy_mcx_with_fiat(self, user_wallet: str, usd_amount: float, payment_method: str = "card") -> dict:
        mcx_amount = int(usd_amount / self.mcx_price_usd)
        if self.network:
            self.network.balances[user_wallet] = self.network.balances.get(user_wallet, 0) + mcx_amount
            self.network.total_minted += mcx_amount
            tx_hash = hashlib.sha256(f"fiat_buy_{user_wallet}{usd_amount}{time.time()}".encode()).hexdigest()[:16]
            return {"success": True, "tx_hash": tx_hash, "mcx_amount": mcx_amount}
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

@dataclass
class Peer:
    address: str
    last_seen: float
    height: int
    version: int = P2P_VERSION
    is_outbound: bool = False

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

# ==================== LEVEL MANAGER ====================
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
        return self.level_unique_wallets.get(level, 0) >= MIN_WALLETS_FOR_NEXT_LEVEL
    
    def process_level_unlock(self):
        for level in range(1, self.max_unlocked_level + 2):
            if level > self.max_unlocked_level and self.can_unlock_next_level(level - 1):
                self.max_unlocked_level = level
                print(f"[LEVEL] Level {level} UNLOCKED!")
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
    
    async def start(self):
        self.server = await asyncio.start_server(self.handle_connection, NODE_HOST, P2P_PORT)
        print(f"[P2P] Server on port {P2P_PORT}")
        if self.public_ip:
            print(f"[P2P] Public IP: {self.public_ip}:{P2P_PORT}")
    
    async def handle_connection(self, reader, writer):
        peer_addr = writer.get_extra_info('peername')
        addr_str = f"{peer_addr[0]}:{peer_addr[1]}"
        try:
            length_data = await reader.read(4)
            if not length_data:
                writer.close()
                return
            msg_len = struct.unpack(">I", length_data)[0]
            if msg_len > 10_000_000:
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
    
    async def process_message(self, msg_type, payload, writer, peer_addr):
        if msg_type == P2P_MSG_HANDSHAKE:
            response = {"node_id": self.network.node_id, "version": P2P_VERSION,
                       "height": self.network.current_block_id, "public_ip": self.public_ip,
                       "username": self.network.node_username, "wallet": self.network.node_wallet}
            data = encode_p2p_message(P2P_MSG_HANDSHAKE, response)
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
            self.peers[peer_addr] = Peer(peer_addr, time.time(), payload.get("height", 0))
            print(f"[P2P] New peer: {peer_addr}")
            await self.send_peers(writer)
            if payload.get("height", 0) > self.network.current_block_id:
                asyncio.create_task(self.request_blocks(peer_addr, self.network.current_block_id, payload.get("height", 0)))
        elif msg_type == P2P_MSG_GET_PEERS:
            await self.send_peers(writer)
        elif msg_type == P2P_MSG_PEERS:
            for p in payload.get("peers", []):
                if p not in self.peers and p != f"{self.public_ip}:{P2P_PORT}":
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
        elif msg_type == P2P_MSG_SLASH_EVENT:
            await self.network.process_slash_event(payload.get("slash"), peer_addr)
        elif msg_type == P2P_MSG_PING:
            data = encode_p2p_message(P2P_MSG_PONG, {"timestamp": time.time()})
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
        elif msg_type == P2P_MSG_PONG:
            if peer_addr in self.peers:
                self.peers[peer_addr].last_seen = time.time()
    
    async def send_peers(self, writer):
        peers_list = list(self.peers.keys())[:100]
        data = encode_p2p_message(P2P_MSG_PEERS, {"peers": peers_list})
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
        if peer_addr in self.peers:
            return
        try:
            host, port = peer_addr.split(":")
            reader, writer = await asyncio.open_connection(host, int(port))
            handshake = {"node_id": self.network.node_id, "version": P2P_VERSION,
                        "height": self.network.current_block_id, "public_ip": self.public_ip,
                        "username": self.network.node_username, "wallet": self.network.node_wallet}
            data = encode_p2p_message(P2P_MSG_HANDSHAKE, handshake)
            writer.write(struct.pack(">I", len(data)) + data)
            await writer.drain()
            self.peers[peer_addr] = Peer(peer_addr, time.time(), self.network.current_block_id, is_outbound=True)
            print(f"[P2P] Connected to {peer_addr}")
            writer.close()
        except Exception as e:
            print(f"[P2P] Failed to connect to {peer_addr}: {e}")
    
    async def broadcast_new_block(self, block: dict):
        data = encode_p2p_message(P2P_MSG_NEW_BLOCK, {"block": block})
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
            if peer not in self.peers:
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
    def __init__(self, is_genesis_node: bool = False, node_username: str = "", node_wallet: str = "", node_priv: str = "", node_pub: str = ""):
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
        self.node_id = hashlib.sha256(f"{node_username}{time.time()}".encode()).hexdigest()[:16]
        self.node_username = node_username
        self.node_wallet = node_wallet
        self.node_priv = node_priv
        self.node_pub = node_pub
        self._last_buyer_distribution = time.time()
        
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
        
        if node_username and node_wallet:
            self.register_this_node()
        self.register_self_miner()
    
    def init_database(self):
        self.conn = sqlite3.connect('microcore_phone.db', check_same_thread=False)
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS miners
                     (validator_id TEXT PRIMARY KEY, public_key TEXT, username TEXT,
                      wallet TEXT, stake INTEGER, level INTEGER, total_rewards INTEGER,
                      blocks_signed INTEGER, slash_count INTEGER, uptime_seconds INTEGER,
                      today_uptime INTEGER, registered_at REAL, last_ping REAL, 
                      miner_type TEXT, last_uptime_reset REAL)''')
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
        self.conn.commit()
    
    def register_self_miner(self):
        if self.node_username and self.node_pub:
            self.miners[self.node_username] = Miner(
                self.node_username, self.node_pub, self.node_username, self.node_wallet,
                1000, 1, 0, 0, time.time(), True, 0, 0, 0, 0, time.time(), "embedded"
            )
            self.level_manager.register_miner_stake(self.node_wallet, 1000)
            c = self.conn.cursor()
            c.execute("INSERT OR REPLACE INTO miners VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (self.node_username, self.node_pub, self.node_username, self.node_wallet,
                      1000, 1, 0, 0, 0, 0, 0, time.time(), time.time(), "embedded", 0))
            self.conn.commit()
            print(f"[EMBEDDED] Self miner '{self.node_username}' active")
    
    def register_this_node(self):
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (self.node_id, self.node_username, self.node_wallet,
                  self.p2p.public_ip or "unknown", P2P_PORT, time.time(),
                  self.current_block_id, 1, 0, P2P_VERSION))
        self.conn.commit()
        print(f"[NODE] Registered: {self.node_id[:16]}...")
    
    def load_nodes(self):
        c = self.conn.cursor()
        c.execute("SELECT node_id, username, wallet, ip, port, last_seen, height, is_active, rewards_earned FROM nodes")
        for row in c.fetchall():
            self.nodes[row[0]] = Node(row[0], row[1], row[2], row[3], row[4], row[5], row[6], bool(row[7]), row[8])
    
    def check_existing_blockchain(self):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM blocks")
        if c.fetchone()[0] == 0:
            print("[SYNC] No blockchain found. Waiting to sync...")
        else:
            print(f"[SYNC] Loading existing blockchain...")
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
        print("\n" + "=" * 60)
        print(f"{NAME} ({SYMBOL}) GENESIS NODE ON PHONE")
        print("=" * 60)
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
            self.balances["MCR_PHONE_GENESIS"] = 100_000
        self.total_minted = 100000
        for wallet, balance in self.balances.items():
            c.execute("INSERT OR REPLACE INTO balances VALUES (?, ?, ?)", (wallet, balance, time.time()))
        c.execute("INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (0, genesis.timestamp, genesis.previous_hash, ','.join(genesis.validators),
                  genesis.level, genesis.block_hash, 0, 0))
        self.conn.commit()
        self.level_manager.max_unlocked_level = 1
        self.level_manager.update_unique_wallets()
        print(f"[GENESIS] Created with 100,000 {SYMBOL}")
    
    def get_current_block_reward(self) -> int:
        remaining = TOTAL_SUPPLY_CAP - self.total_minted
        if remaining <= 0:
            return 1
        halvings = self.current_block_id // HALVING_INTERVAL
        reward = INITIAL_BLOCK_REWARD // (2 ** halvings)
        return max(reward, 1)
    
    def get_balance(self, wallet: str) -> int:
        return self.balances.get(wallet, 0)
    
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
        return hashlib.sha256(f"{block_id}{''.join(sorted(validators))}{time.time()}{self.last_block_hash}".encode()).hexdigest()
    
    def verify_challenge_response(self, vid: str, challenge: str, block_id: int, sig: str) -> bool:
        if vid not in self.miners:
            return False
        message = f"{challenge}{vid}{block_id}"
        return verify_signature(self.miners[vid].public_key, message, sig, self.miners[vid].miner_type)
    
    def register_miner(self, vid: str, pubkey: str, username: str, wallet: str, stake: int, sig: str, ts: float, miner_type: str = "unknown") -> bool:
        message = f"{vid}{username}{stake}{ts}"
        if not verify_signature(pubkey, message, sig, miner_type):
            print(f"[REG] Signature failed for {username}")
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
        else:
            self.miners[vid] = Miner(vid, pubkey, username, wallet, stake, effective_level, registered_at=ts, miner_type=miner_type)
            print(f"[REG] New miner: {username} | Type: {miner_type} | Level {effective_level}")
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO miners VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (vid, pubkey, username, wallet, stake, effective_level,
                  self.miners[vid].total_rewards, self.miners[vid].blocks_signed,
                  self.miners[vid].slash_count, self.miners[vid].uptime_seconds,
                  self.miners[vid].today_uptime, ts, time.time(), miner_type, 0))
        self.conn.commit()
        self.update_level_groups()
        return True
    
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
            print(f"[BAN] {m.username} banned")
        c = self.conn.cursor()
        c.execute("UPDATE miners SET stake=?, level=?, slash_count=?, is_active=? WHERE validator_id=?",
                 (m.stake, m.level, m.slash_count, m.is_active, vid))
        self.conn.commit()
        self.update_level_groups()
        print(f"[SLASH] {m.username}: -{slash} {SYMBOL}")
        return slash
    
    def distribute_block_reward(self, block: Block, signers: List[str]):
        if block.reward_distributed:
            return
        reward = self.get_current_block_reward()
        block.reward_amount = reward
        validator_total = (reward * 70) // 100
        node_total = (reward * 8) // 100
        uptime_total = (reward * 5) // 100
        lp_total = (reward * 5) // 100
        buyer_total = (reward * 12) // 100
        validator_each = validator_total // max(len(signers), 1)
        for vid in signers:
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
        remaining = TOTAL_SUPPLY_CAP - self.total_minted
        print(f"[BLOCK {block.block_id}] REWARD: {reward} {SYMBOL} to {len(signers)} validators")
        print(f"[SUPPLY] {self.total_minted:,} / {TOTAL_SUPPLY_CAP:,} | Remaining: {remaining:,}")
    
    def get_blocks_in_range(self, start: int, end: int) -> List[dict]:
        blocks = []
        for b in self.blocks:
            if start <= b.block_id <= end:
                blocks.append({"block_id": b.block_id, "timestamp": b.timestamp, "previous_hash": b.previous_hash,
                              "validators": b.validators, "level": b.level, "block_hash": b.block_hash,
                              "reward_amount": b.reward_amount})
        return blocks
    
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
            print(f"[P2P] Received block {block_id}")
    
    async def receive_external_transaction(self, tx_data: dict, peer_addr: str):
        print(f"[P2P] Received transaction from {peer_addr}")
    
    async def process_slash_event(self, slash_data: dict, peer_addr: str):
        vid = slash_data.get("validator_id")
        if vid in self.miners:
            self.slash_miner(vid, f"External: {slash_data.get('reason', 'Unknown')}")
    
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
            if vid in self.miners and self.verify_challenge_response(vid, challenge, block_id, sig):
                valid_sigs[vid] = sig
        if len(valid_sigs) >= MIN_VALIDATORS_PER_BLOCK:
            block = Block(block_id, time.time(), self.last_block_hash, list(valid_sigs.keys()), level, valid_sigs)
            block.block_hash = hash_block({"block_id": block_id, "timestamp": block.timestamp,
                                          "previous_hash": self.last_block_hash, "validators": block.validators, "level": level})
            self.last_block_hash = block.block_hash
            self.blocks.append(block)
            self.distribute_block_reward(block, list(valid_sigs.keys()))
            self.current_block_id += 1
            print(f"[BLOCK {block_id}] ✅ ACCEPTED | Level {level} | Validators: {len(valid_sigs)}")
            await self.p2p.broadcast_new_block({"block_id": block_id, "timestamp": block.timestamp,
                                               "previous_hash": block.previous_hash, "validators": block.validators,
                                               "level": level, "block_hash": block.block_hash,
                                               "reward_amount": block.reward_amount})
            await asyncio.sleep(30)
        else:
            missing = set(validators) - set(sigs.keys())
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
                        print(f"[REDIST] {self.miners[vid].username} +{per_signer} {SYMBOL}")
            print(f"[BLOCK {block_id}] ❌ REJECTED | Signatures: {len(valid_sigs)}/{MIN_VALIDATORS_PER_BLOCK}")

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
                    ok = self.network.register_miner(
                        data["validator_id"], data["public_key"], data["username"],
                        data["wallet"], data["stake"], data["signature"],
                        data["timestamp"], data.get("miner_type", "web")
                    )
                    if ok:
                        await websocket.send(json.dumps({
                            "type": "registered",
                            "level": self.network.level_manager.get_effective_level(data["wallet"]),
                            "remaining_supply": TOTAL_SUPPLY_CAP - self.network.total_minted,
                            "current_reward": self.network.get_current_block_reward(),
                            "dex_pools": OWN_POOLS
                        }))
                elif t == "block_signature":
                    ch = data["challenge"]
                    if ch in self.network.pending_challenges:
                        self.network.pending_challenges[ch]["signatures"][data["validator_id"]] = data["signature"]
                elif t == "stake":
                    username = data.get("wallet")
                    amount = data.get("amount", 0)
                    for miner in self.network.miners.values():
                        if miner.username == username and self.network.get_balance(miner.wallet) >= amount:
                            self.network.balances[miner.wallet] -= amount
                            miner.stake += amount
                            self.network.level_manager.register_miner_stake(miner.wallet, miner.stake)
                            miner.level = self.network.level_manager.get_effective_level(miner.wallet)
                            await websocket.send(json.dumps({"type": "staking_confirmed", "staked": miner.stake}))
                            break
                elif t == "swap_quote":
                    quote = await self.network.dex.get_swap_quote(data["from_token"], data["to_token"], data["amount"])
                    await websocket.send(json.dumps({"type": "swap_quote", "data": quote}))
                elif t == "execute_swap":
                    result = await self.network.dex.execute_swap(data["wallet"], data["from_token"], data["to_token"],
                                                                 data["amount"], data.get("fee_mcx", 5))
                    await websocket.send(json.dumps({"type": "swap_result", "data": result}))
                elif t == "get_balance":
                    await websocket.send(json.dumps({"type": "balance", "balance": self.network.get_balance(data["wallet"])}))
                elif t == "get_miners":
                    miners_list = [{"username": m.username, "level": m.level, "stake": m.stake, "blocks_signed": m.blocks_signed} for m in self.network.miners.values()]
                    await websocket.send(json.dumps({"type": "miner_list", "miners": miners_list}))
                elif t == "get_status":
                    await websocket.send(json.dumps({
                        "type": "status",
                        "data": {
                            "block_id": self.network.current_block_id,
                            "total_miners": len(self.network.miners),
                            "current_reward": self.network.get_current_block_reward(),
                            "total_minted": self.network.total_minted,
                            "remaining_supply": TOTAL_SUPPLY_CAP - self.network.total_minted
                        }
                    }))
        except Exception as e:
            print(f"[WS] Error: {e}")
    
    async def block_production_loop(self):
        level = 1
        while True:
            if self.network.level_groups:
                avail = [l for l in self.network.level_groups if len(self.network.level_groups[l]) >= MIN_VALIDATORS_PER_BLOCK]
                if avail:
                    level = (level % max(avail)) + 1
                    if level in avail:
                        await self.network.produce_block(level)
            await asyncio.sleep(0.5)
    
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
            remaining = TOTAL_SUPPLY_CAP - self.network.total_minted
            percent = (self.network.total_minted / TOTAL_SUPPLY_CAP) * 100 if TOTAL_SUPPLY_CAP > 0 else 0
            print(f"\n[STATUS] Block: {self.network.current_block_id} | Reward: {self.network.get_current_block_reward()} MCX")
            print(f"[STATUS] Miners: {len(self.network.miners)} | Peers: {len(self.network.p2p.peers)}")
            print(f"[SUPPLY] {self.network.total_minted:,} / {TOTAL_SUPPLY_CAP:,} ({percent:.2f}%) | Remaining: {remaining:,}\n")
    
    async def embedded_miner_loop(self):
        """Embedded miner that signs challenges"""
        while True:
            for challenge, pending in self.network.pending_challenges.items():
                vid = self.network.node_username
                if vid in pending["validators"] and vid not in pending["signatures"]:
                    message = f"{challenge}{vid}{pending['block_id']}"
                    signature = sign_message(self.network.node_priv, message)
                    pending["signatures"][vid] = signature
                    print(f"[EMBEDDED] Signed block {pending['block_id']}")
            await asyncio.sleep(0.2)
    
    async def run(self):
        asyncio.create_task(self.network.p2p.start())
        asyncio.create_task(self.network.p2p.heartbeat())
        asyncio.create_task(self.peer_discovery())
        asyncio.create_task(self.peer_sync())
        asyncio.create_task(self.block_production_loop())
        asyncio.create_task(self.status_reporter())
        asyncio.create_task(self.embedded_miner_loop())
        async with serve(self.handle, NODE_HOST, NODE_PORT):
            print(f"[WS] Server: ws://0.0.0.0:{NODE_PORT}")
            print(f"[P2P] Port: {P2P_PORT}")
            print(f"[EMBEDDED MINER] Active for '{self.network.node_username}'")
            print(f"[READY] Phone node running!")
            await asyncio.Future()

# ==================== MAIN ====================
async def main():
    parser = argparse.ArgumentParser(description=f'{NAME} Phone Node')
    parser.add_argument('--genesis', action='store_true', help='Run as genesis node')
    parser.add_argument('--peer', type=str, help='Connect to peer node (IP:PORT)')
    parser.add_argument('--username', type=str, default="phone_miner")
    parser.add_argument('--wallet', type=str, default="")
    args = parser.parse_args()
    
    print("\n" + "=" * 50)
    print(f"📱 {NAME} PHONE NODE v{VERSION}")
    print("=" * 50)
    
    # Generate or use wallet
    if args.wallet:
        wallet_addr = args.wallet
        # For demo, generate new key pair
        _, priv, pub = generate_wallet()
        print(f"Using wallet: {wallet_addr}")
    else:
        wallet_addr, priv, pub = generate_wallet()
        print(f"\n🆕 NEW WALLET CREATED!")
        print(f"Username: {args.username}")
        print(f"Wallet: {wallet_addr}")
        print(f"Private Key: {priv}")
        print(f"SAVE THESE!\n")
    
    network = MicroCoreNetwork(
        is_genesis_node=args.genesis,
        node_username=args.username,
        node_wallet=wallet_addr,
        node_priv=priv,
        node_pub=pub
    )
    
    server = MicroCoreServer(network)
    
    if args.peer:
        await network.p2p.connect_to_peer(args.peer)
    
    await server.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Phone node stopped")
        sys.exit(0)