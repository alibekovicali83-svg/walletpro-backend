from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Literal, Optional, Dict, List
from datetime import datetime

# =========================
# CONFIG (MVP)
# =========================
DEPOSIT_ADDRESS = "TL9RqzHjNLfB2soDSK5SZxE2E8wQno9ixB"  # ✅ твой TRC20 адрес (TRON)
DEPOSIT_NETWORK_LABEL = "USDT TRC20 (TRON)"               # ✅ правильная сеть
ADMIN_KEY = "Pipchik121"                         # 🔐 поменяй на свой пароль

app = FastAPI(title="WalletPro Backend", version="0.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    dep_id = f"dep_{len(deposits)+1}"
    item = {
        "id": dep_id,
        "tg_id": tg_id,
        "txid": body.txid,
        "amount_usdt": body.amount_usdt,
        "status": "CREATED",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    deposits.append(item)
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

    if item["status"] == "CREATED" and body.status == "REJECTED":
        w = get_wallet(item["tg_id"])
        w["balance_usdt"] = round(w["balance_usdt"] + float(item["amount_usdt"]), 6)

    item["status"] = body.status
    item["txid"] = body.txid or item["txid"]
    item["updated_at"] = now_iso()
    return {"ok": True, "withdrawal": item}
