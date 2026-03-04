from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal, Optional, Dict, List, Tuple
from datetime import datetime
import os
import hashlib
import httpx

# =========================
# CONFIG
# =========================
DEPOSIT_ADDRESS = os.getenv("DEPOSIT_ADDRESS", "TL9RqzHjNLfB2soDSK5SZxE2E8wQno9ixB")  # ваш TRON адрес (base58)
DEPOSIT_NETWORK_LABEL = os.getenv("DEPOSIT_NETWORK_LABEL", "USDT TRC20 (TRON)")

# Админ-ключ (лучше задавать в Render -> Environment)
ADMIN_KEY = os.getenv("ADMIN_KEY", "change_me_admin_key")

# TRON / TronGrid
TRON_API_BASE = os.getenv("TRON_API_BASE", "https://api.trongrid.io").rstrip("/")
TRON_API_KEY = os.getenv("TRON_API_KEY", "").strip()  # опционально, но рекомендовано (лимиты)
USDT_TRC20_CONTRACT = os.getenv("USDT_TRC20_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
USDT_DECIMALS = int(os.getenv("USDT_DECIMALS", "6"))

app = FastAPI(title="WalletPro Backend", version="0.3-tron")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MVP, потом ограничить доменом GitHub Pages
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Wallet = Dict[str, float]
wallets: Dict[int, Wallet] = {}
deposits: List[dict] = []
withdrawals: List[dict] = []

def get_wallet(tg_id: int) -> Wallet:
    if tg_id not in wallets:
        wallets[tg_id] = {"balance_usdt": 0.0}
    return wallets[tg_id]

def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

# =========================
# TRON address utils (base58check -> 0x...)
# =========================
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

def _b58decode(s: str) -> bytes:
    num = 0
    for ch in s:
        if ch not in _B58_INDEX:
            raise ValueError("invalid base58 char")
        num = num * 58 + _B58_INDEX[ch]

    full = num.to_bytes((num.bit_length() + 7) // 8, "big") if num > 0 else b""
    # leading zeros
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + full

def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()

def tron_base58_to_hex0x(addr_b58: str) -> str:
    """
    TRON base58 address -> '0x' + 20 bytes hex (EVM-style) to compare with TronGrid event result fields.
    """
    raw = _b58decode(addr_b58)
    if len(raw) < 5:
        raise ValueError("bad base58 data")
    payload, checksum = raw[:-4], raw[-4:]
    chk = _sha256(_sha256(payload))[:4]
    if chk != checksum:
        raise ValueError("bad checksum")
    # payload must be 21 bytes: 0x41 + 20 bytes
    if len(payload) != 21 or payload[0] != 0x41:
        raise ValueError("bad tron address payload")
    evm20 = payload[1:]  # 20 bytes
    return "0x" + evm20.hex()

DEPOSIT_EVM_HEX = None
try:
    DEPOSIT_EVM_HEX = tron_base58_to_hex0x(DEPOSIT_ADDRESS).lower()
except Exception:
    DEPOSIT_EVM_HEX = None  # если DEPOSIT_ADDRESS неверный — проверки не будут работать

# =========================
# API models
# =========================
class AuthIn(BaseModel):
    tg_id: int
    username: Optional[str] = None

class AuthOut(BaseModel):
    token: str

class WalletOut(BaseModel):
    deposit_address: str
    deposit_network: str
    balance_usdt: float

class DepositCreateIn(BaseModel):
    txid: str = Field(..., min_length=10)
    amount_usdt: Optional[float] = None

class DepositOut(BaseModel):
    id: str
    tg_id: int
    txid: str
    amount_usdt: Optional[float]
    status: Literal["CREATED", "CREDITED", "REJECTED"]
    created_at: str
    updated_at: str

class DepositRecheckIn(BaseModel):
    id: str = Field(..., min_length=1)

class WithdrawalCreateIn(BaseModel):
    address: str = Field(..., min_length=10)
    amount_usdt: float = Field(..., gt=0)

class WithdrawalOut(BaseModel):
    id: str
    tg_id: int
    address: str
    amount_usdt: float
    status: Literal["CREATED", "PAID", "REJECTED"]
    txid: Optional[str]
    created_at: str
    updated_at: str

class AdminAdjustIn(BaseModel):
    admin_key: str
    tg_id: int
    delta_usdt: float
    reason: str = ""

class AdminSetStatusIn(BaseModel):
    admin_key: str
    id: str
    status: str
    txid: Optional[str] = None

def make_token(tg_id: int) -> str:
    return f"dev-token-{tg_id}"

def parse_token(token: str) -> int:
    if not token.startswith("dev-token-"):
        raise HTTPException(401, "Invalid token")
    try:
        return int(token.replace("dev-token-", "").strip())
    except:
        raise HTTPException(401, "Invalid token")

# =========================
# TronGrid helpers
# =========================
def _trongrid_headers() -> dict:
    h = {"Accept": "application/json"}
    if TRON_API_KEY:
        # стандартный заголовок TronGrid API Key
        h["TRON-PRO-API-KEY"] = TRON_API_KEY
    return h

def try_extract_usdt_incoming_amount(txid: str) -> Optional[float]:
    """
    Ищем в событиях транзакции Transfer() по контракту USDT,
    где to == наш DEPOSIT_ADDRESS. Возвращаем amount_usdt (float) или None.
    Используем TronGrid events-by-txid endpoint. :contentReference[oaicite:4]{index=4}
    """
    if not DEPOSIT_EVM_HEX:
        return None

    url = f"{TRON_API_BASE}/v1/transactions/{txid}/events?only_confirmed=true"
    try:
        with httpx.Client(timeout=12.0) as client:
            r = client.get(url, headers=_trongrid_headers())
        if r.status_code != 200:
            return None
        j = r.json()
    except Exception:
        return None

    data = j.get("data") or []
    for ev in data:
        if str(ev.get("event_name", "")).lower() != "transfer":
            continue
        if ev.get("contract_address") != USDT_TRC20_CONTRACT:
            continue

        res = ev.get("result") or {}
        to_hex = str(res.get("to", "")).lower()
        if to_hex != DEPOSIT_EVM_HEX:
            continue

        value_str = str(res.get("value", "")).strip()
        if not value_str.isdigit():
            continue

        raw_int = int(value_str)
        amt = raw_int / float(10 ** USDT_DECIMALS)
        # округлим аккуратно, чтобы не светить хвосты
        return round(amt, 6)

    return None

def maybe_autocredit_deposit(dep_item: dict) -> dict:
    """
    Если можем подтвердить TXID через TRON — выставляем amount_usdt, CREDITED и начисляем баланс.
    """
    if dep_item.get("status") != "CREATED":
        return dep_item

    amt = try_extract_usdt_incoming_amount(dep_item["txid"])
    if amt is None or amt <= 0:
        return dep_item

    # ставим amount по блокчейну (не верим ручному)
    dep_item["amount_usdt"] = float(amt)

    # начисляем баланс
    w = get_wallet(dep_item["tg_id"])
    w["balance_usdt"] = round(w["balance_usdt"] + float(amt), 6)

    dep_item["status"] = "CREDITED"
    dep_item["updated_at"] = now_iso()
    return dep_item

# =========================
# Routes
# =========================
@app.get("/health")
def health():
    return {"ok": True, "service": "walletpro-backend"}

@app.post("/auth/dev", response_model=AuthOut)
def auth_dev(body: AuthIn):
    get_wallet(body.tg_id)
    return {"token": make_token(body.tg_id)}

@app.get("/wallet", response_model=WalletOut)
def wallet(token: str):
    tg_id = parse_token(token)
    w = get_wallet(tg_id)
    return {
        "deposit_address": DEPOSIT_ADDRESS,
        "deposit_network": DEPOSIT_NETWORK_LABEL,
        "balance_usdt": w["balance_usdt"],
    }

@app.post("/deposits", response_model=DepositOut)
def create_deposit(token: str, body: DepositCreateIn):
    tg_id = parse_token(token)

    txid = body.txid.strip()
    # простая защита от дублей (чтобы один TXID не зачислили дважды)
    if any(d.get("txid") == txid for d in deposits):
        raise HTTPException(400, "TXID already exists")

    dep_id = f"dep_{len(deposits)+1}"
    item = {
        "id": dep_id,
        "tg_id": tg_id,
        "txid": txid,
        "amount_usdt": body.amount_usdt,
        "status": "CREATED",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    deposits.append(item)

    # авто-проверка / авто-зачисление
    maybe_autocredit_deposit(item)
    return item

@app.post("/deposits/recheck", response_model=DepositOut)
def recheck_deposit(token: str, body: DepositRecheckIn):
    tg_id = parse_token(token)
    item = next((d for d in deposits if d["id"] == body.id and d["tg_id"] == tg_id), None)
    if not item:
        raise HTTPException(404, "Deposit not found")
    if item["status"] != "CREATED":
        return item
    maybe_autocredit_deposit(item)
    return item

@app.get("/deposits", response_model=List[DepositOut])
def list_deposits(token: str):
    tg_id = parse_token(token)
    return [d for d in deposits if d["tg_id"] == tg_id]

@app.post("/withdrawals", response_model=WithdrawalOut)
def create_withdrawal(token: str, body: WithdrawalCreateIn):
    tg_id = parse_token(token)
    w = get_wallet(tg_id)
    if body.amount_usdt > w["balance_usdt"]:
        raise HTTPException(400, "Insufficient balance")

    # резерв: сразу уменьшаем баланс
    w["balance_usdt"] = round(w["balance_usdt"] - body.amount_usdt, 6)

    wid = f"wd_{len(withdrawals)+1}"
    item = {
        "id": wid,
        "tg_id": tg_id,
        "address": body.address,
        "amount_usdt": body.amount_usdt,
        "status": "CREATED",
        "txid": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    withdrawals.append(item)
    return item

@app.get("/withdrawals", response_model=List[WithdrawalOut])
def list_withdrawals(token: str):
    tg_id = parse_token(token)
    return [w for w in withdrawals if w["tg_id"] == tg_id]

# =========================
# ADMIN endpoints
# =========================
@app.post("/admin/adjust")
def admin_adjust(body: AdminAdjustIn):
    if body.admin_key != ADMIN_KEY:
        raise HTTPException(401, "Bad admin key")
    w = get_wallet(body.tg_id)
    w["balance_usdt"] = round(w["balance_usdt"] + body.delta_usdt, 6)
    if w["balance_usdt"] < 0:
        w["balance_usdt"] = 0.0
    return {"ok": True, "tg_id": body.tg_id, "balance_usdt": w["balance_usdt"], "reason": body.reason}

@app.post("/admin/deposit_status")
def admin_deposit_status(body: AdminSetStatusIn):
    if body.admin_key != ADMIN_KEY:
        raise HTTPException(401, "Bad admin key")
    item = next((d for d in deposits if d["id"] == body.id), None)
    if not item:
        raise HTTPException(404, "Deposit not found")

    # если кредитуем — начисляем баланс (требуется amount_usdt)
    if item["status"] != "CREDITED" and body.status == "CREDITED":
        amt = float(item["amount_usdt"] or 0.0)
        if amt <= 0:
            raise HTTPException(400, "amount_usdt is required to credit")
        w = get_wallet(item["tg_id"])
        w["balance_usdt"] = round(w["balance_usdt"] + amt, 6)

    item["status"] = body.status
    item["updated_at"] = now_iso()
    return {"ok": True, "deposit": item}

@app.post("/admin/withdrawal_status")
def admin_withdrawal_status(body: AdminSetStatusIn):
    if body.admin_key != ADMIN_KEY:
        raise HTTPException(401, "Bad admin key")
    item = next((w for w in withdrawals if w["id"] == body.id), None)
    if not item:
        raise HTTPException(404, "Withdrawal not found")

    # если отклоняем — возвращаем деньги
    if item["status"] == "CREATED" and body.status == "REJECTED":
        w = get_wallet(item["tg_id"])
        w["balance_usdt"] = round(w["balance_usdt"] + float(item["amount_usdt"]), 6)

    item["status"] = body.status
    item["txid"] = body.txid or item["txid"]
    item["updated_at"] = now_iso()
    return {"ok": True, "withdrawal": item}