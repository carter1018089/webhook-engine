import sqlite3
import json
import urllib.parse
import time
import logging
import requests
import os
from openai import OpenAI
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Axiom Real-Time AI & Chart PnL Engine")

# ==========================================
# HARDCODED DISCORD WEBHOOKS & KEYS
# ==========================================
WEBHOOK_LIVE_TRADES = "https://discordapp.com/api/webhooks/1531447011453174021/DW0zfF-zEdFD3Pvjy09ZjN9DN8zoNThUyZdYoS0FBpGMfNTIzJgLB1T_AO5t_luffccc"
WEBHOOK_CA_ANALYST  = "https://discordapp.com/api/webhooks/1531454599821660270/fbD15xrqZ4yKUwavSwzrRWqCXIqqI8sxLHcx-TBRL2ZkgS-n12422PnMU-XfJIIsvRkZ"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6JXxf4suKDOHpPF2W7AnKPBeD8m2oS-Jwuq5ICCKhE4cw")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "82763842-be46-4d5d-90cd-d697229f357a")
HELIUS_WEBHOOK_ID = os.getenv("HELIUS_WEBHOOK_ID", "c93c768f-c1b7-415d-bdca-cff100e0ed47")

# ==========================================
# MULTI-PROVIDER AI ROTATOR
# ==========================================
class AIRotatorEngine:
    def __init__(self, additional_keys: dict = None):
        self.providers = []

        # TIER 1: Google Gemini (Primary)
        if GEMINI_API_KEY:
            self.providers.append({
                "name": "Google Gemini",
                "client": OpenAI(
                    api_key=GEMINI_API_KEY,
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
                ),
                "model": "gemini-2.5-flash"
            })

        # TIER 2: Groq LPU Failover
        if GROQ_API_KEY or (additional_keys and "groq" in additional_keys):
            groq_key = GROQ_API_KEY or additional_keys["groq"][0]
            self.providers.append({
                "name": "Groq LPU",
                "client": OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1"),
                "model": "llama-3.3-70b-versatile"
            })

        self.current_index = 0

    def generate_completion(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        if not self.providers:
            return "AI analysis skipped (No API keys configured)."

        attempts = 0
        max_attempts = len(self.providers)

        while attempts < max_attempts:
            provider = self.providers[self.current_index]
            try:
                logging.info(f"🤖 Requesting via {provider['name']} ({provider['model']})...")
                response = provider["client"].chat.completions.create(
                    model=provider["model"],
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=temperature,
                    timeout=10.0
                )
                return response.choices[0].message.content
            except Exception as e:
                logging.warning(f"⚠️ {provider['name']} unavailable/rate-limited: {e}")
                self.current_index = (self.current_index + 1) % len(self.providers)
                attempts += 1
                time.sleep(0.5)

        return "AI analysis skipped (All providers rate-limited)."

ai_engine = AIRotatorEngine()

# ==========================================
# DATABASE SETUP
# ==========================================
def init_db():
    conn = sqlite3.connect("pnl_tracker.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            token_mint TEXT PRIMARY KEY,
            symbol TEXT,
            amount_held REAL,
            total_sol_spent REAL,
            avg_cost_per_token REAL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_signature TEXT UNIQUE,
            type TEXT,
            token_mint TEXT,
            amount REAL,
            sol_amount REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ==========================================
# QUICKCHART & DEXSCREENER HELPERS
# ==========================================
def generate_trade_chart_url(symbol: str, entry_price_sol: float, exit_price_sol: float) -> str:
    """Generates a visual bar chart URL using QuickChart REST API."""
    pnl_color = "#10B981" if exit_price_sol >= entry_price_sol else "#EF4444"
    
    chart_config = {
        "type": "bar",
        "data": {
            "labels": ["Buy Entry", "Sell Exit"],
            "datasets": [{
                "label": f"${symbol} Price (SOL)",
                "data": [entry_price_sol, exit_price_sol],
                "backgroundColor": ["#3B82F6", pnl_color],
                "borderRadius": 6
            }]
        },
        "options": {
            "legend": {"display": False},
            "title": {
                "display": True,
                "text": f"Execution Spread: ${symbol}",
                "fontColor": "#FFFFFF",
                "fontSize": 16
            },
            "scales": {
                "yAxes": [{
                    "ticks": {"fontColor": "#A1A1AA", "beginAtZero": False},
                    "gridLines": {"color": "#27272A"}
                }],
                "xAxes": [{
                    "ticks": {"fontColor": "#A1A1AA"},
                    "gridLines": {"display": False}
                }]
            }
        }
    }

    params = {
        "chart": json.dumps(chart_config),
        "width": 500,
        "height": 260,
        "backgroundColor": "#18181B"
    }

    return f"https://quickchart.io/chart?{urllib.parse.urlencode(params)}"

def get_dexscreener_info(token_ca: str):
    if not token_ca or len(token_ca) < 30:
        return 0.0, "TOKEN"
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_ca}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            pairs = data.get("pairs", [])
            if pairs:
                top_pair = max(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0)
                price_sol = float(top_pair.get("priceNative", 0.0))
                symbol = top_pair.get("baseToken", {}).get("symbol", "TOKEN")
                return price_sol, symbol
    except Exception:
        pass
    return 0.0, "TOKEN"

# ==========================================
# DISCORD RICH EMBED SENDER
# ==========================================
def send_discord_rich_embed(symbol: str, action: str, token_amount: float, sol_amount: float, 
                            realized_pnl: float, roi_percent: float, entry_price: float, 
                            exit_price: float, ai_insight: str, mint: str, tx_sig: str):
    
    chart_url = generate_trade_chart_url(symbol, entry_price, exit_price) if (action == "SELL" and exit_price > 0) else None
    dex_url = f"https://dexscreener.com/solana/{mint}" if mint else "https://dexscreener.com"
    solscan_url = f"https://solscan.io/tx/{tx_sig}"

    embed_color = 65280 if realized_pnl >= 0 else 16711680 # Green for Profit/Buy, Red for Loss

    fields = [
        {"name": "Action", "value": f"`{action}`", "inline": True},
        {"name": "Amount", "value": f"`{token_amount:,.2f} ${symbol}`", "inline": True},
        {"name": "SOL Flow", "value": f"`{sol_amount:.3f} SOL`", "inline": True},
    ]

    if action == "SELL":
        fields.append({"name": "Realized PnL", "value": f"**{realized_pnl:+.3f} SOL** ({roi_percent:+.2f}%)", "inline": True})
        fields.append({"name": "Entry ➔ Exit", "value": f"`{entry_price:.8f}` ➔ `{exit_price:.8f}` SOL", "inline": True})

    if ai_insight:
        title_tag = "🧠 Post-Mortem Analysis" if realized_pnl < 0 else "📈 Win Breakdown"
        fields.append({"name": title_tag, "value": f"> {ai_insight}", "inline": False})

    fields.append({"name": "Links", "value": f"[DexScreener]({dex_url}) | [Solscan]({solscan_url})", "inline": False})

    embed_payload = {
        "embeds": [{
            "title": f"⚡ AXIOM TRADE DETECTED: ${symbol}",
            "url": dex_url,
            "color": embed_color,
            "fields": fields,
            "footer": {"text": "Axiom Real-Time Engine • AI Rotator & Charts Active"}
        }]
    }

    if chart_url and action == "SELL":
        embed_payload["embeds"][0]["image"] = {"url": chart_url}

    try:
        requests.post(WEBHOOK_LIVE_TRADES, json=embed_payload, timeout=5)
    except Exception as e:
        logging.error(f"Failed to post to Discord Live Trades Channel: {e}")

# ==========================================
# TRADE PROCESSING ENGINE
# ==========================================
def process_and_log_trade(tx_type: str, mint: str, symbol: str, token_amount: float, sol_amount: float, tx_sig: str):
    conn = sqlite3.connect("pnl_tracker.db")
    cursor = conn.cursor()

    try:
        cursor.execute("INSERT INTO trades (tx_signature, type, token_mint, amount, sol_amount) VALUES (?, ?, ?, ?, ?)",
                       (tx_sig, tx_type, mint, token_amount, sol_amount))
    except sqlite3.IntegrityError:
        conn.close()
        return

    cursor.execute("SELECT amount_held, total_sol_spent, avg_cost_per_token FROM positions WHERE token_mint = ?", (mint,))
    pos = cursor.fetchone()

    entry_price = 0.0
    exit_price = sol_amount / token_amount if token_amount > 0 else 0.0
    realized_pnl = 0.0
    roi_percent = 0.0

    if tx_type == "BUY":
        if pos:
            new_amount = pos[0] + token_amount
            new_sol_spent = pos[1] + sol_amount
            new_avg_cost = new_sol_spent / new_amount if new_amount > 0 else 0
            cursor.execute("UPDATE positions SET amount_held = ?, total_sol_spent = ?, avg_cost_per_token = ? WHERE token_mint = ?",
                           (new_amount, new_sol_spent, new_avg_cost, mint))
            entry_price = new_avg_cost
        else:
            new_avg_cost = exit_price
            cursor.execute("INSERT INTO positions VALUES (?, ?, ?, ?, ?)", 
                           (mint, symbol, token_amount, sol_amount, new_avg_cost))
            entry_price = new_avg_cost

    elif tx_type == "SELL":
        if pos and pos[0] > 0:
            amount_held, total_sol_spent, avg_cost = pos
            entry_price = avg_cost
            cost_basis_sold = token_amount * avg_cost
            realized_pnl = sol_amount - cost_basis_sold
            roi_percent = (realized_pnl / cost_basis_sold * 100) if cost_basis_sold > 0 else 0.0

            remaining_amount = max(0.0, amount_held - token_amount)
            remaining_sol_spent = remaining_amount * avg_cost
            cursor.execute("UPDATE positions SET amount_held = ?, total_sol_spent = ? WHERE token_mint = ?",
                           (remaining_amount, remaining_sol_spent, mint))

    conn.commit()
    conn.close()

    # Conditional AI Generation
    ai_insight = None
    if tx_type == "SELL":
        if realized_pnl < 0:
            sys_prompt = "You are an expert crypto trading analyst. Provide a sharp, 2-sentence post-mortem explaining execution errors, slippage, or risk control lessons."
            usr_prompt = f"Action: SELL (LOSS) | Symbol: ${symbol} | Size: {sol_amount:.3f} SOL | Loss: {realized_pnl:.3f} SOL ({roi_percent:.2f}%)"
            ai_insight = ai_engine.generate_completion(sys_prompt, usr_prompt)
        else:
            sys_prompt = "You are an expert crypto trading analyst. Provide a sharp, 2-sentence breakdown explaining why this trade made money (momentum, entry timing, liquidity)."
            usr_prompt = f"Action: SELL (PROFIT) | Symbol: ${symbol} | Size: {sol_amount:.3f} SOL | Profit: +{realized_pnl:.3f} SOL (+{roi_percent:.2f}%)"
            ai_insight = ai_engine.generate_completion(sys_prompt, usr_prompt)

    send_discord_rich_embed(symbol, tx_type, token_amount, sol_amount, realized_pnl, roi_percent, entry_price, exit_price, ai_insight, mint, tx_sig)

def parse_helius_payload(payload: list):
    wsol_mint = "So11111111111111111111111111111111111111112"
    
    for tx in payload:
        if tx.get("transactionError") is not None:
            continue

        tx_sig = tx.get("signature", "")
        fee_payer = tx.get("feePayer", "")
        token_transfers = tx.get("tokenTransfers", [])
        native_transfers = tx.get("nativeTransfers", [])

        sol_amount = sum([float(nt.get("amount", 0)) / 1e9 for nt in native_transfers]) if native_transfers else 0.0

        for transfer in token_transfers:
            mint = transfer.get("mint", "")
            if mint and mint != wsol_mint:
                token_amount = float(transfer.get("tokenAmount", 0.0))
                to_user = transfer.get("toUserAccount", "")
                from_user = transfer.get("fromUserAccount", "")

                tx_type = "BUY" if to_user == fee_payer else "SELL" if from_user == fee_payer else "SWAP"
                
                _, symbol = get_dexscreener_info(mint)
                process_and_log_trade(tx_type, mint, symbol, token_amount, sol_amount, tx_sig)
                break

@app.post("/helius-webhook")
async def receive_helius_event(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    if isinstance(payload, list):
        background_tasks.add_task(parse_helius_payload, payload)
    return {"status": "ok"}

# ==========================================
# CHANNEL 2: CA ANALYST WITH BUY/SELL & BULL/BEAR NOTIFIER
# ==========================================
class CARequest(BaseModel):
    contract_address: str
    symbol: str = "UNKNOWN"

@app.post("/analyze-ca")
def analyze_ca(data: CARequest):
    ca = data.contract_address.strip()
    if not ca or len(ca) < 30:
        raise HTTPException(status_code=400, detail="Invalid Contract Address.")

    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
    try:
        dex_res = requests.get(dex_url, timeout=10)
        dex_data = dex_res.json()
        pairs = dex_data.get("pairs", [])
    except Exception:
        pairs = []

    if pairs:
        pair = max(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0)
        token_name = pair.get("baseToken", {}).get("name", "Solana Token")
        token_symbol = pair.get("baseToken", {}).get("symbol", data.symbol.upper())
        price_usd = pair.get("priceUsd", "N/A")
        market_cap = pair.get("fdv", pair.get("marketCap", "N/A"))
        liquidity_usd = pair.get("liquidity", {}).get("usd", "N/A")
        
        # Extract Buy/Sell Transaction Notifiers
        txns_24h = pair.get("txns", {}).get("h24", {})
        buys_24h = txns_24h.get("buys", 0)
        sells_24h = txns_24h.get("sells", 0)
        
        txns_5m = pair.get("txns", {}).get("m5", {})
        buys_5m = txns_5m.get("buys", 0)
        sells_5m = txns_5m.get("sells", 0)

        vol_24h = pair.get("volume", {}).get("h24", 0)
        price_change_24h = pair.get("priceChange", {}).get("h24", 0.0)
        
        # Calculate Bullish vs Bearish Overall Indicator
        total_txns_24h = buys_24h + sells_24h
        if total_txns_24h > 0:
            buy_pct = (buys_24h / total_txns_24h) * 100
            
            if buy_pct >= 60 and price_change_24h > 0:
                overall_sentiment = f"🟢 **STRONGLY BULLISH** ({buy_pct:.1f}% Buys)"
                embed_color = 65280 # Green
            elif buy_pct >= 52:
                overall_sentiment = f"🟢 **BULLISH** ({buy_pct:.1f}% Buys)"
                embed_color = 65280 # Green
            elif buy_pct <= 40 and price_change_24h < 0:
                overall_sentiment = f"🔴 **STRONGLY BEARISH** ({buy_pct:.1f}% Buys)"
                embed_color = 16711680 # Red
            elif buy_pct <= 48:
                overall_sentiment = f"🔴 **BEARISH** ({buy_pct:.1f}% Buys)"
                embed_color = 16711680 # Red
            else:
                overall_sentiment = f"🟡 **NEUTRAL / CONSOLIDATING** ({buy_pct:.1f}% Buys)"
                embed_color = 16776960 # Yellow
        else:
            overall_sentiment = "⚪ **NO RECENT TRADES**"
            embed_color = 8421504

        # Buy / Sell Activity Gauge
        if buys_5m > sells_5m:
            momentum_5m = "🔥 **BUY PRESSURE (5m)**"
        elif sells_5m > buys_5m:
            momentum_5m = "⚠️ **SELL PRESSURE (5m)**"
        else:
            momentum_5m = "⚖️ **EQUAL ACTIVITY (5m)**"

        mcap_formatted = f"${market_cap:,.0f}" if isinstance(market_cap, (int, float)) else f"${market_cap}"
        liq_formatted = f"${liquidity_usd:,.0f}" if isinstance(liquidity_usd, (int, float)) else f"${liquidity_usd}"
        vol_formatted = f"${vol_24h:,.0f}" if isinstance(vol_24h, (int, float)) else f"${vol_24h}"
    else:
        token_name = "Solana Token"
        token_symbol = data.symbol.upper() if data.symbol != "UNKNOWN" else "UNKNOWN"
        price_usd = "N/A"
        mcap_formatted = "N/A"
        liq_formatted = "N/A"
        vol_formatted = "N/A"
        buys_24h, sells_24h, buys_5m, sells_5m = 0, 0, 0, 0
        overall_sentiment = "⚠️ **NO DEX DATA FOUND**"
        momentum_5m = "N/A"
        price_change_24h = 0.0
        embed_color = 3447003

    embed = {
        "embeds": [{
            "title": f"📊 CA Analyst Report: {token_name} (${token_symbol})",
            "color": embed_color,
            "fields": [
                {"name": "Contract Address", "value": f"`{ca}`", "inline": False},
                {"name": "Price", "value": f"`${price_usd}` ({price_change_24h:+.2f}% 24h)", "inline": True},
                {"name": "Market Cap", "value": f"`{mcap_formatted}`", "inline": True},
                {"name": "Liquidity", "value": f"`{liq_formatted}`", "inline": True},
                {"name": "🚨 Overall Market Sentiment", "value": overall_sentiment, "inline": False},
                {"name": "⚡ 5m Buy/Sell Notifier", "value": f"🟢 **{buys_5m}** Buys | 🔴 **{sells_5m}** Sells\nStatus: {momentum_5m}", "inline": True},
                {"name": "📊 24h Buy/Sell Notifier", "value": f"🟢 **{buys_24h}** Buys | 🔴 **{sells_24h}** Sells\nVolume: `{vol_formatted}`", "inline": True},
                {
                    "name": "🔗 Quick Links", 
                    "value": f"[DexScreener](https://dexscreener.com/solana/{ca}) | [Solscan](https://solscan.io/token/{ca}) | [RugCheck](https://rugcheck.xyz/tokens/{ca})", 
                    "inline": False
                }
            ],
            "footer": {"text": "Axiom CA Analyst • Buy/Sell & Sentiment Notifiers Active"}
        }]
    }

    try:
        res = requests.post(WEBHOOK_CA_ANALYST, json=embed, timeout=5)
        if res.status_code in [200, 204]:
            return {"status": "posted_to_ca_channel"}
        else:
            raise HTTPException(status_code=500, detail=f"Discord Webhook Error: {res.status_code}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# ADD WALLET TO HELIUS WEBHOOK
# ==========================================
class AddWalletRequest(BaseModel):
    wallet_address: str

@app.post("/add-wallet")
def add_wallet_to_helius(data: AddWalletRequest):
    wallet = data.wallet_address.strip()
    if not wallet or len(wallet) < 32:
        raise HTTPException(status_code=400, detail="Invalid Solana wallet address.")

    get_url = f"https://api.helius.xyz/v0/webhooks/{HELIUS_WEBHOOK_ID}?api-key={HELIUS_API_KEY}"
    res = requests.get(get_url)
    if res.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch current Helius webhook info.")

    webhook_data = res.json()
    current_addresses = webhook_data.get("accountAddresses", [])

    if wallet in current_addresses:
        return {"status": "exists", "message": f"Wallet `{wallet}` is already being tracked."}

    current_addresses.append(wallet)
    webhook_data["accountAddresses"] = current_addresses

    put_res = requests.put(get_url, json=webhook_data)
    if put_res.status_code == 200:
        return {"status": "success", "message": f"Successfully added `{wallet}` to Helius tracker!", "total_tracked": len(current_addresses)}
    else:
        raise HTTPException(status_code=500, detail="Failed to update Helius webhook.")

if __name__ == "__main__":
    print("🚀 Axiom AI, PnL & Chart Webhook Engine running on port 8000...")
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8000, reload=True)
# ==========================================
# RUGCHECK & ON-CHAIN RISK HELPER
# ==========================================
def check_solana_rug_risk(ca: str):
    """
    Fetches bundling, holder concentration, and mint/freeze authority risks via RugCheck API.
    """
    url = f"https://api.rugcheck.xyz/v1/tokens/{ca}/report/summary"
    try:
        res = requests.get(url, timeout=6)
        if res.status_code == 200:
            data = res.json()
            
            # Extract risks
            risks = data.get("risks", [])
            risk_score = data.get("score", 0)
            
            # Extract Holder & Bundling metrics
            top_holders = data.get("topHolders", [])
            total_top_10_pct = sum([h.get("pct", 0) for h in top_holders[:10]])
            
            # Detect bundling / high risk flags
            bundled_flag = False
            mint_active = False
            freeze_active = False
            risk_summary_list = []

            for r in risks:
                name = r.get("name", "").lower()
                desc = r.get("description", "")
                if "bundle" in name or "bundle" in desc.lower():
                    bundled_flag = True
                    risk_summary_list.append("⚠️ **BUNDLED LAUNCH DETECTED**")
                if "mint" in name:
                    mint_active = True
                    risk_summary_list.append("🔴 Mint Authority Active")
                if "freeze" in name:
                    freeze_active = True
                    risk_summary_list.append("🔴 Freeze Authority Active")

            # Check single max holder
            max_single_holder = top_holders[0].get("pct", 0) if top_holders else 0
            if max_single_holder > 15:
                risk_summary_list.append(f"⚠️ Single Wallet Holds {max_single_holder:.1f}%")

            return {
                "score": risk_score,
                "top_10_pct": total_top_10_pct,
                "is_bundled": bundled_flag,
                "mint_active": mint_active,
                "freeze_active": freeze_active,
                "risk_flags": risk_summary_list
            }
    except Exception as e:
        logging.error(f"RugCheck API query failed: {e}")
        
    return {
        "score": 0,
        "top_10_pct": 0.0,
        "is_bundled": False,
        "mint_active": False,
        "freeze_active": False,
        "risk_flags": ["⚠️ Safety check timed out"]
    }

# ==========================================
# UPDATED CA ANALYST ENDPOINT
# ==========================================
@app.post("/analyze-ca")
def analyze_ca(data: CARequest):
    ca = data.contract_address.strip()
    if not ca or len(ca) < 30:
        raise HTTPException(status_code=400, detail="Invalid Contract Address.")

    # 1. Fetch DexScreener Trading Data
    dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
    try:
        dex_res = requests.get(dex_url, timeout=8)
        dex_data = dex_res.json()
        pairs = dex_data.get("pairs", [])
    except Exception:
        pairs = []

    # 2. Fetch On-Chain Rug & Bundling Risk Data
    rug_info = check_solana_rug_risk(ca)

    if pairs:
        pair = max(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0)
        token_name = pair.get("baseToken", {}).get("name", "Solana Token")
        token_symbol = pair.get("baseToken", {}).get("symbol", data.symbol.upper())
        price_usd = pair.get("priceUsd", "N/A")
        market_cap = pair.get("fdv", pair.get("marketCap", "N/A"))
        liquidity_usd = pair.get("liquidity", {}).get("usd", "N/A")
        
        # Transactions & Volume
        txns_24h = pair.get("txns", {}).get("h24", {})
        buys_24h = txns_24h.get("buys", 0)
        sells_24h = txns_24h.get("sells", 0)
        
        txns_5m = pair.get("txns", {}).get("m5", {})
        buys_5m = txns_5m.get("buys", 0)
        sells_5m = txns_5m.get("sells", 0)

        vol_24h = pair.get("volume", {}).get("h24", 0)
        price_change_24h = pair.get("priceChange", {}).get("h24", 0.0)
        
        # DexScreener Boosts / Traffic Indicators
        boosts = pair.get("boosts", {}).get("active", 0)
        boost_status = f"🔥 `{boosts}` DexScreener Boosts Active" if boosts > 0 else "⚪ No Active Boosts"

        # Calculate Sentiment & Safety Adjustments
        total_txns_24h = buys_24h + sells_24h
        buy_pct = (buys_24h / total_txns_24h * 100) if total_txns_24h > 0 else 0

        # OVERRIDE: If coin is bundled or top 10 hold > 50%, force sentiment to DANGER
        if rug_info["is_bundled"] or rug_info["top_10_pct"] > 50.0:
            overall_sentiment = f"🚨 **EXTREME RUG RISK / BUNDLED**"
            embed_color = 16711680 # Red
        elif buy_pct >= 58 and price_change_24h > 0:
            overall_sentiment = f"🟢 **BULLISH** ({buy_pct:.1f}% Buys)"
            embed_color = 65280 # Green
        elif buy_pct <= 45:
            overall_sentiment = f"🔴 **BEARISH** ({buy_pct:.1f}% Buys)"
            embed_color = 16711680 # Red
        else:
            overall_sentiment = f"🟡 **NEUTRAL / CONSOLIDATING** ({buy_pct:.1f}% Buys)"
            embed_color = 16776960 # Yellow

        mcap_formatted = f"${market_cap:,.0f}" if isinstance(market_cap, (int, float)) else f"${market_cap}"
        liq_formatted = f"${liquidity_usd:,.0f}" if isinstance(liquidity_usd, (int, float)) else f"${liquidity_usd}"
        vol_formatted = f"${vol_24h:,.0f}" if isinstance(vol_24h, (int, float)) else f"${vol_24h}"
    else:
        token_name = "Solana Token"
        token_symbol = data.symbol.upper()
        price_usd = "N/A"
        mcap_formatted = "N/A"
        liq_formatted = "N/A"
        vol_formatted = "N/A"
        buys_24h, sells_24h, buys_5m, sells_5m = 0, 0, 0, 0
        overall_sentiment = "⚠️ **NO DEX DATA FOUND**"
        boost_status = "N/A"
        price_change_24h = 0.0
        embed_color = 8421504

    # Build Risk Warning String
    risk_warnings = "\n".join(rug_info["risk_flags"]) if rug_info["risk_flags"] else "✅ No major mint/freeze/bundle flags"

    embed = {
        "embeds": [{
            "title": f"🔍 CA Security & Market Report: {token_name} (${token_symbol})",
            "color": embed_color,
            "fields": [
                {"name": "Contract Address", "value": f"`{ca}`", "inline": False},
                {"name": "Price", "value": f"`${price_usd}` ({price_change_24h:+.2f}%)", "inline": True},
                {"name": "Market Cap", "value": f"`{mcap_formatted}`", "inline": True},
                {"name": "Liquidity", "value": f"`{liq_formatted}`", "inline": True},
                
                # --- NEW SAFETY & HOLDER METRICS ---
                {"name": "👥 Top 10 Holders", "value": f"`{rug_info['top_10_pct']:.1f}%` of Total Supply", "inline": True},
                {"name": "📦 Bundled Launch Status", "value": "🚨 **YES (BUNDLED)**" if rug_info["is_bundled"] else "✅ **Clean / Unbundled**", "inline": True},
                {"name": "👀 DexScreener Activity", "value": boost_status, "inline": True},
                
                # --- RISK & SENTIMENT ---
                {"name": "🚨 Risk Flags & Warnings", "value": risk_warnings, "inline": False},
                {"name": "📊 Market Sentiment", "value": overall_sentiment, "inline": False},
                {"name": "⚡ 5m Volume / Notifier", "value": f"🟢 **{buys_5m}** Buys | 🔴 **{sells_5m}** Sells", "inline": True},
                {"name": "📊 24h Volume / Notifier", "value": f"🟢 **{buys_24h}** Buys | 🔴 **{sells_24h}** Sells\nVol: `{vol_formatted}`", "inline": True},
                
                {
                    "name": "🔗 Verification Links", 
                    "value": f"[RugCheck Report](https://rugcheck.xyz/tokens/{ca}) | [DexScreener](https://dexscreener.com/solana/{ca}) | [Photon](https://photon-sol.tinyastro.io/en/lp/{ca})", 
                    "inline": False
                }
            ],
            "footer": {"text": "Axiom CA Security Engine • RugCheck & Bundle Sniffer Active"}
        }]
    }

    try:
        res = requests.post(WEBHOOK_CA_ANALYST, json=embed, timeout=5)
        if res.status_code in [200, 204]:
            return {"status": "posted_to_ca_channel"}
        else:
            raise HTTPException(status_code=500, detail=f"Discord Webhook Error: {res.status_code}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))