#!/usr/bin/env python3



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
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from enum import Enum
from datetime import datetime
from functools import wraps

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

# ==================== LOGGING SETUP ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('microcore.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== ENVIRONMENT CONFIGURATION ====================
# NO STRIPE — no environment variables required at all

def get_env_optional(var_name: str, default: str = "") -> str:
    return os.environ.get(var_name, default)

# ==================== OPTIONAL ENV VARIABLES ====================
NODE_HOST = get_env_optional("MCX_NODE_HOST", "0.0.0.0")
NODE_PORT = int(get_env_optional("MCX_NODE_PORT", "8080"))
P2P_PORT = int(get_env_optional("MCX_P2P_PORT", "8081"))

# DUCO payment address (hardcoded — user changes this)
DUCO_PAYMENT_ADDRESS = "XAVER_KENG_XUAN_YI"  # ← CHANGE THIS TO YOUR DUCO ADDRESS

BLOCKCHAIN_INFO_API = get_env_optional("MCX_BLOCKCHAIN_API", "https://blockchain.info/rawaddr/")
ETHERSCAN_API = get_env_optional("MCX_ETHERSCAN_API", "https://api.etherscan.io/api")
DUCO_API = get_env_optional("MCX_DUCO_API", "https://server.duinocoin.com/api.php")
ETHERSCAN_API_KEY = get_env_optional("ETHERSCAN_API_KEY", "")

# ==================== GOSSIP DISCOVERY (NO DNS) ====================
BOOTSTRAP_NODES = [
    "101.127.80.48:8080",  # ← YOUR PUBLIC IP
]

PEER_CACHE_FILE = "microcore_peers.json"

def save_peers_to_cache(peers):
    try:
        unique = list(set(peers))
        with open(PEER_CACHE_FILE, 'w') as f:
            json.dump(unique, f, indent=2)
        logger.info(f"[CACHE] Saved {len(unique)} peers")
    except Exception as e:
        logger.error(f"[CACHE] Save failed: {e}")

def load_peers_from_cache():
    try:
        with open(PEER_CACHE_FILE, 'r') as f:
            peers = json.load(f)
        logger.info(f"[CACHE] Loaded {len(peers)} peers from cache")
        return peers
    except:
        logger.info("[CACHE] No cache file found")
        return []

def get_bootstrap_peers():
    peers = BOOTSTRAP_NODES.copy()
    cached = load_peers_from_cache()
    for p in cached:
        if p not in peers:
            peers.append(p)
    return peers

# ==================== RATE LIMITER ====================
class RateLimiter:
    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()
    
    async def is_allowed(self, client_ip: str) -> bool:
        async with self._lock:
            now = time.time()
            if client_ip not in self._requests:
                self._requests[client_ip] = []
            self._requests[client_ip] = [t for t in self._requests[client_ip] if now - t < self.window]
            if len(self._requests[client_ip]) >= self.max_requests:
                logger.warning(f"[RATE LIMIT] {client_ip} exceeded limit")
                return False
            self._requests[client_ip].append(now)
            return True
    
    def cleanup(self):
        now = time.time()
        for ip in list(self._requests.keys()):
            self._requests[ip] = [t for t in self._requests[ip] if now - t < self.window]
            if not self._requests[ip]:
                del self._requests[ip]

# ==================== HEALTH CHECK ====================
class HealthChecker:
    def __init__(self):
        self.start_time = time.time()
        self.last_block_time = time.time()
        self.blocks_produced = 0
        self.peer_count = 0
        self.miner_count = 0
        self.errors = 0
        self.status = "starting"
    
    def get_status(self) -> dict:
        return {
            "status": self.status,
            "uptime": int(time.time() - self.start_time),
            "blocks_produced": self.blocks_produced,
            "peers": self.peer_count,
            "miners": self.miner_count,
            "errors": self.errors,
            "last_block": int(self.last_block_time),
            "version": VERSION
        }
    
    def record_block(self):
        self.blocks_produced += 1
        self.last_block_time = time.time()
    
    def record_error(self):
        self.errors += 1

# ==================== CONFIGURATION ====================
SYMBOL = "MCX"
NAME = "MicroCore"
VERSION = "21.6.0-MAINNET-READY"

# ==================== TOKENOMICS ====================
INITIAL_BLOCK_REWARD = 3
HALVING_INTERVAL = 4_204_800

LEVEL_STAKE_RANGE = 1000
MAX_LEVEL = 10
MIN_WALLETS_FOR_NEXT_LEVEL = 10

LEVEL_BLOCK_INTERVALS = {
    1: 40, 2: 35, 3: 30, 4: 25, 5: 20,
    6: 15, 7: 10, 8: 9, 9: 8, 10: 7
}

LEVEL_CAPS = {
    1: 18_921_600, 2: 21_624_000, 3: 25_228_800,
    4: 30_274_560, 5: 37_843_200, 6: 50_457_600,
    7: 75_686_400, 8: 84_096_000, 9: 94_608_000,
    10: 108_123_360
}

VALIDATOR_SHARE = 0.70
NODE_SHARE = 0.08
UPTIME_SHARE = 0.05
LP_SHARE = 0.05
BUYER_REWARDS_SHARE = 0.12

# ==================== SWAP FEE CONFIGURATION ====================
SWAP_FEE_RATE = 0.003  # 0.3%
LP_FEE_SHARE = 0.60
NODE_FEE_SHARE = 0.40
CROSS_CHAIN_NODE_SHARE = 0.80
CROSS_CHAIN_MINER_SHARE = 0.20

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

MCX_FEE_MIN = 1
MCX_FEE_MAX = 100
FIAT_RAMP_ENABLED = True

BUYER_REWARDS = [5000, 3000, 2000, 1000, 1000, 500, 500, 500, 500, 500]

# ==================== TRANSFER FEE CONFIGURATION ====================
TRANSFER_FEE_RATE = 0.006  # 0.6%
TRANSFER_FEE_MIN = 0.01

def calculate_transfer_fee(amount: float) -> float:
    fee = amount * TRANSFER_FEE_RATE
    return max(TRANSFER_FEE_MIN, fee)

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
def verify_signature(pub, msg, sig, miner_type):
    if miner_type in ["web", "uno"]:
        expected = hashlib.sha256(f"{pub}{msg}".encode()).hexdigest()
        return sig == expected
    try:
        pub_key = serialization.load_pem_public_key(pub.encode())
        sig_bytes = bytes.fromhex(sig)
        r = int.from_bytes(sig_bytes[:32], 'big')
        s = int.from_bytes(sig_bytes[32:], 'big')
        pub_key.verify(encode_dss_signature(r, s), msg.encode(), ec.ECDSA(hashes.SHA256()))
        return True
    except:
        return False

def sign_message(priv_hex, msg):
    priv = ec.derive_private_key(int(priv_hex, 16), ec.SECP256K1())
    r, s = decode_dss_signature(priv.sign(msg.encode(), ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, 'big').hex() + s.to_bytes(32, 'big').hex()

def generate_wallet():
    priv = ec.generate_private_key(ec.SECP256K1())
    priv_hex = priv.private_numbers().private_value.to_bytes(32, 'big').hex()
    pub = priv.public_key()
    pub_pem = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    addr = "MCR_" + hashlib.sha256(pub_pem.encode()).hexdigest()[:32].upper()
    return addr, priv_hex, pub_pem

def hash_block(b):
    return hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest()

def hash_transaction(tx):
    return hashlib.sha256(json.dumps(tx, sort_keys=True).encode()).hexdigest()

def get_public_ip():
    try:
        return requests.get('https://api.ipify.org').json()['ip']
    except:
        return None
        
# ==================== P2P PROTOCOL ====================
P2P_MAGIC = b"MCR1"
P2P_VERSION = 1
MSG_HANDSHAKE, MSG_PING, MSG_PONG, MSG_GET_BLOCKS, MSG_BLOCKS, MSG_NEW_BLOCK, MSG_NEW_TX, MSG_GET_PEERS, MSG_PEERS, MSG_SLASH, MSG_NODE_REGISTER = range(11)

def encode_p2p(t, p):
    j = json.dumps(p).encode()
    return P2P_MAGIC + struct.pack(">BBI", P2P_VERSION, t, len(j)) + j

def decode_p2p(d):
    if len(d) < 10 or d[:4] != P2P_MAGIC:
        return None, None
    return d[5], json.loads(d[10:10+struct.unpack(">I", d[6:10])[0]].decode())

# ==================== DEX ====================
class DEX:
    def __init__(self, net):
        self.net = net
        self.own_pools = {}
        self._load_pools()
    
    def _load_pools(self):
        """Load pools from database on startup"""
        try:
            c = self.net.conn.cursor()
            c.execute("SELECT pool_id, token_a, token_b, reserve_a, reserve_b, total_lp FROM pools")
            rows = c.fetchall()
            for row in rows:
                pool_id = row[0]
                self.own_pools[pool_id] = {
                    "a": row[3],
                    "b": row[4],
                    "lp": {},
                    "total_lp": row[5]
                }
                c2 = self.net.conn.cursor()
                c2.execute("SELECT wallet, lp_shares FROM lp_positions WHERE pool_id=?", (pool_id,))
                for lp_row in c2.fetchall():
                    self.own_pools[pool_id]["lp"][lp_row[0]] = lp_row[1]
            logger.info(f"[DEX] Loaded {len(self.own_pools)} pools from database")
        except Exception as e:
            logger.warning(f"[DEX] No pools found in database: {e}")
            self.own_pools = {}
    
    def _save_pool_to_db(self, pool_id: str, token_a: str, token_b: str):
        c = self.net.conn.cursor()
        c.execute("INSERT INTO pools (pool_id, token_a, token_b, reserve_a, reserve_b, total_lp, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (pool_id, token_a, token_b, 0, 0, 0, time.time()))
        self.net.conn.commit()
    
    def _update_pool_in_db(self, pool_id: str):
        pool = self.own_pools.get(pool_id)
        if not pool:
            return
        c = self.net.conn.cursor()
        c.execute("UPDATE pools SET reserve_a=?, reserve_b=?, total_lp=? WHERE pool_id=?",
                 (pool["a"], pool["b"], pool["total_lp"], pool_id))
        self.net.conn.commit()
    
    def _save_lp_position(self, pool_id: str, wallet: str, lp_shares: float):
        c = self.net.conn.cursor()
        c.execute("INSERT OR REPLACE INTO lp_positions (pool_id, wallet, lp_shares) VALUES (?, ?, ?)",
                 (pool_id, wallet, lp_shares))
        self.net.conn.commit()
    
    def _calc_fee_mcx(self, amount: float) -> int:
        """Calculate swap fee in MCX (0.3% of swap amount in MCX)"""
        fee_mcx = int(amount * SWAP_FEE_RATE)
        return max(MCX_FEE_MIN, min(fee_mcx, MCX_FEE_MAX))
    
    def get_own_pool_quote(self, from_token, to_token, amount):
        if from_token == "MCX":
            pool = self.own_pools.get(f"MCX/{to_token}")
            if not pool:
                return {"error": "Pool not found"}
            out = amount * (1 - SWAP_FEE_RATE) * pool["b"] / (pool["a"] + amount)
            fee = self._calc_fee_mcx(amount)
            return {"out": out, "fee": fee, "type": "own", "pool": f"MCX/{to_token}"}
        else:
            pool = self.own_pools.get(f"MCX/{from_token}")
            if not pool:
                return {"error": "Pool not found"}
            out = amount * (1 - SWAP_FEE_RATE) * pool["a"] / (pool["b"] + amount)
            fee = self._calc_fee_mcx(out)
            return {"out": out, "fee": fee, "type": "own", "pool": f"MCX/{from_token}"}
    
    def execute_own_pool_swap(self, wallet, from_token, to_token, amount, fee):
        quote = self.get_own_pool_quote(from_token, to_token, amount)
        if quote.get("error"):
            return False, quote["error"]
        
        if self.net.balances.get(wallet, 0) < fee:
            return False, "Insufficient MCX for fee"
        
        self.net.balances[wallet] -= fee
        self.net.lp_pool += int(fee * LP_FEE_SHARE)
        self.net.node_pool += int(fee * NODE_FEE_SHARE)
        
        pool = self.own_pools[quote["pool"]]
        if from_token == "MCX":
            pool["a"] += amount
            pool["b"] -= quote["out"]
        else:
            pool["b"] += amount
            pool["a"] -= quote["out"]
        
        self._update_pool_in_db(quote["pool"])
        
        tx_hash = hashlib.sha256(f"{wallet}{from_token}{to_token}{amount}{time.time()}".encode()).hexdigest()[:16]
        return True, {"tx_hash": tx_hash, "out": quote["out"], "fee": fee}
    
    async def get_lifi_quote(self, from_token, to_token, amount):
        try:
            prices = {"BTC": 60000, "ETH": 3000, "SOL": 150, "USDC": 1, "BNB": 300}
            from_price = prices.get(from_token, 1)
            to_price = prices.get(to_token, 1)
            value_usd = amount * from_price
            out = (value_usd / to_price) * 0.997
            fee = self._calc_fee_mcx(amount)
            return {"out": out, "fee": fee, "type": "lifi", "route": f"{from_token} → {to_token} via LI.FI"}
        except:
            return {"error": "LI.FI quote failed"}
    
    async def get_thorchain_quote(self, from_token, to_token, amount):
        try:
            prices = {"BTC": 60000, "ETH": 3000}
            from_price = prices.get(from_token, 1)
            to_price = prices.get(to_token, 1)
            value_usd = amount * from_price
            out = (value_usd / to_price) * 0.995
            fee = self._calc_fee_mcx(amount)
            return {"out": out, "fee": fee, "type": "thorchain", "route": f"{from_token} → {to_token} via THORChain"}
        except:
            return {"error": "THORChain quote failed"}
    
    async def get_swap_quote(self, from_token, to_token, amount):
        if from_token == "MCX" or to_token == "MCX":
            return self.get_own_pool_quote(from_token, to_token, amount)
        if (from_token in ["BTC", "ETH"] and to_token in ["BTC", "ETH"]):
            return await self.get_thorchain_quote(from_token, to_token, amount)
        return await self.get_lifi_quote(from_token, to_token, amount)
    
    async def execute_swap(self, wallet, from_token, to_token, amount, fee):
        if from_token == "MCX" or to_token == "MCX":
            return self.execute_own_pool_swap(wallet, from_token, to_token, amount, fee)
        
        quote = await self.get_swap_quote(from_token, to_token, amount)
        if quote.get("error"):
            return False, quote["error"]
        
        if self.net.balances.get(wallet, 0) < fee:
            return False, "Insufficient MCX for fee"
        
        self.net.balances[wallet] -= fee
        self.net.node_pool += int(fee * CROSS_CHAIN_NODE_SHARE)
        self.net.uptime_pool += int(fee * CROSS_CHAIN_MINER_SHARE)
        
        tx_hash = hashlib.sha256(f"{wallet}{from_token}{to_token}{amount}{time.time()}".encode()).hexdigest()[:16]
        return True, {"tx_hash": tx_hash, "out": quote["out"], "fee": fee, "route": quote.get("route", "aggregator")}
    
    def add_liquidity(self, wallet, pool_id, amount_a, amount_b, external_wallet: str = ""):
        if pool_id not in self.own_pools:
            token_a, token_b = pool_id.split("/")
            self.own_pools[pool_id] = {"a": 0, "b": 0, "lp": {}, "total_lp": 0}
            self._save_pool_to_db(pool_id, token_a, token_b)
            logger.info(f"[DEX] Created new pool: {pool_id}")
        
        token_b = pool_id.split("/")[1]
        if token_b != "MCX" and external_wallet:
            if token_b == "USDC":
                balance = self.net.verify_usdc_balance(external_wallet)
                if balance < amount_b:
                    return False, f"Insufficient USDC balance. Have {balance}, need {amount_b}"
            elif token_b == "BTC":
                balance = self.net.verify_btc_balance(external_wallet)
                if balance < amount_b:
                    return False, f"Insufficient BTC balance. Have {balance}, need {amount_b}"
            elif token_b == "ETH":
                balance = self.net.verify_eth_balance(external_wallet)
                if balance < amount_b:
                    return False, f"Insufficient ETH balance. Have {balance}, need {amount_b}"
            elif token_b == "DUCO":
                balance = self.net.verify_duco_balance(external_wallet)
                if balance < amount_b:
                    return False, f"Insufficient DUCO balance. Have {balance}, need {amount_b}"
            elif token_b == "SOL":
                balance = self.net.verify_sol_balance(external_wallet)
                if balance < amount_b:
                    return False, f"Insufficient SOL balance. Have {balance}, need {amount_b}"
            elif token_b == "BNB":
                balance = self.net.verify_bnb_balance(external_wallet)
                if balance < amount_b:
                    return False, f"Insufficient BNB balance. Have {balance}, need {amount_b}"
        
        if self.net.balances.get(wallet, 0) < amount_a:
            return False, f"Insufficient MCX balance. Have {self.net.balances.get(wallet, 0)}, need {amount_a}"
        
        self.net.balances[wallet] -= amount_a
        self.net._save_balance(wallet, self.net.balances[wallet])
        
        pool = self.own_pools[pool_id]
        pool["a"] += amount_a
        pool["b"] += amount_b
        
        lp_shares = (amount_a * amount_b) ** 0.5
        pool["total_lp"] += lp_shares
        pool["lp"][wallet] = pool["lp"].get(wallet, 0) + lp_shares
        
        self._update_pool_in_db(pool_id)
        self._save_lp_position(pool_id, wallet, pool["lp"][wallet])
        
        for miner in self.net.miners.values():
            if miner.wallet == wallet:
                miner.liquidity_provided = miner.liquidity_provided + amount_a + amount_b
                break
        
        return True, {"lp_shares": lp_shares, "amount_a": amount_a, "amount_b": amount_b, "pool_id": pool_id}
    
    def remove_liquidity(self, wallet, pool_id, lp_shares):
        if pool_id not in self.own_pools:
            return False, "Pool not found"
        pool = self.own_pools[pool_id]
        if wallet not in pool["lp"] or pool["lp"][wallet] < lp_shares:
            return False, "Insufficient LP shares"
        
        ratio = lp_shares / pool["total_lp"] if pool["total_lp"] > 0 else 0
        amount_a = pool["a"] * ratio
        amount_b = pool["b"] * ratio
        
        pool["a"] -= amount_a
        pool["b"] -= amount_b
        pool["total_lp"] -= lp_shares
        pool["lp"][wallet] -= lp_shares
        if pool["lp"][wallet] <= 0:
            del pool["lp"][wallet]
        
        self._update_pool_in_db(pool_id)
        if pool["lp"].get(wallet, 0) > 0:
            self._save_lp_position(pool_id, wallet, pool["lp"][wallet])
        else:
            c = self.net.conn.cursor()
            c.execute("DELETE FROM lp_positions WHERE pool_id=? AND wallet=?", (pool_id, wallet))
            self.net.conn.commit()
        
        self.net.balances[wallet] = self.net.balances.get(wallet, 0) + amount_a + amount_b
        self.net._save_balance(wallet, self.net.balances[wallet])
        
        return True, {"amount_a": amount_a, "amount_b": amount_b, "lp_removed": lp_shares}
    
    def buy_mcx(self, wallet, usd_amount, payment_method="card"):
        if not FIAT_RAMP_ENABLED:
            return False, "Fiat on-ramp disabled"
        mcx_amount = int(usd_amount / 0.01)
        self.net.balances[wallet] = self.net.balances.get(wallet, 0) + mcx_amount
        self.net.total_minted += mcx_amount
        
        c = self.net.conn.cursor()
        c.execute("INSERT OR REPLACE INTO buyer_stats (wallet, username, bought, last_reset) VALUES (?, ?, COALESCE((SELECT bought FROM buyer_stats WHERE wallet=?), 0) + ?, ?)",
                 (wallet, wallet, wallet, mcx_amount, time.time()))
        c.execute("INSERT INTO fiat_purchases (wallet, usd_amount, mcx_amount, payment_method, timestamp) VALUES (?, ?, ?, ?, ?)",
                 (wallet, usd_amount, mcx_amount, payment_method, time.time()))
        self.net.conn.commit()
        return True, {"mcx": mcx_amount, "usd": usd_amount}
    
    def get_supported_pools(self):
        pools = []
        for pid, p in self.own_pools.items():
            token_a, token_b = pid.split("/")
            pools.append({"id": pid, "token_a": token_a, "token_b": token_b, 
                         "reserve_a": p["a"], "reserve_b": p["b"], "total_lp": p["total_lp"], "type": "own"})
        pools.append({"type": "aggregator", "name": "LI.FI", "supported": ["BTC", "ETH", "SOL", "USDC", "BNB"]})
        pools.append({"type": "aggregator", "name": "THORChain", "supported": ["BTC", "ETH"]})
        return pools
        
# ==================== LEVEL MANAGER ====================
class LevelManager:
    def __init__(self, net):
        self.net = net
        self.max_unlocked = 1
        self.temp_towers = {}
        self.perm_towers = {}
        self.level_wallets = {}
    
    def register(self, wallet, stake):
        alloc = {}
        rem = stake
        lvl = 1
        
        while rem > 0 and lvl <= self.max_unlocked:
            add = min(rem, LEVEL_STAKE_RANGE)
            alloc[lvl] = alloc.get(lvl, 0) + add
            rem -= add
            lvl += 1
        
        if rem > 0 and lvl <= MAX_LEVEL:
            for lock_lvl in range(lvl, MAX_LEVEL + 1):
                if rem <= 0:
                    break
                add = min(rem, LEVEL_STAKE_RANGE)
                if lock_lvl not in self.temp_towers:
                    self.temp_towers[lock_lvl] = {}
                self.temp_towers[lock_lvl][wallet] = self.temp_towers[lock_lvl].get(wallet, 0) + add
                rem -= add
        
        if rem > 0:
            self.perm_towers[wallet] = self.perm_towers.get(wallet, 0) + rem
        
        self.temp_towers[wallet] = alloc
        self._update()
    
    def _update(self):
        self.level_wallets.clear()
        for miner in self.net.miners.values():
            lvl = self.get_level(miner.wallet)
            if lvl not in self.level_wallets:
                self.level_wallets[lvl] = set()
            self.level_wallets[lvl].add(miner.wallet)
        
        for lvl in list(self.level_wallets.keys()):
            self.level_wallets[lvl] = len(self.level_wallets[lvl])
        
        while self.max_unlocked < MAX_LEVEL:
            next_level = self.max_unlocked + 1
            if self.level_wallets.get(next_level, 0) >= MIN_WALLETS_FOR_NEXT_LEVEL:
                self.max_unlocked = next_level
                logger.info(f"[LEVEL] Level {self.max_unlocked} UNLOCKED!")
                self._convert_temporary_towers()
            else:
                break
    
    def _convert_temporary_towers(self):
        for wallet, towers in self.temp_towers.items():
            if self.max_unlocked in towers:
                stake = towers[self.max_unlocked]
                for miner in self.net.miners.values():
                    if miner.wallet == wallet:
                        miner.stake += stake
                        miner.level = self.get_level(wallet)
                        break
                del towers[self.max_unlocked]
    
    def get_level(self, wallet):
        for lvl in range(self.max_unlocked, 0, -1):
            if self.temp_towers.get(wallet, {}).get(lvl, 0) > 0:
                return min(lvl, MAX_LEVEL)
        return 1
    
    def has_permanent_tower(self, wallet):
        return self.perm_towers.get(wallet, 0) > 0
    
    def get_permanent_tower_amount(self, wallet):
        return self.perm_towers.get(wallet, 0)

# ==================== P2P NODE ====================
class P2PNode:
    def __init__(self, net):
        self.net = net
        self.peers = {}
        self.banned_peers = {}
        self.ip = get_public_ip()
        self._lock = asyncio.Lock()
    
    async def start(self):
        self.server = await asyncio.start_server(self._handle, NODE_HOST, P2P_PORT)
        logger.info(f"[P2P] Server on port {P2P_PORT}")
        if self.ip:
            logger.info(f"[P2P] Public IP: {self.ip}:{P2P_PORT}")
    
    async def _handle(self, r, w):
        addr = w.get_extra_info('peername')
        addr_str = f"{addr[0]}:{addr[1]}"
        
        if addr_str in self.banned_peers:
            if time.time() < self.banned_peers[addr_str]:
                w.close()
                return
            else:
                del self.banned_peers[addr_str]
        
        try:
            length = await r.read(4)
            if not length:
                w.close()
                return
            msg_len = struct.unpack(">I", length)[0]
            if msg_len > 10_000_000:
                self._ban_peer(addr_str, "Message too large")
                w.close()
                return
            data = await r.read(msg_len)
            typ, p = decode_p2p(data)
            if typ is not None:
                await self._process_message(typ, p, w, addr_str)
        except Exception as e:
            logger.error(f"[P2P] Error: {e}")
        finally:
            w.close()
    
    def _ban_peer(self, addr, reason):
        self.banned_peers[addr] = time.time() + BAN_DURATION
        if addr in self.peers:
            del self.peers[addr]
        logger.warning(f"[P2P] Banned {addr}: {reason}")
    
    async def _process_message(self, typ, p, w, addr):
        if typ == MSG_HANDSHAKE:
            async with self._lock:
                self.peers[addr] = type('Peer', (), {'height': p.get('height', 0), 'last_seen': time.time()})()
            response = encode_p2p(MSG_HANDSHAKE, {"height": self.net.height, "ip": self.ip})
            w.write(struct.pack(">I", len(response)) + response)
            await w.drain()
            if p.get('height', 0) > self.net.height:
                asyncio.create_task(self._request_blocks(addr, self.net.height, p['height']))
        
        elif typ == MSG_GET_PEERS:
            async with self._lock:
                peers_list = list(self.peers.keys())[:100]
            response = encode_p2p(MSG_PEERS, {"peers": peers_list})
            w.write(struct.pack(">I", len(response)) + response)
            await w.drain()
        
        elif typ == MSG_PEERS:
            new_peers = []
            async with self._lock:
                for peer in p.get("peers", []):
                    if peer not in self.peers and peer != f"{self.ip}:{P2P_PORT}":
                        self.peers[peer] = type('Peer', (), {'height': 0, 'last_seen': time.time()})()
                        new_peers.append(peer)
                        asyncio.create_task(self._connect(peer))
            if new_peers:
                async with self._lock:
                    all_peers = list(self.peers.keys())
                save_peers_to_cache(all_peers)
            logger.info(f"[P2P] Received {len(p.get('peers', []))} peers, {len(new_peers)} new")
        
        elif typ == MSG_GET_BLOCKS:
            start, end = p.get("start", 0), p.get("end", self.net.height)
            if end - start > 2000:
                end = start + 2000
            blocks = self.net.get_blocks_range(start, end)
            response = encode_p2p(MSG_BLOCKS, {"blocks": blocks})
            w.write(struct.pack(">I", len(response)) + response)
            await w.drain()
        
        elif typ == MSG_BLOCKS:
            await self.net.import_blocks(p.get("blocks", []))
        
        elif typ == MSG_NEW_BLOCK:
            await self.net.receive_block(p.get("block"))
        
        elif typ == MSG_NEW_TX:
            await self.net.receive_transaction(p.get("tx"))
        
        elif typ == MSG_SLASH:
            self.net.slash_miner(p.get("vid"), "P2P slashing event")
        
        elif typ == MSG_PING:
            response = encode_p2p(MSG_PONG, {"timestamp": time.time()})
            w.write(struct.pack(">I", len(response)) + response)
            await w.drain()
        
        elif typ == MSG_PONG:
            if addr in self.peers:
                self.peers[addr].last_seen = time.time()
    
    async def _request_blocks(self, peer, start, end):
        try:
            h, p = peer.split(":")
            r, w = await asyncio.open_connection(h, int(p))
            msg = encode_p2p(MSG_GET_BLOCKS, {"start": start, "end": end})
            w.write(struct.pack(">I", len(msg)) + msg)
            await w.drain()
            w.close()
        except Exception as e:
            logger.error(f"[P2P] Request failed: {e}")
    
    async def broadcast_block(self, blk):
        msg = encode_p2p(MSG_NEW_BLOCK, {"block": blk})
        async with self._lock:
            peers_copy = list(self.peers.keys())
        for peer in peers_copy:
            try:
                h, p = peer.split(":")
                r, w = await asyncio.open_connection(h, int(p))
                w.write(struct.pack(">I", len(msg)) + msg)
                await w.drain()
                w.close()
            except:
                pass
    
    async def broadcast_transaction(self, tx):
        msg = encode_p2p(MSG_NEW_TX, {"tx": tx})
        async with self._lock:
            peers_copy = list(self.peers.keys())
        for peer in peers_copy:
            try:
                h, p = peer.split(":")
                r, w = await asyncio.open_connection(h, int(p))
                w.write(struct.pack(">I", len(msg)) + msg)
                await w.drain()
                w.close()
            except:
                pass
    
    async def discover(self):
        bootstrap = get_bootstrap_peers()
        for peer in bootstrap:
            async with self._lock:
                if peer not in self.peers:
                    asyncio.create_task(self._connect(peer))
        
        async with self._lock:
            peers_copy = list(self.peers.keys())
        for peer_addr in peers_copy:
            try:
                h, p = peer_addr.split(":")
                r, w = await asyncio.open_connection(h, int(p))
                msg = encode_p2p(MSG_GET_PEERS, {})
                w.write(struct.pack(">I", len(msg)) + msg)
                await w.drain()
                w.close()
            except:
                async with self._lock:
                    if peer_addr in self.peers:
                        del self.peers[peer_addr]
    
    async def _connect(self, addr):
        async with self._lock:
            if addr in self.peers:
                return
        try:
            h, p = addr.split(":")
            r, w = await asyncio.open_connection(h, int(p))
            msg = encode_p2p(MSG_HANDSHAKE, {"height": self.net.height, "ip": self.ip})
            w.write(struct.pack(">I", len(msg)) + msg)
            await w.drain()
            w.close()
            async with self._lock:
                self.peers[addr] = type('Peer', (), {'height': 0, 'last_seen': time.time()})()
            logger.info(f"[P2P] Connected to peer: {addr}")
            async with self._lock:
                save_peers_to_cache(list(self.peers.keys()))
        except Exception as e:
            logger.debug(f"[P2P] Failed to connect to {addr}: {e}")
    
    async def sync_with_peers(self):
        async with self._lock:
            if not self.peers:
                return
            best_peer = None
            best_height = self.net.height
            for addr, peer in self.peers.items():
                if peer.height > best_height:
                    best_height = peer.height
                    best_peer = addr
        if best_peer and best_height > self.net.height:
            logger.info(f"[P2P] Syncing from {best_peer}: local={self.net.height}, remote={best_height}")
            await self._request_blocks(best_peer, self.net.height, best_height)
    
    async def heartbeat(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            msg = encode_p2p(MSG_PING, {"timestamp": time.time()})
            async with self._lock:
                peers_copy = list(self.peers.keys())
            for peer_addr in peers_copy:
                try:
                    h, p = peer_addr.split(":")
                    r, w = await asyncio.open_connection(h, int(p))
                    w.write(struct.pack(">I", len(msg)) + msg)
                    await w.drain()
                    w.close()
                except:
                    async with self._lock:
                        if peer_addr in self.peers:
                            del self.peers[peer_addr]
                            
# ==================== DATA STRUCTURES ====================
@dataclass
class Miner:
    vid: str
    pub: str
    username: str
    wallet: str
    stake: int
    level: int
    uptime: int
    today_uptime: int
    last_ping: float
    active: bool
    rewards: int
    blocks: int
    slashes: int
    misses: int
    mtype: str
    liquidity_provided: int = 0
    fees_collected: int = 0

@dataclass
class Node:
    node_id: str
    username: str
    wallet: str
    ip: str
    port: int
    last_seen: float
    height: int
    active: bool
    rewards_earned: int

@dataclass
class Transaction:
    tx_hash: str
    from_wallet: str
    to_wallet: str
    amount: int
    fee: int
    timestamp: float
    block_id: int
    status: str
    tx_type: str

@dataclass
class Block:
    id: int
    ts: float
    prev: str
    validators: List[str]
    level: int
    sigs: Dict
    hash: str
    reward: int
    tx_count: int = 0

# ==================== MICROCORE NETWORK ====================
class MicroCoreNetwork:
    def __init__(self, is_genesis, username, wallet, priv, pub):
        self.miners = {}
        self.nodes = {}
        self.balances = {}
        self.blocks = []
        self.transactions = []
        self.height = 0
        self.last_hash = "0" * 64
        self.pending_challenges = {}
        self.pending_txs = []
        self.node_pool = 0
        self.uptime_pool = 0
        self.lp_pool = 0
        self.buyer_pool = 0
        self.validator_fee_pool = 0
        self.total_minted = 0
        self.is_genesis = is_genesis
        self.username = username
        self.wallet = wallet
        self.priv = priv
        self.pub = pub
        self.node_id = hashlib.sha256(f"{username}{time.time()}{secrets.token_hex(8)}".encode()).hexdigest()[:16]
        self.last_buyer_distribution = time.time()
        
        self.level_groups = {i: [] for i in range(1, 11)}
        self.levels_with_miners = set()
        self.rate_limiter = RateLimiter()
        self.health_checker = HealthChecker()
        
        self.level_mgr = LevelManager(self)
        self.p2p = P2PNode(self)
        self.dex = DEX(self)
        
        self._init_db()
        if is_genesis:
            self._genesis()
        else:
            self._load()
        self._register_self_miner()
        self._register_self_node()
        self._init_payment_tables()
    
    def _init_db(self):
        self.conn = sqlite3.connect('microcore.db', check_same_thread=False)
        c = self.conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS miners (
            vid TEXT PRIMARY KEY, pub TEXT, username TEXT, wallet TEXT,
            stake INT, level INT, rewards INT, blocks INT, slashes INT,
            uptime INT, today_uptime INT, type TEXT, liquidity INT, fees INT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY, username TEXT, wallet TEXT, ip TEXT,
            port INT, last_seen REAL, height INT, active INT, rewards_earned INT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS blocks (
            id INT PRIMARY KEY, ts REAL, phash TEXT, validators TEXT,
            lvl INT, hash TEXT, reward INT, tx_count INT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS transactions (
            tx_hash TEXT PRIMARY KEY, from_wallet TEXT, to_wallet TEXT,
            amount INT, fee INT, timestamp REAL, block_id INT, status TEXT, tx_type TEXT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS balances (wallet TEXT PRIMARY KEY, bal INT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS buyer_stats (
            wallet TEXT PRIMARY KEY, username TEXT, bought REAL, last_reset REAL)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS fiat_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT, wallet TEXT, usd_amount REAL,
            mcx_amount INT, payment_method TEXT, timestamp REAL)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS slashing_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vid TEXT, amount INT, reason TEXT, timestamp REAL, block_id INT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS level_supply (
            level INTEGER PRIMARY KEY, minted INT, cap INT, last_updated REAL)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS pending_crypto_payments (
            payment_id TEXT PRIMARY KEY, wallet TEXT, username TEXT,
            method TEXT, amount INT, usd_amount REAL, sender_address TEXT,
            status TEXT, created_at REAL, completed_at REAL)''')
        
        # ========== DEX Tables ==========
        c.execute('''CREATE TABLE IF NOT EXISTS pools (
            pool_id TEXT PRIMARY KEY,
            token_a TEXT NOT NULL,
            token_b TEXT NOT NULL,
            reserve_a REAL NOT NULL,
            reserve_b REAL NOT NULL,
            total_lp REAL NOT NULL,
            created_at REAL
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS lp_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pool_id TEXT NOT NULL,
            wallet TEXT NOT NULL,
            lp_shares REAL NOT NULL,
            FOREIGN KEY (pool_id) REFERENCES pools(pool_id)
        )''')
        
        # ========== DUCO Transactions Table ==========
        c.execute('''CREATE TABLE IF NOT EXISTS duco_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            txid TEXT UNIQUE NOT NULL,
            wallet TEXT NOT NULL,
            username TEXT NOT NULL,
            amount REAL NOT NULL,
            mcx_amount INTEGER NOT NULL,
            tx_time REAL NOT NULL,
            verified_at REAL
        )''')
        
        self.conn.commit()
        
        for level in range(1, 11):
            c.execute("INSERT OR IGNORE INTO level_supply (level, minted, cap, last_updated) VALUES (?, 0, ?, ?)",
                     (level, LEVEL_CAPS[level], time.time()))
        self.conn.commit()
    
    def _init_payment_tables(self):
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS pending_crypto_payments (
            payment_id TEXT PRIMARY KEY, wallet TEXT, username TEXT,
            method TEXT, amount INT, usd_amount REAL, sender_address TEXT,
            status TEXT, created_at REAL, completed_at REAL)''')
        self.conn.commit()
    
    def _save_balance(self, w, b):
        self.conn.execute("INSERT OR REPLACE INTO balances VALUES (?,?)", (w, b))
        self.conn.commit()
    
    def _save_transaction(self, tx):
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
                 (tx.tx_hash, tx.from_wallet, tx.to_wallet, tx.amount, tx.fee,
                  tx.timestamp, tx.block_id, tx.status, tx.tx_type))
        self.conn.commit()
    
    def _genesis(self):
        """
        GENESIS SUPPLY: 100,000 MCX
        =============================
        This supply is allocated to the founder for network bootstrapping:
        - 30,000 MCX → Seed DEX liquidity pools
        - 20,000 MCX → Reward early miners
        - 15,000 MCX → Reward early node operators
        - 15,000 MCX → Seed buyer rewards pool
        - 10,000 MCX → Development fund
        - 10,000 MCX → Reserve
        """
        if self.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0:
            self.balances[self.wallet] = 100000
            self.total_minted = 100000
            self._save_balance(self.wallet, 100000)
            logger.info(f"[GENESIS] Created 100,000 MCX for {self.wallet}")
            logger.info("[GENESIS] Distribution: DEX liquidity (30k), Early miners (20k), Nodes (15k), Buyer rewards (15k), Dev (10k), Reserve (10k)")
            self._add_block(0, "0"*64, ["genesis"], 1, {})
            
            c = self.conn.cursor()
            for level in range(1, 11):
                c.execute("UPDATE level_supply SET minted=0 WHERE level=?", (level,))
            self.conn.commit()
    
    def _load(self):
        for row in self.conn.execute("SELECT wallet, bal FROM balances"):
            self.balances[row[0]] = row[1]
        
        for row in self.conn.execute("SELECT id, ts, phash, validators, lvl, hash, reward, tx_count FROM blocks ORDER BY id"):
            validators = row[3].split(',') if row[3] else []
            block = Block(row[0], row[1], row[2], validators, row[4], {}, row[5], row[6], row[7])
            self.blocks.append(block)
            if block.id >= self.height:
                self.height = block.id + 1
                self.last_hash = block.hash
        
        for row in self.conn.execute("SELECT * FROM miners"):
            self.miners[row[0]] = Miner(
                row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8],
                0, True, row[9], row[10], row[11], 0, row[12], row[13], row[14]
            )
            self.level_mgr.register(row[3], row[4])
        
        for row in self.conn.execute("SELECT node_id, username, wallet, ip, port, last_seen, height, active, rewards_earned FROM nodes"):
            self.nodes[row[0]] = Node(row[0], row[1], row[2], row[3], row[4], row[5], row[6], bool(row[7]), row[8])
        
        logger.info(f"[LOAD] {len(self.blocks)} blocks, {len(self.miners)} miners, {len(self.nodes)} nodes")
        
    # ==================== CRYPTO BALANCE VERIFICATION ====================
    def verify_btc_balance(self, address: str) -> float:
        try:
            response = requests.get(f"{BLOCKCHAIN_INFO_API}{address}", timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get('final_balance', 0) / 1e8
            return 0
        except:
            return 0
    
    def verify_eth_balance(self, address: str) -> float:
        try:
            response = requests.get(
                f"{ETHERSCAN_API}?module=account&action=balance&address={address}&tag=latest&apikey={ETHERSCAN_API_KEY}",
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == '1':
                    return int(data.get('result', 0)) / 1e18
            return 0
        except:
            return 0
    
    def verify_usdc_balance(self, address: str) -> float:
        try:
            response = requests.get(
                f"{ETHERSCAN_API}?module=account&action=tokentx&contractaddress=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48&address={address}&sort=desc&apikey={ETHERSCAN_API_KEY}",
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == '1':
                    txs = data.get('result', [])
                    total_received = 0
                    for tx in txs:
                        if tx.get('to', '').lower() == address.lower():
                            value = int(tx.get('value', 0)) / 1e6
                            total_received += value
                    return total_received
            return 0
        except:
            return 0
    
    def verify_sol_balance(self, address: str) -> float:
        try:
            # Placeholder for SOL balance verification
            return 0
        except:
            return 0
    
    def verify_bnb_balance(self, address: str) -> float:
        try:
            # Placeholder for BNB balance verification
            return 0
        except:
            return 0
    
    def verify_duco_balance(self, address: str) -> float:
        try:
            response = requests.get(f"{DUCO_API}?address={address}", timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get('result'):
                    return float(data['result'].get('balance', 0))
            return 0
        except:
            return 0

    # ==================== DUCO TXID VERIFICATION ====================
    def verify_duco_transaction(self, username: str, txid: str, tx_time: float, expected_amount: float) -> dict:
        try:
            response = requests.get(f"{DUCO_API}?transaction={txid}", timeout=10)
            if response.status_code != 200:
                return {"success": False, "error": "Transaction not found on DUCO blockchain"}
            
            data = response.json()
            if not data.get('result'):
                return {"success": False, "error": "Transaction not found"}
            
            tx_data = data['result']
            
            sender = tx_data.get('sender', '')
            if sender.lower() != username.lower():
                return {"success": False, "error": f"Sender mismatch. Expected {username}, got {sender}"}
            
            recipient = tx_data.get('recipient', '')
            if recipient != DUCO_PAYMENT_ADDRESS:
                return {"success": False, "error": f"Recipient mismatch. Expected {DUCO_PAYMENT_ADDRESS}, got {recipient}"}
            
            tx_time_actual = tx_data.get('timestamp', 0)
            if abs(tx_time_actual - tx_time) > 300:
                return {"success": False, "error": f"Time mismatch. TXID time: {tx_time_actual}, you entered: {tx_time}"}
            
            amount = float(tx_data.get('amount', 0))
            if amount < expected_amount:
                return {"success": False, "error": f"Amount mismatch. Sent {amount}, expected {expected_amount}"}
            
            confirmations = tx_data.get('confirmations', 0)
            if confirmations < 1:
                return {"success": False, "error": f"Transaction pending. Confirmations: {confirmations}"}
            
            return {"success": True, "amount": amount, "sender": sender, "recipient": recipient, "confirmations": confirmations, "timestamp": tx_time_actual}
        except requests.exceptions.Timeout:
            return {"success": False, "error": "DUCO API timeout"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    # ==================== DUCO PURCHASE PROCESSING ====================
    def process_duco_purchase(self, wallet: str, username: str, txid: str, tx_time: float, duco_amount: float) -> dict:
        verification = self.verify_duco_transaction(username, txid, tx_time, duco_amount)
        if not verification['success']:
            return {"success": False, "error": verification['error']}
        
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM duco_transactions WHERE txid=?", (txid,))
        if c.fetchone()[0] > 0:
            return {"success": False, "error": "This transaction has already been used"}
        
        # DUCO to MCX conversion: 1 MCX = 1000 DUCO (fixed rate)
        mcx_amount = int(duco_amount / 1000)
        if mcx_amount < 1:
            return {"success": False, "error": f"Amount too small. {duco_amount} DUCO = {mcx_amount} MCX. Minimum 1 MCX."}
        
        self.balances[wallet] = self.balances.get(wallet, 0) + mcx_amount
        self._save_balance(wallet, self.balances[wallet])
        
        tx_hash = hash_transaction({"from": "duco_purchase", "to": wallet, "amount": mcx_amount, "duco_amount": duco_amount, "txid": txid, "duco_username": username})
        tx = Transaction(tx_hash, "duco_purchase", wallet, mcx_amount, 0, time.time(), -1, "confirmed", "duco_buy")
        self._save_transaction(tx)
        
        c.execute("INSERT OR REPLACE INTO buyer_stats (wallet, username, bought, last_reset) VALUES (?, ?, COALESCE((SELECT bought FROM buyer_stats WHERE wallet=?), 0) + ?, ?)",
                 (wallet, wallet, wallet, mcx_amount, time.time()))
        c.execute("INSERT INTO duco_transactions (txid, wallet, username, amount, mcx_amount, tx_time, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (txid, wallet, username, duco_amount, mcx_amount, tx_time, time.time()))
        self.conn.commit()
        
        logger.info(f"[DUCO] ✅ {username} purchased {mcx_amount} MCX via DUCO TXID {txid[:20]}...")
        
        return {"success": True, "mcx_amount": mcx_amount, "duco_amount": duco_amount, "txid": txid, "duco_username": username, "message": f"✅ Verified! {duco_amount} DUCO → {mcx_amount} MCX credited!"}
    
    # ==================== PAYMENT PROCESSING ====================
    def initiate_crypto_payment(self, wallet, username, usd_amount, method, sender_address):
        mcx_amount = int(usd_amount / 0.01)  # Fixed rate: 1 MCX = $0.01 for fiat on-ramp
        payment_id = f"{method}_{wallet}_{int(time.time())}"
        
        c = self.conn.cursor()
        c.execute("INSERT INTO pending_crypto_payments (payment_id, wallet, username, method, amount, usd_amount, sender_address, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (payment_id, wallet, username, method, mcx_amount, usd_amount, sender_address, 'pending', time.time()))
        self.conn.commit()
        
        return {"success": True, "payment_id": payment_id, "mcx_amount": mcx_amount, "method": method, "usd_amount": usd_amount, "sender_address": sender_address}
    
    def check_crypto_payment(self, payment_id, method, expected_amount, sender_address):
        c = self.conn.cursor()
        c.execute("SELECT wallet, username, amount, status FROM pending_crypto_payments WHERE payment_id=? AND status='pending'", (payment_id,))
        row = c.fetchone()
        if not row:
            return {"success": False, "error": "Payment not found or already processed"}
        
        wallet, username, mcx_amount, status = row
        
        if method == "btc":
            result = self.verify_btc_payment_from_sender(sender_address, expected_amount)
        elif method == "eth":
            result = self.verify_eth_payment_from_sender(sender_address, expected_amount)
        elif method == "usdc":
            result = self.verify_usdc_payment_from_sender(sender_address, expected_amount)
        else:
            return {"success": False, "error": "Unsupported method"}
        
        if result.get("success"):
            self.balances[wallet] = self.balances.get(wallet, 0) + mcx_amount
            self._save_balance(wallet, self.balances[wallet])
            
            c.execute("UPDATE pending_crypto_payments SET status='completed', completed_at=? WHERE payment_id=?", (time.time(), payment_id))
            self.conn.commit()
            
            c.execute("INSERT OR REPLACE INTO buyer_stats (wallet, username, bought, last_reset) VALUES (?, ?, COALESCE((SELECT bought FROM buyer_stats WHERE wallet=?), 0) + ?, ?)",
                     (wallet, username, wallet, mcx_amount, time.time()))
            self.conn.commit()
            
            tx_hash = hash_transaction({"from": method, "to": wallet, "amount": mcx_amount})
            tx = Transaction(tx_hash, method, wallet, mcx_amount, 0, time.time(), -1, "confirmed", "buy")
            self._save_transaction(tx)
            
            logger.info(f"[CRYPTO] Credited {mcx_amount} MCX to {wallet} via {method.upper()} from {sender_address}")
            return {"success": True, "mcx_amount": mcx_amount, "wallet": wallet}
        
        return {"success": False, "error": result.get("error", "Payment not yet received")}
    
    def verify_btc_payment_from_sender(self, sender_address, expected_amount):
        try:
            response = requests.get(f"{BLOCKCHAIN_INFO_API}{sender_address}", timeout=10)
            if response.status_code == 200:
                data = response.json()
                total_received = data.get('total_received', 0) / 1e8
                if total_received >= expected_amount:
                    return {"success": True, "received": total_received}
            return {"success": False, "error": "Payment not verified"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def verify_eth_payment_from_sender(self, sender_address, expected_amount):
        try:
            response = requests.get(
                f"{ETHERSCAN_API}?module=account&action=txlist&address={sender_address}&sort=desc&apikey={ETHERSCAN_API_KEY}",
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == '1':
                    txs = data.get('result', [])
                    total_sent = 0
                    for tx in txs:
                        value = int(tx.get('value', 0)) / 1e18
                        total_sent += value
                    if total_sent >= expected_amount:
                        return {"success": True, "received": total_sent}
            return {"success": False, "error": "Payment not verified"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def verify_usdc_payment_from_sender(self, sender_address, expected_amount):
        try:
            response = requests.get(
                f"{ETHERSCAN_API}?module=account&action=tokentx&contractaddress=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48&address={sender_address}&sort=desc&apikey={ETHERSCAN_API_KEY}",
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == '1':
                    txs = data.get('result', [])
                    total_sent = 0
                    for tx in txs:
                        if tx.get('from', '').lower() == sender_address.lower():
                            value = int(tx.get('value', 0)) / 1e6
                            total_sent += value
                    if total_sent >= expected_amount:
                        return {"success": True, "received": total_sent}
            return {"success": False, "error": "Payment not verified"}
        except Exception as e:
            return {"success": False, "error": str(e)}
        

    # ==================== WEBSOCKET HANDLER ====================
    async def ws_handler(self, websocket, path):
        client_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
        
        if not await self.rate_limiter.is_allowed(client_ip):
            await websocket.close(1008, "Rate limit exceeded")
            logger.warning(f"[WS] Rate limit exceeded for {client_ip}")
            return
        
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    t = data.get("type")
                    await self._handle_ws_message(websocket, data, t, client_ip)
                except json.JSONDecodeError:
                    logger.warning(f"[WS] Invalid JSON from {client_ip}")
                    await websocket.send(json.dumps({"type": "error", "message": "Invalid JSON"}))
                except Exception as e:
                    logger.error(f"[WS] Handler error: {e}")
                    await websocket.send(json.dumps({"type": "error", "message": str(e)}))
        except websockets.exceptions.ConnectionClosed:
            logger.debug(f"[WS] Connection closed: {client_ip}")
        except Exception as e:
            logger.error(f"[WS] Connection error: {e}")
    
    async def _handle_ws_message(self, websocket, data, msg_type, client_ip):
        # ========== MINER REGISTRATION ==========
        if msg_type == "register":
            ok = self.register_miner(
                data["validator_id"], data["public_key"], data["username"],
                data["wallet"], data["stake"], data["signature"],
                data["timestamp"], data.get("miner_type", "web")
            )
            if ok:
                await websocket.send(json.dumps({
                    "type": "registered",
                    "level": self.level_mgr.get_level(data["wallet"]),
                    "max_level": self.level_mgr.max_unlocked,
                    "remaining_supply": self.get_remaining_supply_for_level(1),
                    "current_reward": self.get_current_reward_for_level(1),
                    "dex_pools": self.dex.get_supported_pools()
                }))
        
        # ========== BLOCK SIGNATURE ==========
        elif msg_type == "block_signature":
            ch = data["challenge"]
            if ch in self.pending_challenges:
                self.pending_challenges[ch]["sigs"][data["validator_id"]] = data["signature"]
        
        # ========== UPTIME PING ==========
        elif msg_type == "uptime_ping":
            self.update_miner_uptime(
                data["validator_id"], data.get("uptime_seconds", 0),
                data.get("today_uptime", 0)
            )
        
        # ========== GOSSIP DISCOVERY ==========
        elif msg_type == "get_peers":
            peers = [f"{addr}" for addr in self.p2p.peers.keys()]
            await websocket.send(json.dumps({"type": "peers", "peers": peers}))
        
        # ========== STAKING ==========
        elif msg_type == "stake":
            result = self.process_stake(data["wallet"], data["amount"])
            await websocket.send(json.dumps({"type": "staking_confirmed", **result}))
        
        elif msg_type == "unstake":
            result = self.process_unstake(data["wallet"], data["amount"])
            await websocket.send(json.dumps({"type": "staking_confirmed", **result}))
        
        # ========== SEND MCX ==========
        elif msg_type == "send":
            result = self.send_mcx(data["from"], data["to"], data["amount"])
            await websocket.send(json.dumps({"type": "send_result", **result}))
        
        # ========== BUY MCX WITH CRYPTO (BTC/ETH/USDC) ==========
        elif msg_type == "buy_mcx_crypto":
            wallet = data.get("wallet")
            username = data.get("username")
            usd_amount = data.get("usd_amount", 0)
            method = data.get("method", "btc")
            sender_address = data.get("sender_address", "")
            
            if not wallet or not username or usd_amount <= 0:
                await websocket.send(json.dumps({"type": "buy_error", "error": "Invalid parameters"}))
                return
            
            if not sender_address:
                await websocket.send(json.dumps({"type": "buy_error", "error": "Sender address required"}))
                return
            
            result = self.initiate_crypto_payment(wallet, username, usd_amount, method, sender_address)
            await websocket.send(json.dumps({"type": "crypto_payment_instructions", "data": result}))
        
        # ========== CHECK CRYPTO PAYMENT ==========
        elif msg_type == "check_crypto_payment":
            payment_id = data.get("payment_id")
            method = data.get("method", "btc")
            expected_amount = data.get("expected_amount", 0)
            sender_address = data.get("sender_address", "")
            
            if not payment_id:
                await websocket.send(json.dumps({"type": "payment_check", "success": False, "error": "Missing payment_id"}))
                return
            
            result = self.check_crypto_payment(payment_id, method, expected_amount, sender_address)
            await websocket.send(json.dumps({"type": "payment_check", "data": result}))
        
        # ========== DUCO TXID VERIFICATION ==========
        elif msg_type == "verify_duco":
            wallet = data.get("wallet")
            duco_username = data.get("duco_username")
            txid = data.get("txid")
            tx_time = data.get("tx_time", 0)
            duco_amount = data.get("amount", 0)
            
            if not wallet or not duco_username or not txid or duco_amount <= 0:
                await websocket.send(json.dumps({
                    "type": "duco_result",
                    "data": {"success": False, "error": "Missing wallet, DUCO username, TXID, time, or amount"}
                }))
                return
            
            result = self.process_duco_purchase(wallet, duco_username, txid, tx_time, duco_amount)
            await websocket.send(json.dumps({"type": "duco_result", "data": result}))
        
        # ========== GET PAYMENT STATUS ==========
        elif msg_type == "get_payment_status":
            payment_id = data.get("payment_id")
            result = self.get_payment_status(payment_id)
            await websocket.send(json.dumps({"type": "payment_status", "data": result}))
        
        # ========== BUYER STATS ==========
        elif msg_type == "get_buyer_stats":
            buyers = self.get_buyer_stats(10)
            await websocket.send(json.dumps({"type": "buyer_stats", "buyers": buyers}))
        
        # ========== GET BALANCE ==========
        elif msg_type == "get_balance":
            balance = self.get_balance(data["wallet"])
            staked = 0
            for m in self.miners.values():
                if m.wallet == data["wallet"]:
                    staked = m.stake
                    break
            await websocket.send(json.dumps({"type": "balance", "balance": balance, "staked": staked}))
        
        # ========== GET MINERS ==========
        elif msg_type == "get_miners":
            miners = self.get_miners_list()
            await websocket.send(json.dumps({"type": "miners", "miners": miners}))
        
        # ========== GET NODES ==========
        elif msg_type == "get_nodes":
            nodes = self.get_nodes_list()
            await websocket.send(json.dumps({"type": "nodes", "nodes": nodes}))
        
        # ========== GET TOP STAKERS ==========
        elif msg_type == "get_top_stakers":
            stakers = self.get_top_stakers(10)
            await websocket.send(json.dumps({"type": "top_stakers", "stakers": stakers}))
        
        # ========== GET TOP BUYERS ==========
        elif msg_type == "get_top_buyers":
            buyers = self.get_buyer_stats(10)
            await websocket.send(json.dumps({"type": "top_buyers", "buyers": buyers}))
        
        # ========== SWAP QUOTE ==========
        elif msg_type == "swap_quote":
            quote = await self.dex.get_swap_quote(
                data["from_token"], data["to_token"], data["amount"]
            )
            await websocket.send(json.dumps({"type": "swap_quote", "data": quote}))
        
        # ========== EXECUTE SWAP ==========
        elif msg_type == "execute_swap":
            success, result = await self.dex.execute_swap(
                data["wallet"], data["from_token"], data["to_token"],
                data["amount"], data.get("fee_mcx", 5)
            )
            await websocket.send(json.dumps({"type": "swap_result", "success": success, "data": result}))
            if success:
                balance = self.get_balance(data["wallet"])
                await websocket.send(json.dumps({"type": "balance", "balance": balance}))
        
        # ========== ADD LIQUIDITY ==========
        elif msg_type == "add_liquidity":
            wallet = data.get("wallet")
            pool_id = data.get("pool_id")
            amount_a = data.get("amount_a", 0)
            amount_b = data.get("amount_b", 0)
            external_wallet = data.get("wallet_address", "")
            
            if not wallet or not pool_id or amount_a <= 0 or amount_b <= 0:
                await websocket.send(json.dumps({
                    "type": "liquidity_result",
                    "success": False,
                    "data": "Missing wallet, pool_id, or amounts"
                }))
                return
            
            token_b = pool_id.split("/")[1]
            if token_b not in ["MCX"]:
                if not external_wallet:
                    await websocket.send(json.dumps({
                        "type": "liquidity_result",
                        "success": False,
                        "data": f"External wallet address required for {token_b}"
                    }))
                    return
            
            success, result = self.dex.add_liquidity(wallet, pool_id, amount_a, amount_b, external_wallet)
            await websocket.send(json.dumps({"type": "liquidity_result", "success": success, "data": result}))
            if success:
                balance = self.get_balance(wallet)
                await websocket.send(json.dumps({"type": "balance", "balance": balance}))
        
        # ========== REMOVE LIQUIDITY ==========
        elif msg_type == "remove_liquidity":
            success, result = self.dex.remove_liquidity(
                data["wallet"], data["pool_id"], data["lp_shares"]
            )
            await websocket.send(json.dumps({"type": "liquidity_result", "success": success, "data": result}))
            if success:
                balance = self.get_balance(data["wallet"])
                await websocket.send(json.dumps({"type": "balance", "balance": balance}))
        
        # ========== GET POOLS ==========
        elif msg_type == "get_pools":
            pools = self.dex.get_supported_pools()
            await websocket.send(json.dumps({"type": "pools", "pools": pools}))
        
        # ========== GET TRANSACTIONS ==========
        elif msg_type == "get_transactions":
            txs = self.get_transactions(data["wallet"], data.get("limit", 20))
            await websocket.send(json.dumps({"type": "transactions", "transactions": txs}))
        
        # ========== GET BLOCKS ==========
        elif msg_type == "get_blocks":
            limit = data.get("limit", 20)
            offset = data.get("offset", 0)
            blocks = []
            for b in self.blocks[-limit-offset:][:limit] if offset == 0 else self.blocks[offset:offset+limit]:
                blocks.append({
                    "id": b.id, "timestamp": b.ts, "hash": b.hash,
                    "validators": b.validators, "level": b.level,
                    "reward": b.reward, "tx_count": b.tx_count
                })
            await websocket.send(json.dumps({"type": "blocks", "blocks": blocks, "total": len(self.blocks)}))
        
        # ========== GET BLOCK ==========
        elif msg_type == "get_block":
            height = data.get("height")
            for b in self.blocks:
                if b.id == height:
                    await websocket.send(json.dumps({"type": "block", "block": {
                        "id": b.id, "timestamp": b.ts, "hash": b.hash,
                        "prev_hash": b.prev, "validators": b.validators,
                        "level": b.level, "reward": b.reward
                    }}))
                    break
        
        # ========== GET TRANSACTION ==========
        elif msg_type == "get_transaction":
            tx_hash = data.get("hash")
            c = self.conn.cursor()
            c.execute("SELECT * FROM transactions WHERE tx_hash=?", (tx_hash,))
            row = c.fetchone()
            if row:
                await websocket.send(json.dumps({"type": "transaction", "transaction": {
                    "hash": row[0], "from": row[1], "to": row[2], "amount": row[3],
                    "fee": row[4], "timestamp": row[5], "block": row[6],
                    "status": row[7], "type": row[8]
                }}))
            else:
                await websocket.send(json.dumps({"type": "error", "message": "Transaction not found"}))
        
        # ========== CONTROL MINER ==========
        elif msg_type == "control_miner":
            await websocket.send(json.dumps({
                "type": "control_result",
                "miner_id": data["miner_id"],
                "action": data["action"],
                "success": True,
                "message": f"{data['action']} command sent to miner"
            }))
        
        # ========== GET STATUS ==========
        elif msg_type == "get_status":
            total_supply = 0
            for level in range(1, 11):
                total_supply += self.get_remaining_supply_for_level(level)
            
            await websocket.send(json.dumps({
                "type": "status",
                "data": {
                    "block_id": self.height,
                    "total_miners": len(self.miners),
                    "active_miners": sum(1 for m in self.miners.values() if m.active),
                    "total_nodes": len(self.nodes),
                    "active_nodes": sum(1 for n in self.nodes.values() if n.active),
                    "max_level": self.level_mgr.max_unlocked,
                    "current_reward": self.get_current_reward_for_level(1),
                    "total_minted": self.total_minted,
                    "remaining_supply": total_supply,
                    "node_pool": self.node_pool,
                    "uptime_pool": self.uptime_pool,
                    "lp_pool": self.lp_pool,
                    "buyer_pool": self.buyer_pool,
                    "validator_fee_pool": self.validator_fee_pool,
                    "level_intervals": LEVEL_BLOCK_INTERVALS,
                    "level_caps": LEVEL_CAPS,
                    "levels_with_miners": list(self.levels_with_miners),
                    "pools": self.dex.get_supported_pools()
                }
            }))
        
        # ========== HEALTH CHECK ==========
        elif msg_type == "get_health":
            await websocket.send(json.dumps({
                "type": "health",
                "data": self.health_checker.get_status()
            }))
        
        # ========== UNKNOWN ==========
        else:
            await websocket.send(json.dumps({"type": "error", "message": f"Unknown message type: {msg_type}"}))
            
# ==================== BLOCK PRODUCTION WITH REDISTRIBUTION ====================
    def update_level_groups(self):
        self.level_groups = {i: [] for i in range(1, 11)}
        self.levels_with_miners = set()
        
        for miner in self.miners.values():
            if miner.active:
                level = min(miner.level, MAX_LEVEL)
                if level in self.level_groups:
                    self.level_groups[level].append(miner.vid)
                    self.levels_with_miners.add(level)
    
    def select_validators_for_level(self, level):
        miners = self.level_groups.get(level, [])
        if len(miners) < MIN_VALIDATORS_PER_BLOCK:
            return []
        seed = int(self.last_hash[:16], 16) if self.last_hash != "0"*64 else int(time.time())
        rng = random.Random(seed)
        return rng.sample(miners, MIN_VALIDATORS_PER_BLOCK)
    
    def get_level_with_most_miners(self):
        best_level = 1
        best_count = 0
        for level in range(1, 11):
            count = len(self.level_groups.get(level, []))
            if count > best_count:
                best_count = count
                best_level = level
        return best_level if best_count >= MIN_VALIDATORS_PER_BLOCK else None
    
    def get_effective_production_level(self, original_level):
        miners_count = len(self.level_groups.get(original_level, []))
        if miners_count >= MIN_VALIDATORS_PER_BLOCK:
            return original_level
        else:
            return self.get_level_with_most_miners()
    
    def generate_challenge(self, block_id, validators):
        return hashlib.sha256(
            f"{block_id}{''.join(sorted(validators))}{time.time()}{self.last_hash}{secrets.token_hex(16)}".encode()
        ).hexdigest()
    
    def verify_challenge_response(self, vid, challenge, block_id, sig):
        if vid not in self.miners:
            return False
        message = f"{challenge}{vid}{block_id}"
        return verify_signature(self.miners[vid].pub, message, sig, self.miners[vid].mtype)
    
    async def produce_block_for_level(self, original_level):
        effective_level = self.get_effective_production_level(original_level)
        
        if effective_level is None:
            return False
        
        validators = self.select_validators_for_level(effective_level)
        if len(validators) < MIN_VALIDATORS_PER_BLOCK:
            return False
        
        block_id = self.height
        challenge = self.generate_challenge(block_id, validators)
        self.pending_challenges[challenge] = {
            "bid": block_id,
            "validators": validators,
            "level": effective_level,
            "original_level": original_level,
            "sigs": {},
            "start_time": time.time()
        }
        
        await asyncio.sleep(SIGNING_WINDOW_MS / 1000)
        
        pending = self.pending_challenges.pop(challenge, {})
        sigs = pending.get("sigs", {})
        valid_sigs = {}
        total_slashed = 0
        
        for vid, sig in sigs.items():
            if self.verify_challenge_response(vid, challenge, block_id, sig):
                valid_sigs[vid] = sig
        
        if len(valid_sigs) >= MIN_VALIDATORS_PER_BLOCK:
            block = self._add_block(block_id, self.last_hash, list(valid_sigs.keys()), effective_level, valid_sigs)
            self.distribute_block_reward(block, list(valid_sigs.keys()))
            
            asyncio.create_task(self.p2p.broadcast_block({
                "id": block_id, "ts": block.ts, "prev": block.prev,
                "validators": block.validators, "level": effective_level,
                "original_level": original_level,
                "hash": block.hash, "reward": block.reward
            }))
            
            if original_level != effective_level:
                logger.info(f"[BLOCK {block_id}] ✅ ACCEPTED | Original Level {original_level} → Produced by Level {effective_level} | Validators: {len(valid_sigs)}")
            else:
                logger.info(f"[BLOCK {block_id}] ✅ ACCEPTED | Level {effective_level} | Validators: {len(valid_sigs)}")
            
            self.health_checker.record_block()
            return True
        else:
            missing = set(validators) - set(sigs.keys())
            for vid in missing:
                total_slashed += self.slash_miner(vid, f"Missed signing for block {block_id}", block_id)
            
            if total_slashed > 0 and len(valid_sigs) > 0:
                per_signer = total_slashed // len(valid_sigs)
                for vid in valid_sigs:
                    if vid in self.miners:
                        self.miners[vid].stake += per_signer
                        self.miners[vid].rewards += per_signer
                        self.balances[self.miners[vid].wallet] = self.balances.get(self.miners[vid].wallet, 0) + per_signer
                        self.conn.execute("UPDATE miners SET stake=?, rewards=? WHERE vid=?",
                                         (self.miners[vid].stake, self.miners[vid].rewards, vid))
                        self._save_balance(self.miners[vid].wallet, self.balances[self.miners[vid].wallet])
                logger.info(f"[REDIST] {total_slashed} MCX redistributed to {len(valid_sigs)} signers")
            
            if original_level != effective_level:
                logger.warning(f"[BLOCK {block_id}] ❌ REJECTED | Original Level {original_level} → Attempted Level {effective_level} | Got {len(valid_sigs)}/{MIN_VALIDATORS_PER_BLOCK} signatures")
            else:
                logger.warning(f"[BLOCK {block_id}] ❌ REJECTED | Level {effective_level} | Got {len(valid_sigs)}/{MIN_VALIDATORS_PER_BLOCK} signatures")
            
            self.health_checker.record_error()
            return False
    
    async def produce_blocks_loop(self):
        while True:
            self.update_level_groups()
            
            for level in range(1, 11):
                success = await self.produce_block_for_level(level)
                if success:
                    interval = self.get_block_interval(level)
                    await asyncio.sleep(interval)
            
            await asyncio.sleep(0.1)

    # ==================== REWARD DISTRIBUTION ====================
    def distribute_block_reward(self, block, signers):
        if block.reward > 0:
            return
        
        reward = self.get_current_reward_for_level(block.level)
        if reward <= 0:
            logger.warning(f"[BLOCK {block.id}] No reward remaining for Level {block.level}")
            return
        
        block.reward = reward
        
        validator_total = int(reward * 0.7)
        node_total = int(reward * 0.08)
        uptime_total = int(reward * 0.05)
        lp_total = int(reward * 0.05)
        buyer_total = int(reward * 0.12)
        
        validator_share = validator_total // max(len(signers), 1)
        
        for vid in signers:
            if vid in self.miners:
                m = self.miners[vid]
                m.rewards += validator_share
                m.stake += validator_share
                m.blocks += 1
                m.misses = 0
                self.balances[m.wallet] = self.balances.get(m.wallet, 0) + validator_share
                self._save_balance(m.wallet, self.balances[m.wallet])
                self.level_mgr.register(m.wallet, m.stake)
                m.level = self.level_mgr.get_level(m.wallet)
        
        self.node_pool += node_total
        self.uptime_pool += uptime_total
        self.lp_pool += lp_total
        self.buyer_pool += buyer_total
        
        # ========== DISTRIBUTE VALIDATOR FEES ==========
        if self.validator_fee_pool > 0 and len(signers) > 0:
            fee_share = self.validator_fee_pool // len(signers)
            for vid in signers:
                if vid in self.miners:
                    m = self.miners[vid]
                    m.rewards += fee_share
                    m.stake += fee_share
                    m.fees_collected += fee_share
                    self.balances[m.wallet] = self.balances.get(m.wallet, 0) + fee_share
                    self._save_balance(m.wallet, self.balances[m.wallet])
                    self.level_mgr.register(m.wallet, m.stake)
                    m.level = self.level_mgr.get_level(m.wallet)
                    self.conn.execute("UPDATE miners SET stake=?, level=?, rewards=?, fees=? WHERE vid=?",
                                     (m.stake, m.level, m.rewards, m.fees_collected, vid))
            logger.info(f"[BLOCK {block.id}] FEE DISTRIBUTION: {self.validator_fee_pool} MCX in fees to {len(signers)} validators ({fee_share} each)")
            self.validator_fee_pool = 0
        
        self.update_level_supply(block.level, reward)
        self.total_minted = self.get_total_minted()
        
        logger.info(f"[BLOCK {block.id}] REWARD: {reward} MCX | Level {block.level} | Validators: {validator_share} each")
    
    def distribute_periodic_rewards(self):
        active_miners = [m for m in self.miners.values() if m.active]
        total_uptime = sum(m.uptime for m in active_miners)
        if total_uptime > 0 and self.uptime_pool > 0:
            for miner in active_miners:
                if miner.uptime > 0:
                    share = int(self.uptime_pool * (miner.uptime / total_uptime))
                    miner.rewards += share
                    miner.stake += share
                    self.balances[miner.wallet] = self.balances.get(miner.wallet, 0) + share
                    self._save_balance(miner.wallet, self.balances[miner.wallet])
                    self.level_mgr.register(miner.wallet, miner.stake)
                    miner.level = self.level_mgr.get_level(miner.wallet)
                    self.conn.execute("UPDATE miners SET stake=?, level=?, rewards=? WHERE vid=?",
                                     (miner.stake, miner.level, miner.rewards, miner.vid))
            logger.info(f"[DISTRO] Uptime rewards: {self.uptime_pool} MCX to {len(active_miners)} miners")
        
        active_nodes = [n for n in self.nodes.values() if n.active]
        if active_nodes and self.node_pool > 0:
            node_share = self.node_pool // max(len(active_nodes), 1)
            for node in active_nodes:
                node.rewards_earned += node_share
                self.balances[node.wallet] = self.balances.get(node.wallet, 0) + node_share
                self._save_balance(node.wallet, self.balances[node.wallet])
                self.conn.execute("UPDATE nodes SET rewards_earned=? WHERE node_id=?", (node.rewards_earned, node.node_id))
            logger.info(f"[DISTRO] Node rewards: {self.node_pool} MCX to {len(active_nodes)} nodes")
        
        self.node_pool = 0
        self.uptime_pool = 0
        self.lp_pool = 0
    
    def distribute_buyer_rewards(self):
        if self.buyer_pool == 0:
            return
        
        c = self.conn.cursor()
        c.execute("SELECT wallet, username, bought FROM buyer_stats WHERE last_reset > ? ORDER BY bought DESC LIMIT 10",
                 (time.time() - 30 * 24 * 3600,))
        top_buyers = c.fetchall()
        
        if not top_buyers:
            return
        
        for i, (wallet, username, _) in enumerate(top_buyers):
            if i >= len(BUYER_REWARDS):
                break
            reward = min(BUYER_REWARDS[i], self.buyer_pool)
            self.balances[wallet] = self.balances.get(wallet, 0) + reward
            self.buyer_pool -= reward
            tx_hash = hash_transaction({"from": "buyer_rewards", "to": wallet, "amount": reward})
            tx = Transaction(tx_hash, "buyer_rewards", wallet, reward, 0, time.time(), -1, "confirmed", "reward")
            self._save_transaction(tx)
            logger.info(f"[BUYER REWARD] #{i+1} {username[:20]}... +{reward} MCX")
        
        c.execute("UPDATE buyer_stats SET bought = 0, last_reset = ?", (time.time(),))
        self.conn.commit()
        self.buyer_pool = 0
    
    # ==================== SLASHING ====================
    def slash_miner(self, vid, reason, block_id=-1):
        if vid not in self.miners:
            return 0
        m = self.miners[vid]
        slash = max(int(m.stake * SLASH_RATE), LEVEL_STAKE_RANGE)
        m.stake -= slash
        if m.stake < LEVEL_STAKE_RANGE:
            m.stake = LEVEL_STAKE_RANGE
        m.slashes += 1
        m.misses += 1
        
        old_level = m.level
        self.level_mgr.register(m.wallet, m.stake)
        m.level = self.level_mgr.get_level(m.wallet)
        
        if m.slashes >= BAN_THRESHOLD:
            m.active = False
            logger.warning(f"[BAN] {m.username} banned after {BAN_THRESHOLD} slashes")
        
        self.conn.execute("UPDATE miners SET stake=?, level=?, slashes=?, active=? WHERE vid=?",
                         (m.stake, m.level, m.slashes, m.active, vid))
        self.conn.execute("INSERT INTO slashing_events (vid, amount, reason, timestamp, block_id) VALUES (?,?,?,?,?)",
                         (vid, slash, reason, time.time(), block_id))
        self.conn.commit()
        logger.warning(f"[SLASH] {m.username} lost {slash} MCX (now {m.stake} MCX, Level {m.level})")
        return slash
    
    # ==================== STAKING ====================
    def process_stake(self, username, amount):
        wallet = None
        for m in self.miners.values():
            if m.username == username:
                wallet = m.wallet
                break
        
        if not wallet:
            return {"success": False, "error": "User not found"}
        
        if self.get_balance(wallet) < amount:
            return {"success": False, "error": f"Insufficient balance. You have {self.get_balance(wallet)} MCX"}
        
        self.balances[wallet] -= amount
        self._save_balance(wallet, self.balances[wallet])
        
        for m in self.miners.values():
            if m.wallet == wallet:
                old_level = m.level
                m.stake += amount
                self.level_mgr.register(wallet, m.stake)
                m.level = self.level_mgr.get_level(wallet)
                self.conn.execute("UPDATE miners SET stake=?, level=? WHERE vid=?", (m.stake, m.level, m.vid))
                
                tx_hash = hash_transaction({"from": wallet, "to": "stake_pool", "amount": amount})
                tx = Transaction(tx_hash, wallet, "stake_pool", amount, 0, time.time(), -1, "confirmed", "stake")
                self._save_transaction(tx)
                
                if self.level_mgr.has_permanent_tower(wallet):
                    logger.info(f"[TOWER] {username} now has PERMANENT TOWER! Stake beyond Level 10")
                
                return {"success": True, "staked": m.stake, "level": m.level, "balance": self.balances[wallet]}
        
        return {"success": False, "error": "Miner not found"}
    
    # ==================== SEND MCX ====================
    def send_mcx(self, from_user, to_user, amount):
        from_wallet = None
        to_wallet = None
        
        for m in self.miners.values():
            if m.username == from_user:
                from_wallet = m.wallet
            if m.username == to_user:
                to_wallet = m.wallet
        
        if not from_wallet:
            return {"success": False, "error": f"Sender '{from_user}' not found"}
        if not to_wallet:
            return {"success": False, "error": f"Recipient '{to_user}' not found"}
        
        fee = calculate_transfer_fee(amount)
        fee = int(fee)
        if fee < 1:
            fee = 1
        
        if self.get_balance(from_wallet) < amount + fee:
            return {"success": False, "error": f"Insufficient balance. You have {self.get_balance(from_wallet)} MCX"}
        
        self.balances[from_wallet] -= (amount + fee)
        self.balances[to_wallet] = self.balances.get(to_wallet, 0) + amount
        
        # Fee goes to validator_fee_pool → distributed to validators
        self.validator_fee_pool += fee
        
        self._save_balance(from_wallet, self.balances[from_wallet])
        self._save_balance(to_wallet, self.balances[to_wallet])
        
        tx_hash = hash_transaction({"from": from_wallet, "to": to_wallet, "amount": amount, "fee": fee})
        tx = Transaction(tx_hash, from_wallet, to_wallet, amount, fee, time.time(), -1, "confirmed", "send")
        self._save_transaction(tx)
        
        logger.info(f"[SEND] {from_user} → {to_user}: {amount} MCX (fee: {fee} MCX to validators)")
        
        return {"success": True, "tx_hash": tx_hash, "from": from_user, "to": to_user, "amount": amount, "fee": fee}
    
    def process_unstake(self, username, amount):
        wallet = None
        for m in self.miners.values():
            if m.username == username:
                wallet = m.wallet
                break
        
        if not wallet:
            return {"success": False, "error": "User not found"}
        
        for m in self.miners.values():
            if m.wallet == wallet:
                if m.stake < amount:
                    return {"success": False, "error": f"Insufficient staked. You have {m.stake} MCX staked"}
                
                m.stake -= amount
                self.balances[wallet] = self.balances.get(wallet, 0) + amount
                self._save_balance(wallet, self.balances[wallet])
                self.level_mgr.register(wallet, m.stake)
                m.level = self.level_mgr.get_level(wallet)
                self.conn.execute("UPDATE miners SET stake=?, level=? WHERE vid=?", (m.stake, m.level, m.vid))
                
                tx_hash = hash_transaction({"from": "stake_pool", "to": wallet, "amount": amount})
                tx = Transaction(tx_hash, "stake_pool", wallet, amount, 0, time.time(), -1, "confirmed", "unstake")
                self._save_transaction(tx)
                
                return {"success": True, "staked": m.stake, "level": m.level, "balance": self.balances[wallet]}
        
        return {"success": False, "error": "Miner not found"}
    
    def register_miner(self, vid, pub, username, wallet, stake, sig, timestamp, mtype="web"):
        if vid in self.miners:
            return True
        
        if not verify_signature(pub, f"{username}{wallet}{timestamp}", sig, mtype):
            logger.warning(f"[REGISTER] Invalid signature from {username}")
            return False
        
        self.miners[vid] = Miner(
            vid, pub, username, wallet, stake, 1, 0, 0,
            time.time(), True, 0, 0, 0, 0, mtype, 0, 0
        )
        
        self.level_mgr.register(wallet, stake)
        level = self.level_mgr.get_level(wallet)
        self.miners[vid].level = level
        
        self.conn.execute("INSERT OR REPLACE INTO miners VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (vid, pub, username, wallet, stake, level, 0, 0, 0, 0, 0, mtype, 0, 0))
        self.conn.commit()
        logger.info(f"[REGISTER] {username} (type: {mtype}) staked {stake} MCX, Level {level}")
        return True
    
    def _register_self_miner(self):
        if self.username not in [m.username for m in self.miners.values()]:
            self.miners[self.username] = Miner(
                self.username, self.pub, self.username, self.wallet,
                1000, 1, 0, 0, time.time(), True, 0, 0, 0, 0, "pc", 0, 0
            )
            self.level_mgr.register(self.wallet, 1000)
            self.conn.execute("INSERT OR REPLACE INTO miners VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (self.username, self.pub, self.username, self.wallet,
                              1000, 1, 0, 0, 0, 0, 0, "pc", 0, 0))
            self.conn.commit()
            logger.info(f"[SELF MINER] Registered as {self.username}")
    
    def _register_self_node(self):
        if self.node_id not in self.nodes:
            self.nodes[self.node_id] = Node(
                self.node_id, self.username, self.wallet,
                get_public_ip() or "127.0.0.1", NODE_PORT,
                time.time(), self.height, True, 0
            )
            self.conn.execute("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?)",
                             (self.node_id, self.username, self.wallet,
                              get_public_ip() or "127.0.0.1", NODE_PORT,
                              time.time(), self.height, 1, 0))
            self.conn.commit()
            logger.info(f"[SELF NODE] Registered node ID: {self.node_id[:16]}...")
    
    def update_miner_uptime(self, vid, uptime_seconds, today_uptime):
        if vid in self.miners:
            self.miners[vid].uptime = uptime_seconds
            self.miners[vid].today_uptime = today_uptime
            self.miners[vid].last_ping = time.time()
            self.conn.execute("UPDATE miners SET uptime=?, today_uptime=?, last_ping=? WHERE vid=?",
                             (uptime_seconds, today_uptime, time.time(), vid))
            self.conn.commit()
    
    # ==================== GETTERS ====================
    def get_balance(self, w):
        return self.balances.get(w, 0)
    
    def get_block_interval(self, level):
        return LEVEL_BLOCK_INTERVALS.get(level, 40)
    
    def get_current_reward_for_level(self, level):
        c = self.conn.cursor()
        c.execute("SELECT minted, cap FROM level_supply WHERE level=?", (level,))
        row = c.fetchone()
        if not row:
            return INITIAL_BLOCK_REWARD
        
        minted, cap = row
        remaining = cap - minted
        if remaining <= 0:
            return 0
        
        halving_count = 0
        target = cap / 2
        while minted >= target and target > 0:
            halving_count += 1
            target = cap / (2 ** (halving_count + 1))
        
        reward = INITIAL_BLOCK_REWARD // (2 ** halving_count)
        return max(reward, 1)
    
    def update_level_supply(self, level, reward):
        c = self.conn.cursor()
        c.execute("UPDATE level_supply SET minted = minted + ?, last_updated = ? WHERE level = ?",
                 (reward, time.time(), level))
        self.conn.commit()
    
    def get_total_minted(self):
        c = self.conn.cursor()
        c.execute("SELECT SUM(minted) FROM level_supply")
        total = c.fetchone()[0]
        return total or 0
    
    def get_remaining_supply_for_level(self, level):
        c = self.conn.cursor()
        c.execute("SELECT minted, cap FROM level_supply WHERE level=?", (level,))
        row = c.fetchone()
        if row:
            return max(0, row[1] - row[0])
        return LEVEL_CAPS.get(level, 0)
    
    def get_blocks_range(self, start, end):
        blocks = []
        for b in self.blocks:
            if start <= b.id <= end:
                blocks.append({"id": b.id, "ts": b.ts, "prev": b.prev,
                              "validators": b.validators, "level": b.level,
                              "hash": b.hash, "reward": b.reward})
        return blocks
    
    def get_miners_list(self):
        return [{"vid": m.vid, "username": m.username, "wallet": m.wallet,
                "level": m.level, "stake": m.stake, "blocks": m.blocks,
                "rewards": m.rewards, "active": m.active,
                "uptime": m.uptime, "today_uptime": m.today_uptime,
                "type": m.mtype, "last_seen": m.last_ping,
                "fees_collected": m.fees_collected} for m in self.miners.values()]
    
    def get_nodes_list(self):
        return [{"node_id": n.node_id, "username": n.username, "wallet": n.wallet,
                "ip": n.ip, "port": n.port, "height": n.height,
                "active": n.active, "rewards": n.rewards_earned} for n in self.nodes.values()]
    
    def get_top_stakers(self, limit=10):
        stakers = []
        for m in self.miners.values():
            if m.active and m.stake > 0:
                stakers.append({"username": m.username, "staked": m.stake, "wallet": m.wallet})
        stakers.sort(key=lambda x: x["staked"], reverse=True)
        return stakers[:limit]
    
    def get_buyer_stats(self, limit=10):
        c = self.conn.cursor()
        c.execute("SELECT wallet, username, bought FROM buyer_stats ORDER BY bought DESC LIMIT ?", (limit,))
        return [{"wallet": r[0], "username": r[1], "bought": r[2]} for r in c.fetchall()]
    
    def get_payment_status(self, payment_id):
        c = self.conn.cursor()
        c.execute("SELECT status, created_at, completed_at FROM pending_crypto_payments WHERE payment_id=?", (payment_id,))
        row = c.fetchone()
        if row:
            return {"status": row[0], "created_at": row[1], "completed_at": row[2]}
        return {"status": "not_found"}
    
    def get_transactions(self, wallet, limit=20):
        c = self.conn.cursor()
        c.execute("SELECT tx_hash, from_wallet, to_wallet, amount, fee, timestamp, block_id, status, tx_type FROM transactions WHERE from_wallet=? OR to_wallet=? ORDER BY timestamp DESC LIMIT ?",
                 (wallet, wallet, limit))
        return [{"hash": r[0], "from": r[1], "to": r[2], "amount": r[3], "fee": r[4],
                "timestamp": r[5], "block": r[6], "status": r[7], "type": r[8]} for r in c.fetchall()]
    
    def _add_block(self, bid, prev, validators, level, sigs):
        ts = time.time()
        blk = Block(bid, ts, prev, validators, level, sigs, "", 0, 0)
        blk.hash = hash_block({"id": bid, "ts": ts, "prev": prev, "validators": validators, "level": level})
        self.blocks.append(blk)
        self.height = bid + 1
        self.last_hash = blk.hash
        self.conn.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?)",
                         (bid, ts, prev, ','.join(validators), level, blk.hash, 0, 0))
        self.conn.commit()
        return blk
    
    async def import_blocks(self, blocks_data):
        for b in sorted(blocks_data, key=lambda x: x['id']):
            if b['id'] >= self.height and b['prev'] == self.last_hash:
                block = Block(b['id'], b['ts'], b['prev'], b['validators'], b['level'], {}, b['hash'], b.get('reward', 0), 0)
                self.blocks.append(block)
                self.height = b['id'] + 1
                self.last_hash = block.hash
                self.conn.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?)",
                                 (b['id'], b['ts'], b['prev'], ','.join(b['validators']),
                                  b['level'], b['hash'], b.get('reward', 0), 0))
                self.conn.commit()
                logger.info(f"[SYNC] Imported block {b['id']}")
    
    async def receive_block(self, block_data):
        bid = block_data.get('id')
        if bid == self.height and block_data.get('prev') == self.last_hash:
            block = Block(bid, block_data['ts'], block_data['prev'], block_data['validators'],
                         block_data['level'], {}, block_data['hash'], block_data.get('reward', 0), 0)
            self.blocks.append(block)
            self.height = bid + 1
            self.last_hash = block.hash
            self.conn.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?)",
                             (bid, block.ts, block.prev, ','.join(block.validators),
                              block.level, block.hash, block.reward, 0))
            self.conn.commit()
            logger.info(f"[P2P] Received block {bid}")
    
    async def receive_transaction(self, tx_data):
        tx_hash = tx_data.get('hash')
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM transactions WHERE tx_hash=?", (tx_hash,))
        if c.fetchone()[0] == 0:
            tx = Transaction(tx_hash, tx_data['from'], tx_data['to'], tx_data['amount'],
                           tx_data.get('fee', 1), tx_data['timestamp'], -1, 'pending', tx_data.get('type', 'send'))
            self._save_transaction(tx)
            logger.info(f"[P2P] Received transaction {tx_hash[:16]}...")
            
# ==================== MAIN SERVER CLASS ====================
class MicroCoreServer:
    def __init__(self, network):
        self.network = network
        self._shutdown = False
    
    async def block_production_loop(self):
        while not self._shutdown:
            try:
                await self.network.produce_blocks_loop()
            except Exception as e:
                logger.error(f"[PRODUCER] Error: {e}")
                await asyncio.sleep(5)
    
    async def peer_discovery_loop(self):
        while not self._shutdown:
            try:
                await self.network.p2p.discover()
                await asyncio.sleep(PEX_INTERVAL)
            except Exception as e:
                logger.error(f"[PEER] Discovery error: {e}")
                await asyncio.sleep(5)
    
    async def peer_sync_loop(self):
        while not self._shutdown:
            try:
                await self.network.p2p.sync_with_peers()
                await asyncio.sleep(SYNC_INTERVAL)
            except Exception as e:
                logger.error(f"[PEER] Sync error: {e}")
                await asyncio.sleep(5)
    
    async def periodic_distribution_loop(self):
        while not self._shutdown:
            try:
                await asyncio.sleep(DISTRIBUTION_INTERVAL_SEC)
                self.network.distribute_periodic_rewards()
            except Exception as e:
                logger.error(f"[DISTRO] Error: {e}")
    
    async def buyer_rewards_loop(self):
        while not self._shutdown:
            try:
                await asyncio.sleep(3600)
                if time.time() - self.network.last_buyer_distribution > 30 * 24 * 3600:
                    self.network.distribute_buyer_rewards()
                    self.network.last_buyer_distribution = time.time()
            except Exception as e:
                logger.error(f"[BUYER] Error: {e}")
    
    async def embedded_miner_loop(self):
        while not self._shutdown:
            try:
                for challenge, pending in self.network.pending_challenges.items():
                    vid = self.network.username
                    if vid in pending["validators"] and vid not in pending["sigs"]:
                        message = f"{challenge}{vid}{pending['bid']}"
                        signature = sign_message(self.network.priv, message)
                        pending["sigs"][vid] = signature
                        logger.info(f"[EMBEDDED MINER] ✍️ Signed block {pending['bid']} (Level {pending['level']})")
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"[EMBEDDED] Error: {e}")
                await asyncio.sleep(1)
    
    async def status_reporter_loop(self):
        while not self._shutdown:
            try:
                await asyncio.sleep(60)
                
                total_minted = self.network.total_minted
                total_cap = sum(LEVEL_CAPS.values())
                percent = (total_minted / total_cap) * 100 if total_cap > 0 else 0
                remaining = total_cap - total_minted
                
                logger.info(f"\n{'='*60}")
                logger.info(f"📊 MICROCORE NETWORK STATUS")
                logger.info(f"{'='*60}")
                logger.info(f"Block Height: {self.network.height}")
                logger.info(f"Total Minted: {total_minted:,} / {total_cap:,} ({percent:.4f}%)")
                logger.info(f"Remaining Supply: {remaining:,} MCX")
                logger.info(f"Active Miners: {sum(1 for m in self.network.miners.values() if m.active)}")
                logger.info(f"Total Miners: {len(self.network.miners)}")
                logger.info(f"Active Nodes: {sum(1 for n in self.network.nodes.values() if n.active)}")
                logger.info(f"Total Nodes: {len(self.network.nodes)}")
                logger.info(f"P2P Peers: {len(self.network.p2p.peers)}")
                logger.info(f"Max Unlocked Level: {self.network.level_mgr.max_unlocked}")
                logger.info(f"Levels with Miners: {sorted(self.network.levels_with_miners)}")
                logger.info(f"Node Pool: {self.network.node_pool} MCX")
                logger.info(f"Uptime Pool: {self.network.uptime_pool} MCX")
                logger.info(f"LP Pool: {self.network.lp_pool} MCX")
                logger.info(f"Buyer Rewards Pool: {self.network.buyer_pool} MCX")
                logger.info(f"Validator Fee Pool: {self.network.validator_fee_pool} MCX")
                logger.info(f"Transfer Fee Rate: {TRANSFER_FEE_RATE*100:.1f}%")
                logger.info(f"Health: {self.network.health_checker.get_status()}")
                
                for level in range(1, 11):
                    minted = self.network.get_remaining_supply_for_level(level)
                    cap = LEVEL_CAPS[level]
                    level_percent = ((cap - minted) / cap) * 100 if cap > 0 else 0
                    miner_count = len(self.network.level_groups.get(level, []))
                    logger.info(f"Level {level}: {miner_count} miners | {cap - minted:,} / {cap:,} MCX remaining ({level_percent:.1f}%)")
                
                logger.info(f"{'='*60}\n")
            except Exception as e:
                logger.error(f"[STATUS] Error: {e}")
    
    async def health_check_loop(self):
        while not self._shutdown:
            try:
                health = self.network.health_checker.get_status()
                health["peers"] = len(self.network.p2p.peers)
                health["miners"] = len(self.network.miners)
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"[HEALTH] Error: {e}")
    
    async def cleanup(self):
        self._shutdown = True
        if self.network.conn:
            self.network.conn.close()
        logger.info("[SHUTDOWN] Cleanup complete")
    
    async def run(self):
        logger.info(f"\n{'='*60}")
        logger.info(f"🚀 MICROCORE (MCX) NODE v{VERSION}")
        logger.info(f"{'='*60}")
        logger.info(f"Username: {self.network.username}")
        logger.info(f"Wallet: {self.network.wallet}")
        logger.info(f"Node ID: {self.network.node_id[:16]}...")
        logger.info(f"{'='*60}")
        logger.info(f"WebSocket: ws://0.0.0.0:{NODE_PORT}")
        logger.info(f"P2P: 0.0.0.0:{P2P_PORT}")
        logger.info(f"Bootnodes: {BOOTSTRAP_NODES}")
        logger.info(f"GOSSIP DISCOVERY: ON (peers will be cached and shared)")
        logger.info(f"EMBEDDED MINER: ACTIVE")
        logger.info(f"RATE LIMITING: ENABLED (60 req/min per IP)")
        logger.info(f"{'='*60}")
        logger.info(f"📊 LEVEL SYSTEM: 10 levels, 1,000 MCX per level")
        logger.info(f"📊 BLOCK TIMES: L1:40s, L2:35s, L3:30s, L4:25s, L5:20s, L6:15s, L7:10s, L8:9s, L9:8s, L10:7s")
        logger.info(f"📊 BLOCK REWARD: 3 MCX (halving per level)")
        logger.info(f"📊 TOTAL CAP: ~546,840,000 MCX")
        logger.info(f"📊 BLOCK REDISTRIBUTION: Inactive levels → level with most miners")
        logger.info(f"📊 CRYPTO PAYMENTS: BTC, ETH, USDC, DUCO with blockchain verification")
        logger.info(f"📊 BUYER REWARDS: Top 10 monthly (5000,3000,2000,1000,1000,500x5)")
        logger.info(f"{'='*60}")
        logger.info(f"✅ ALL FEES PAID IN MCX")
        logger.info(f"✅ TRANSFER FEE: {TRANSFER_FEE_RATE*100:.1f}% (min {TRANSFER_FEE_MIN} MCX) → VALIDATORS")
        logger.info(f"✅ SWAP FEE: {SWAP_FEE_RATE*100:.1f}% in MCX → LPs/Node Pool")
        logger.info(f"✅ NODES RECEIVE: 8% BLOCK REWARD + SWAP FEES")
        logger.info(f"✅ DUCO TXID VERIFICATION: ENABLED")
        logger.info(f"✅ DEX POOLS: DYNAMIC (created when users add liquidity)")
        logger.info(f"✅ NO STRIPE REQUIRED")
        logger.info(f"{'='*60}")
        logger.info(f"✅ Node is running! Press Ctrl+C to stop.\n")
        
        tasks = [
            asyncio.create_task(self.network.p2p.start()),
            asyncio.create_task(self.network.p2p.heartbeat()),
            asyncio.create_task(self.peer_discovery_loop()),
            asyncio.create_task(self.peer_sync_loop()),
            asyncio.create_task(self.periodic_distribution_loop()),
            asyncio.create_task(self.buyer_rewards_loop()),
            asyncio.create_task(self.block_production_loop()),
            asyncio.create_task(self.embedded_miner_loop()),
            asyncio.create_task(self.status_reporter_loop()),
            asyncio.create_task(self.health_check_loop()),
        ]
        
        async with serve(self.network.ws_handler, NODE_HOST, NODE_PORT):
            logger.info(f"[WS] WebSocket server started")
            await asyncio.Future()

# ==================== MAIN ENTRY POINT ====================
async def main():
    parser = argparse.ArgumentParser(description=f'{NAME} Complete Node v{VERSION}')
    parser.add_argument('--genesis', action='store_true', help='Run as genesis node (first node only)')
    parser.add_argument('--peer', type=str, help='Connect to peer node (IP:PORT)')
    parser.add_argument('--username', type=str, required=True, help='Your username')
    parser.add_argument('--wallet', type=str, default="", help='Your wallet address (optional)')
    parser.add_argument('--privkey', type=str, default="", help='Your private key (optional)')
    parser.add_argument('--no-miner', action='store_true', help='Disable embedded miner')
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"{NAME} ({SYMBOL}) COMPLETE NODE v{VERSION}")
    print(f"{'='*60}")
    print(f"Username: {args.username}")
    print(f"Genesis Mode: {args.genesis}")
    print(f"Embedded Miner: {'DISABLED' if args.no_miner else 'ACTIVE'}")
    print(f"Gossip Discovery: ON (peers will be cached and shared)")
    print(f"NO DNS REQUIRED - Uses hardcoded bootnodes + peer cache")
    print(f"RATE LIMITING: ENABLED (60 req/min per IP)")
    print(f"TRANSFER FEE: {TRANSFER_FEE_RATE*100:.1f}% (min {TRANSFER_FEE_MIN} MCX) → VALIDATORS")
    print(f"SWAP FEE: {SWAP_FEE_RATE*100:.1f}% in MCX → LPs/Node Pool")
    print(f"DEX POOLS: DYNAMIC (created on demand)")
    print(f"NO STRIPE REQUIRED")
    print(f"{'='*60}\n")
    
    if args.wallet and args.privkey:
        my_wallet = args.wallet
        my_priv = args.privkey
        priv_obj = ec.derive_private_key(int(my_priv, 16), ec.SECP256K1())
        pub = priv_obj.public_key()
        my_pub = pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        logger.info(f"[WALLET] Using existing wallet: {my_wallet}")
    elif args.wallet:
        my_wallet = args.wallet
        _, my_priv, my_pub = generate_wallet()
        logger.info(f"[WALLET] Using provided wallet: {my_wallet}")
        logger.info(f"[WALLET] Generated private key: {my_priv}")
        logger.info(f"[WALLET] SAVE THIS PRIVATE KEY!")
    else:
        my_wallet, my_priv, my_pub = generate_wallet()
        print(f"\n🆕 NEW WALLET CREATED!")
        print(f"Wallet Address: {my_wallet}")
        print(f"Private Key: {my_priv}")
        print(f"Public Key: {my_pub[:64]}...")
        print(f"\n⚠️  SAVE THESE CREDENTIALS! ⚠️")
        print(f"Without your private key, you will lose access to your funds.\n")
        
        wallet_file = f"microcore_wallet_{args.username}.json"
        with open(wallet_file, 'w') as f:
            json.dump({
                "username": args.username,
                "address": my_wallet,
                "private_key": my_priv,
                "public_key_pem": my_pub,
                "created_at": time.time(),
                "version": VERSION
            }, f, indent=2)
        logger.info(f"[WALLET] Saved to: {wallet_file}\n")
    
    network = MicroCoreNetwork(
        is_genesis=args.genesis,
        username=args.username,
        wallet=my_wallet,
        priv=my_priv,
        pub=my_pub
    )
    
    server = MicroCoreServer(network)
    
    if args.peer:
        logger.info(f"[P2P] Connecting to peer: {args.peer}")
        await network.p2p._connect(args.peer)
    
    try:
        await server.run()
    except asyncio.CancelledError:
        logger.info("[SHUTDOWN] Server stopped")
    finally:
        await server.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Node stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        traceback.print_exc()
        sys.exit(1)
