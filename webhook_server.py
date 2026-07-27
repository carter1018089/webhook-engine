import sqlite3
import json
import urllib.parse
import time
import logging
import requests
from openai import OpenAI
from fastapi import FastAPI, Request, BackgroundTasks
import uvicorn

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Axiom Real-Time AI & Chart PnL Engine")

# ==========================================
# HARDCODED INTEGRATIONS
# ==========================================
DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1531132856003330230/I9_fR0OV1k9H3yB4hg8maNDvp0yhs0-lEbrCNJjJlBgx_nCWXllEXKKci_F3Y3RQD_Wx"
GEMINI_API_KEY = "AQ.Ab8RN6JXxf4suKDOHpPF2W7AnKPBeD8m2oS-Jwuq5ICCKhE4cw"

# ==========================================
# MULTI-PROVIDER AI ROTATOR
# ==========================================
class AIRotatorEngine:
    def __init__(self, additional_keys: dict = None):
        self.providers = []

        # TIER 1: Google Gemini (Hardcoded Primary)
        self.providers.append({
            "name": "Google Gemini",
            "client": OpenAI(
                api_key=GEMINI_API_KEY,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
            ),
            "model": "gemini-2.5-flash"
        })

        # TIER 2 & 3: Fallback providers if provided
        if additional_keys:
            if "groq" in additional_keys:
                for k in additional_keys["groq"]:
                    self.providers.append({
                        "name": "Groq LPU",
                        "client": OpenAI(api_key=k, base_url="https://api.groq.com/openai/v1"),
                        "model": "llama-3.3-70b-versatile"
                    })
            if "openrouter" in additional_keys:
                for k in additional_keys["openrouter"]:
                    self.providers.append({
                        "name": "OpenRouter Free",
                        "client": OpenAI(api_key=k, base_url="https://openrouter.ai/api/v1"),
                        "model": "openrouter/free"
                    })

        self.current_index = 0

    def generate_completion(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
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
    """Generates an instant visual bar chart URL using QuickChart REST API."""
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
    
    chart_url = generate_trade_chart_url(symbol, entry_price, exit_price) if exit_price > 0 else None
    dex_url = f"https://dexscreener.com/solana/{mint}" if mint else "https://dexscreener.com"
    solscan_url = f"https://solscan.io/tx/{tx_sig}"

    embed_color = 65280 if realized_pnl >= 0 else 16711680 # Green vs Red

    fields = [
        {"name": "Action", "value": f"`{action}`", "inline": True},
        {"name": "Amount", "value": f"`{token_amount:,.2f} ${symbol}`", "inline": True},
        {"name": "SOL Flow", "value": f"`{sol_amount:.3f} SOL`", "inline": True},
    ]

    if action == "SELL":
        fields.append({"name": "Realized PnL", "value": f"**{realized_pnl:+.3f} SOL** ({roi_percent:+.2f}%)", "inline": True})
        fields.append({"name": "Entry ➔ Exit", "value": f"`{entry_price:.8f}` ➔ `{exit_price:.8f}` SOL", "inline": True})

    fields.append({"name": "🧠 AI Post-Mortem", "value": f"> {ai_insight}", "inline": False})
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
        requests.post(DISCORD_WEBHOOK_URL, json=embed_payload, timeout=5)
    except Exception as e:
        logging.error(f"Failed to post to Discord: {e}")

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

    # Generate AI Post-Mortem
    sys_prompt = "You are a sharp crypto trading analyst. Provide a 2-sentence breakdown of the execution and momentum."
    usr_prompt = f"Action: {tx_type} | Symbol: ${symbol} | Size: {sol_amount:.3f} SOL | Realized PnL: {realized_pnl:+.3f} SOL ({roi_percent:+.2f}%)"
    ai_insight = ai_engine.generate_completion(sys_prompt, usr_prompt)

    # Dispatch Rich Embed
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

if __name__ == "__main__":
    print("🚀 Axiom AI, PnL & Chart Webhook Engine running on port 8000...")
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8000, reload=True)