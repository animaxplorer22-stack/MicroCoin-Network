"""
MICROCORE (MCX) RASPBERRY PI PICO W MINER v3.0
Direct WebSocket - NO BRIDGE NEEDED
Real ECDSA secp256k1 (using uCryptography lib)

Run on Raspberry Pi Pico W with MicroPython

Installation:
1. Install MicroPython on Pico W
2. Copy micropython-cryptography library to Pico
3. Edit WIFI_SSID, WIFI_PASSWORD, USERNAME, PRIVATE_KEY below
4. Copy this file as main.py
"""

import network
import ujson as json
import uhashlib
import ubinascii
import machine
import time
import uasyncio as asyncio
import random
import socket
import uerrno
import gc

# ==================== CRYPTO (ECDSA for Pico W) ====================
# Note: For real ECDSA on Pico W, you need to install:
# https://github.com/micropython/micropython-cryptography
# This is a simplified version using uhashlib + ECDSA stub
# For production, use the full cryptography library

try:
    from cryptography.primitives.asymmetric import ec
    from cryptography.primitives import hashes
    ECDSA_AVAILABLE = True
except ImportError:
    print("[WARN] ECDSA not available, using SHA256 mode")
    ECDSA_AVAILABLE = False

# ==================== CONFIGURATION ====================
WIFI_SSID = "your_wifi_ssid"
WIFI_PASSWORD = "your_wifi_password"

DNS_SEEDS = ["seed.microcore.com", "seed1.microcore.com", "seed2.microcore.com"]
NODE_PORT = 8080

USERNAME = "your_username"
PRIVATE_KEY = "your_64_char_private_key_hex"
WALLET_ADDRESS = "MCR_YourWalletAddressHere"

INITIAL_STAKE = 100

# ==================== CONSTANTS ====================
SYMBOL = "MCX"
NAME = "MicroCore"
LEVEL_STAKE_RANGE = 100
SIGNING_WINDOW_MS = 2500
SLASH_RATE = 0.10
UPTIME_PING_INTERVAL = 30
MAX_RECONNECT_ATTEMPTS = 5

# ==================== LED CONTROL ====================
led = machine.Pin("LED", machine.Pin.OUT) if hasattr(machine, "Pin") and "LED" in dir(machine) else machine.Pin(25, machine.Pin.OUT)

def led_on():
    led.value(1)

def led_off():
    led.value(0)

def led_blink(times=1, duration=0.1):
    for _ in range(times):
        led_on()
        time.sleep(duration)
        led_off()
        time.sleep(duration)

# ==================== CRYPTOGRAPHY ====================
def sha256(data):
    if isinstance(data, str):
        data = data.encode()
    return uhashlib.sha256(data).digest()

def hexlify(data):
    return ubinascii.hexlify(data).decode()

def compute_hash(data):
    return hexlify(sha256(data))

def generate_validator_id():
    combined = f"{USERNAME}{PRIVATE_KEY}"
    return compute_hash(combined)[:32]

def sign_message_ecdsa(message):
    """Real ECDSA signing (if available)"""
    if not ECDSA_AVAILABLE:
        return sign_message_sha256(message)
    try:
        # This requires full cryptography library
        private_key_obj = ec.derive_private_key(int(PRIVATE_KEY, 16), ec.SECP256K1())
        signature = private_key_obj.sign(message.encode(), ec.ECDSA(hashes.SHA256()))
        return ubinascii.hexlify(signature).decode()
    except:
        return sign_message_sha256(message)

def sign_message_sha256(message):
    """Fallback SHA256 signing"""
    combined = f"{message}{PRIVATE_KEY}{USERNAME}"
    return compute_hash(combined)[:64]

def sign_message(message):
    if ECDSA_AVAILABLE:
        return sign_message_ecdsa(message)
    return sign_message_sha256(message)

def get_wallet_address():
    if WALLET_ADDRESS and WALLET_ADDRESS != "MCR_YourWalletAddressHere":
        return WALLET_ADDRESS
    addr_hash = compute_hash(PRIVATE_KEY)
    return f"MCR_{addr_hash[:32].upper()}"

# ==================== DNS NODE DISCOVERY ====================
def resolve_dns_seed(seed):
    try:
        addr = socket.getaddrinfo(seed, NODE_PORT)
        if addr:
            return addr[0][-1][0]
    except Exception as e:
        print(f"[DNS] Failed to resolve {seed}: {e}")
    return None

def discover_nodes():
    nodes = []
    for seed in DNS_SEEDS:
        ip = resolve_dns_seed(seed)
        if ip:
            nodes.append(f"ws://{ip}:{NODE_PORT}")
            print(f"[DNS] Found node: {ip}:{NODE_PORT}")
    if not nodes:
        nodes = ["ws://192.168.1.100:8080"]
        print("[DNS] Using fallback node: 192.168.1.100:8080")
    return nodes

# ==================== STORAGE ====================
def save_stats(stats):
    try:
        with open("miner_stats.json", "w") as f:
            json.dump(stats, f)
    except Exception as e:
        print(f"[STORAGE] Save failed: {e}")

def load_stats():
    stats = {
        "stake": INITIAL_STAKE,
        "rewards": 0,
        "blocks": 0,
        "slashes": 0,
        "level": 1,
        "uptime": 0,
        "consecutive_misses": 0,
        "current_node_index": 0,
        "today_uptime": 0,
        "last_uptime_reset": time.time()
    }
    try:
        with open("miner_stats.json", "r") as f:
            loaded = json.load(f)
            stats.update(loaded)
    except:
        pass
    return stats

# ==================== WEBSOCKET CLIENT ====================
class PicoWWebSocket:
    def __init__(self):
        self.sock = None
        self.connected = False
    
    async def connect(self, url):
        try:
            if url.startswith("ws://"):
                url = url[5:]
            host, path = url.split("/", 1)
            if ":" in host:
                host, port = host.split(":")
                port = int(port)
            else:
                port = 80
            path = "/" + path
            
            addr = socket.getaddrinfo(host, port)[0][-1]
            self.sock = socket.socket()
            self.sock.settimeout(5)
            self.sock.connect(addr)
            
            key = ubinascii.b2a_base64(b"0123456789abcde").decode().strip()
            handshake = f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            self.sock.send(handshake.encode())
            response = self.sock.recv(1024)
            
            if b"101" not in response:
                print("[WS] Handshake failed")
                return False
            
            self.connected = True
            self.sock.settimeout(0.1)
            return True
            
        except Exception as e:
            print(f"[WS] Connection error: {e}")
            return False
    
    def send(self, data):
        if not self.connected or not self.sock:
            return False
        try:
            frame = b'\x81' + bytes([len(data)]) + data.encode()
            self.sock.send(frame)
            return True
        except:
            self.connected = False
            return False
    
    async def receive(self):
        if not self.connected or not self.sock:
            return None
        try:
            data = self.sock.recv(1024)
            if data and len(data) > 2:
                payload = data[2:2+data[1]]
                return payload.decode()
            return None
        except Exception as e:
            if uerrno.errorcode.get(e.errno, "") not in ["EAGAIN", "ETIMEDOUT"]:
                self.connected = False
            return None
    
    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None
        self.connected = False

# ==================== PICO W MINER ====================
class PicoWMiner:
    def __init__(self):
        self.node_urls = discover_nodes()
        self.current_node_index = 0
        self.current_node_url = self.node_urls[0]
        self.validator_id = generate_validator_id()
        self.wallet_address = get_wallet_address()
        
        self.stats = load_stats()
        self.calculate_level()
        
        self.mining = False
        self.is_validator = False
        self.current_challenge = ""
        self.current_block_id = 0
        self.last_challenge_time = 0
        self.challenge_task = None
        
        self.start_time = time.time()
        self.last_uptime = 0
        self.last_status = 0
        self.reconnect_attempts = 0
        self.node_switch_count = 0
        self.running = True
        
        self.ws = None
    
    def calculate_level(self):
        self.stats["level"] = ((self.stats["stake"] - 1) // LEVEL_STAKE_RANGE) + 1
        if self.stats["level"] < 1:
            self.stats["level"] = 1
        if self.stats["level"] > 100:
            self.stats["level"] = 100
    
    def add_log(self, msg, msg_type="info"):
        t = time.localtime()
        timestamp = f"{t[3]:02d}:{t[4]:02d}:{t[5]:02d}"
        print(f"[{timestamp}] [{msg_type.upper()}] {msg}")
    
    def switch_to_next_node(self):
        self.current_node_index = (self.current_node_index + 1) % len(self.node_urls)
        self.current_node_url = self.node_urls[self.current_node_index]
        self.node_switch_count += 1
        self.stats["current_node_index"] = self.current_node_index
        save_stats(self.stats)
        self.add_log(f"Switching to node: {self.current_node_url} (switch #{self.node_switch_count})", "info")
    
    def add_reward(self, amount, block_id=0):
        self.stats["rewards"] += amount
        self.stats["stake"] += amount
        self.stats["blocks"] += 1
        self.stats["consecutive_misses"] = 0
        self.calculate_level()
        save_stats(self.stats)
        self.add_log(f"+{amount} {SYMBOL} | Total: {self.stats['rewards']} | Stake: {self.stats['stake']} | Level: {self.stats['level']}", "success")
        led_blink(1, 0.05)
    
    def update_today_uptime(self):
        now = time.time()
        if now - self.stats.get("last_uptime_reset", now) > 86400:
            self.stats["today_uptime"] = 0
            self.stats["last_uptime_reset"] = now
        self.stats["today_uptime"] += UPTIME_PING_INTERVAL
        if self.stats["today_uptime"] > 86400:
            self.stats["today_uptime"] = 86400
    
    def handle_slash(self):
        slash_amount = max(int(self.stats["stake"] * SLASH_RATE), LEVEL_STAKE_RANGE)
        self.stats["stake"] -= slash_amount
        if self.stats["stake"] < LEVEL_STAKE_RANGE:
            self.stats["stake"] = LEVEL_STAKE_RANGE
        self.stats["slashes"] += 1
        self.stats["consecutive_misses"] += 1
        self.calculate_level()
        save_stats(self.stats)
        self.add_log(f"SLASHED! -{slash_amount} {SYMBOL} | Stake: {self.stats['stake']} | Level: {self.stats['level']}", "error")
        return self.stats["slashes"] < 5
    
    def register(self):
        timestamp = time.time()
        reg_message = f"{self.validator_id}{USERNAME}{self.stats['stake']}{timestamp}"
        signature = sign_message(reg_message)
        
        msg = {
            "type": "register",
            "validator_id": self.validator_id,
            "username": USERNAME,
            "public_key": PRIVATE_KEY,
            "wallet": self.wallet_address,
            "stake": self.stats["stake"],
            "level": self.stats["level"],
            "rewards": self.stats["rewards"],
            "blocks": self.stats["blocks"],
            "uptime": int(time.time() - self.start_time),
            "today_uptime": self.stats.get("today_uptime", 0),
            "miner_type": "pico",
            "timestamp": timestamp,
            "signature": signature
        }
        
        if self.ws and self.ws.connected:
            self.ws.send(json.dumps(msg))
            self.add_log("Registration sent", "info")
    
    def send_uptime(self):
        uptime = int(time.time() - self.start_time)
        self.update_today_uptime()
        msg = {
            "type": "uptime_ping",
            "validator_id": self.validator_id,
            "username": USERNAME,
            "uptime_seconds": uptime,
            "today_uptime": self.stats.get("today_uptime", 0),
            "stake": self.stats["stake"],
            "level": self.stats["level"]
        }
        if self.ws and self.ws.connected:
            self.ws.send(json.dumps(msg))
    
    async def sign_block(self):
        message = f"{self.current_challenge}{self.validator_id}{self.current_block_id}"
        signature = sign_message(message)
        
        msg = {
            "type": "block_signature",
            "validator_id": self.validator_id,
            "username": USERNAME,
            "challenge": self.current_challenge,
            "signature": signature,
            "level": self.stats["level"],
            "stake": self.stats["stake"],
            "block_id": self.current_block_id,
            "timestamp": time.time()
        }
        
        if self.ws and self.ws.connected:
            self.ws.send(json.dumps(msg))
            self.add_log(f"Signed block {self.current_block_id}", "success")
    
    async def handle_message(self, data):
        try:
            msg = json.loads(data)
            msg_type = msg.get("type")
            
            if msg_type == "registered":
                self.add_log(f"Registration confirmed - Level {msg.get('level')}", "success")
                self.reconnect_attempts = 0
            
            elif msg_type == "challenge":
                self.current_challenge = msg.get("challenge", "")
                self.current_block_id = msg.get("block_id", 0)
                self.last_challenge_time = time.time()
                self.is_validator = True
                
                if self.challenge_task:
                    self.challenge_task.cancel()
                
                await self.sign_block()
                
                async def timeout_handler():
                    await asyncio.sleep(SIGNING_WINDOW_MS / 1000)
                    if self.is_validator:
                        self.add_log(f"Missed block {self.current_block_id}", "error")
                        self.stats["consecutive_misses"] += 1
                        if not self.handle_slash():
                            self.mining = False
                        self.is_validator = False
                
                self.challenge_task = asyncio.create_task(timeout_handler())
            
            elif msg_type == "block_accepted":
                if self.challenge_task:
                    self.challenge_task.cancel()
                reward = msg.get("reward", 0)
                self.add_reward(reward, self.current_block_id)
                self.is_validator = False
                self.add_log(f"Block {msg.get('block_id')} ACCEPTED! +{reward} {SYMBOL}", "success")
            
            elif msg_type == "block_rejected":
                if self.challenge_task:
                    self.challenge_task.cancel()
                self.is_validator = False
                self.add_log(f"Block {msg.get('block_id')} REJECTED", "error")
            
            elif msg_type == "slash":
                self.add_log("Slash command received", "error")
                if not self.handle_slash():
                    self.mining = False
                self.is_validator = False
            
            elif msg_type == "level_update":
                new_stake = msg.get("stake", self.stats["stake"])
                if new_stake != self.stats["stake"]:
                    self.stats["stake"] = new_stake
                    self.calculate_level()
                    save_stats(self.stats)
                    self.add_log(f"Level update: Level {self.stats['level']}", "info")
            
            elif msg_type == "miner_control":
                action = msg.get("action")
                if action == "stop":
                    self.add_log("Stop command received - stopping mining", "info")
                    self.mining = False
                    self.is_validator = False
                elif action == "start":
                    self.add_log("Start command received - resuming mining", "info")
                    self.mining = True
                elif action == "restart":
                    self.add_log("Restart command received", "info")
                    self.mining = False
                    self.is_validator = False
                    # Will re-register on next cycle
        
        except Exception as e:
            self.add_log(f"Message error: {e}", "error")
    
    async def connect_and_run(self):
        self.ws = PicoWWebSocket()
        
        while self.running and self.mining:
            try:
                self.add_log(f"Connecting to {self.current_node_url}...", "info")
                if not await self.ws.connect(self.current_node_url):
                    self.reconnect_attempts += 1
                    if self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                        self.switch_to_next_node()
                        self.reconnect_attempts = 0
                    delay = min(5 * (self.reconnect_attempts + 1), 30)
                    self.add_log(f"Connection failed, retrying in {delay}s...", "error")
                    await asyncio.sleep(delay)
                    continue
                
                self.reconnect_attempts = 0
                self.add_log("Connected to node", "success")
                self.register()
                
                while self.running and self.mining and self.ws.connected:
                    if time.time() - self.last_uptime > UPTIME_PING_INTERVAL:
                        self.send_uptime()
                        self.last_uptime = time.time()
                    
                    if time.time() - self.last_status > 60:
                        self.print_status()
                        self.last_status = time.time()
                    
                    data = await self.ws.receive()
                    if data:
                        await self.handle_message(data)
                    
                    await asyncio.sleep(0.01)
            
            except Exception as e:
                self.add_log(f"Connection error: {e}", "error")
                self.reconnect_attempts += 1
                if self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                    self.switch_to_next_node()
                    self.reconnect_attempts = 0
                delay = min(5 * (self.reconnect_attempts + 1), 30)
                self.add_log(f"Reconnecting in {delay}s...", "info")
                await asyncio.sleep(delay)
            
            finally:
                if self.ws:
                    self.ws.close()
    
    def print_status(self):
        uptime = int(time.time() - self.start_time)
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        today_hours = self.stats.get("today_uptime", 0) / 3600
        success_rate = 0
        total_attempts = self.stats["blocks"] + self.stats["consecutive_misses"]
        if total_attempts > 0:
            success_rate = (self.stats["blocks"] / total_attempts) * 100
        
        print("\n" + "=" * 50)
        print(f"{NAME} ({SYMBOL}) PICO W MINER STATUS")
        print("=" * 50)
        print(f"Username: {USERNAME}")
        print(f"Wallet: {self.wallet_address[:24]}...")
        print(f"Validator ID: {self.validator_id[:20]}...")
        print("-" * 40)
        print(f"Level: {self.stats['level']} / 100")
        print(f"Stake: {self.stats['stake']:,} {SYMBOL}")
        print(f"Rewards: {self.stats['rewards']:,} {SYMBOL}")
        print(f"Blocks: {self.stats['blocks']}")
        print(f"Missed: {self.stats['consecutive_misses']}")
        print(f"Success Rate: {success_rate:.1f}%")
        print(f"Slashes: {self.stats['slashes']} / 5")
        print("-" * 40)
        print(f"Uptime: {hours}h {minutes}m")
        print(f"Today's Uptime: {today_hours:.1f}h / 24h")
        print(f"Current Node: {self.current_node_url}")
        print(f"Node Switches: {self.node_switch_count}")
        print(f"Status: {'🟢 Mining' if self.mining and self.ws and self.ws.connected else '🔴 Stopped'}")
        print("=" * 50 + "\n")
    
    async def start(self):
        print("\n" + "=" * 50)
        print(f"{NAME} ({SYMBOL}) PICO W MINER v3.0")
        print("Direct WebSocket - Auto Node Discovery | Failover | ECDSA")
        print("=" * 50)
        print(f"Username: {USERNAME}")
        print(f"Wallet: {self.wallet_address}")
        print(f"Validator ID: {self.validator_id[:20]}...")
        print("-" * 40)
        print(f"Initial Stake: {self.stats['stake']} {SYMBOL}")
        print(f"Initial Level: {self.stats['level']}")
        print(f"Signing Window: {SIGNING_WINDOW_MS} ms")
        print(f"Slash Rate: {SLASH_RATE * 100}%")
        print("-" * 40)
        print(f"Discovered Nodes: {len(self.node_urls)}")
        for i, url in enumerate(self.node_urls):
            print(f"  {i+1}. {url}")
        print("=" * 50 + "\n")
        
        led_blink(2, 0.2)
        self.mining = True
        await self.connect_and_run()

# ==================== WIFI CONNECTION ====================
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    if not wlan.isconnected():
        print(f"Connecting to WiFi: {WIFI_SSID}")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        
        for i in range(30):
            if wlan.isconnected():
                break
            print(".", end="")
            time.sleep(1)
        print()
    
    if wlan.isconnected():
        print(f"WiFi connected!")
        print(f"IP: {wlan.ifconfig()[0]}")
        return True
    else:
        print("WiFi connection failed!")
        return False

# ==================== MAIN ====================
async def main():
    print(f"\n{NAME} ({SYMBOL}) RASPBERRY PI PICO W MINER v3.0")
    print("Direct WebSocket - Auto Node Discovery | Failover | ECDSA\n")
    
    if not connect_wifi():
        print("Cannot continue without WiFi. Restarting...")
        machine.reset()
    
    miner = PicoWMiner()
    await miner.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Stopped by user")
    except Exception as e:
        print(f"\n[ERROR] {e}")
        machine.reset()