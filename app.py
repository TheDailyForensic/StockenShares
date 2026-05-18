import os
import sqlite3
from flask import Flask, request, jsonify, session
import yfinance as yf
from groq import Groq

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-desk-terminal-key-999")

# Initialize Groq client
# Ensure GROQ_API_KEY is set in your environment variables
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

DB_FILE = "stocksim.db"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Users Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            password TEXT NOT NULL,
            cash REAL NOT NULL
        )
    ''')
    # Positions Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            shares REAL NOT NULL,
            avg_cost REAL NOT NULL,
            raw_token TEXT NOT NULL,
            UNIQUE(user_id, symbol)
        )
    ''')
    # History Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            shares REAL NOT NULL,
            price REAL NOT NULL,
            total_sum REAL NOT NULL,
            realized_pl REAL NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

# Initialize Database Architecture
init_db()

def interpret_asset_query(user_raw_input):
    """
    Leverages Groq AI to process organic English and output clean stock asset info
    """
    try:
        system_prompt = (
            "You are a financial parsing router. Convert the user input into a single financial token "
            "compatible with Yahoo Finance (yfinance). Examples: 'Apple' -> 'AAPL', 'Nvidia' -> 'NVDA', "
            "'Nifty index' -> '^NSEI', 'Reliance' -> 'RELIANCE.NS'. "
            "Respond with ONLY the exact string of the symbol. Do not include spaces, markdown formatting, "
            "punctuation, or extra words."
        )
        completion = groq_client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_raw_input}
            ],
            temperature=0.0,
            max_tokens=10
        )
        return completion.choices[0].message.content.strip()
    except Exception:
        # Emergency raw fallback string assumptions if API drop matches
        cleaned = user_raw_input.strip().upper()
        return cleaned

@app.route('/')
def serve_index():
    # Helper to stream static file via absolute path pointers cleanly
    with open("templates/index.html", "r") as f:
        return f.read()

@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    data = request.json or {}
    username = data.get("username", "").strip().lower()
    display_name = data.get("displayName", "").strip()
    password = data.get("password", "").strip()

    if not username or not display_name or len(password) < 4:
        return jsonify({"error": "Invalid profile entry details. Password must be >= 4 chars."}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, display_name, password, cash) VALUES (?, ?, ?, ?)",
            (username, display_name, password, 10000.0)
        )
        conn.commit()
        # Fetch newly created user ID
        cursor.execute("SELECT id, username, display_name, cash FROM users WHERE username = ?", (username,))
        user = cursor.fetchone()
        session["user_id"] = user["id"]
        return jsonify({"username": user["username"], "displayName": user["display_name"], "cash": user["cash"]})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Operator profile identification handle already registered."}), 400
    finally:
        conn.close()

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.json or {}
    username = data.get("username", "").strip().lower()
    password = data.get("password", "").strip()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ? AND password = ?", (username, password))
    user = cursor.fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "Access Denied. Verification keys mismatch."}), 401

    session["user_id"] = user["id"]
    return jsonify({"username": user["username"], "displayName": user["display_name"], "cash": user["cash"]})

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify({"status": "logged out"})

@app.route('/api/user/portfolio', methods=['GET'])
def get_portfolio():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Unauthorized"}), 401

    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get User Data
    cursor.execute("SELECT cash FROM users WHERE id = ?", (uid,))
    user_row = cursor.fetchone()
    cash = user_row["cash"]

    # Get Active Stock Positions
    cursor.execute("SELECT * FROM positions WHERE user_id = ? AND shares > 0", (uid,))
    db_positions = cursor.fetchall()

    positions = []
    invested_market_value = 0.0

    for pos in db_positions:
        symbol = pos["symbol"]
        shares = pos["shares"]
        avg_cost = pos["avg_cost"]
        raw_token = pos["raw_token"]

        # Fetch Live Current Pricing Quotes from yfinance
        current_price = avg_cost
        try:
            ticker = yf.Ticker(symbol)
            # Use fast_info if available, fallback to history or info mapping
            current_price = ticker.fast_info.last_price
        except Exception:
            try:
                hist = ticker.history(period="1d")
                if not hist.empty:
                    current_price = hist['Close'].iloc[-1]
            except Exception:
                pass

        market_value = shares * current_price
        invested_market_value += market_value
        gain_loss = market_value - (shares * avg_cost)

        positions.append({
            "symbol": symbol,
            "rawToken": raw_token,
            "shares": shares,
            "avgCost": avg_cost,
            "currentPrice": current_price,
            "marketValue": market_value,
            "gainLoss": gain_loss
        })

    net_value = cash + invested_market_value
    returns_pct = ((net_value - 10000.0) / 10000.0) * 100.0

    # Get Historical Records
    cursor.execute("SELECT * FROM history WHERE user_id = ? ORDER BY id DESC", (uid,))
    db_history = cursor.fetchall()
    history_logs = []
    for h in db_history:
        history_logs.append({
            "date": h["timestamp"],
            "type": h["type"],
            "cleanSymbol": h["symbol"],
            "shares": h["shares"],
            "price": h["price"],
            "sum": h["total_sum"],
            "pl": h["realized_pl"]
        })

    conn.close()

    return jsonify({
        "cash": cash,
        "invested": invested_market_value,
        "netValue": net_value,
        "returns": returns_pct,
        "positions": positions,
        "history": history_logs
    })

@app.route('/api/market/query', methods=['GET'])
def query_market():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Unauthorized"}), 401

    query_str = request.args.get("query", "").strip()
    if not query_str:
        return jsonify({"error": "Empty tracking query parameter received."}), 400

    # AI determines ticker matching string symbol sequence
    symbol = interpret_asset_query(query_str)

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        
        live_price = info.last_price
        open_price = info.open if info.open else live_price
        day_high = info.day_high if info.day_high else live_price
        day_low = info.day_low if info.day_low else live_price
        
        change = live_price - open_price
        pct = (change / open_price) * 100.0 if open_price != 0 else 0.0

        # Check user's current holdings for this stock to pass to the client
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT shares FROM positions WHERE user_id = ? AND symbol = ?", (uid, symbol))
        pos_row = cursor.fetchone()
        shares_owned = pos_row["shares"] if pos_row else 0.0
        conn.close()

        return jsonify({
            "symbol": symbol,
            "cleanName": symbol,
            "assetClassDescription": "Global Market Instrument Tracker Quote",
            "price": live_price,
            "change": change,
            "pct": pct,
            "high": day_high,
            "low": day_low,
            "sharesOwned": shares_owned
        })
    except Exception as e:
        return jsonify({"error": f"Asset parsing matrix engine timed out or invalid ticker: {symbol}"}), 404

@app.route('/api/trade/execute', methods=['POST'])
def execute_trade():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    symbol = data.get("symbol", "").strip().upper()
    try:
        qty = float(data.get("qty", 0))
    except ValueError:
        return jsonify({"error": "Invalid volume quantity specified."}), 400
        
    mode = data.get("mode", "").lower() # "buy" or "sell"
    execution_price = float(data.get("price", 0))

    if qty <= 0 or not symbol or mode not in ["buy", "sell"]:
        return jsonify({"error": "Transaction parameter layout structure broken."}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    # Get absolute current balance status lock
    cursor.execute("SELECT cash FROM users WHERE id = ?", (uid,))
    user_row = cursor.fetchone()
    current_cash = user_row["cash"]

    # Get active inventory tracking data for asset
    cursor.execute("SELECT * FROM positions WHERE user_id = ? AND symbol = ?", (uid, symbol))
    position = cursor.fetchone()
    current_shares = position["shares"] if position else 0.0
    current_avg_cost = position["avg_cost"] if position else 0.0

    total_sum = qty * execution_price
    realized_pl = 0.0

    if mode == "buy":
        if current_cash < total_sum:
            conn.close()
            return jsonify({"error": "Insufficient wallet funding assets available."}), 400

        new_cash = current_cash - total_sum
        new_shares = current_shares + qty
        # Calculate new dollar cost average value
        new_avg = ((current_shares * current_avg_cost) + total_sum) / new_shares if new_shares > 0 else 0.0

        if position:
            cursor.execute("UPDATE positions SET shares = ?, avg_cost = ? WHERE id = ?", (new_shares, new_avg, position["id"]))
        else:
            cursor.execute("INSERT INTO positions (user_id, symbol, shares, avg_cost, raw_token) VALUES (?, ?, ?, ?, ?)",
                           (uid, symbol, new_shares, new_avg, symbol))

    else: # SELL operations execution branch
        if current_shares < qty:
            conn.close()
            return jsonify({"error": f"Short sell blocked. Wallet only holds {current_shares} shares."}), 400

        new_cash = current_cash + total_sum
        new_shares = current_shares - qty
        realized_pl = (execution_price - current_avg_cost) * qty

        if new_shares == 0:
            cursor.execute("DELETE FROM positions WHERE id = ?", (position["id"],))
        else:
            cursor.execute("UPDATE positions SET shares = ? WHERE id = ?", (new_shares, position["id"]))

    # Commit updated balances to persistence model
    cursor.execute("UPDATE users SET cash = ? WHERE id = ?", (new_cash, uid))
    
    # Track permanent historical log block ledger entries
    cursor.execute(
        "INSERT INTO history (user_id, type, symbol, shares, price, total_sum, realized_pl) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, mode.upper(), symbol, qty, execution_price, total_sum, realized_pl)
    )

    conn.commit()
    conn.close()
    return jsonify({"status": "success", "newCash": new_cash})

@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, display_name, cash FROM users")
    users = cursor.fetchall()

    leaderboard = []
    for u in users:
        uid = u["id"]
        cursor.execute("SELECT shares, avg_cost, symbol FROM positions WHERE user_id = ?", (uid,))
        positions = cursor.fetchall()

        invested_value = 0.0
        for pos in positions:
            symbol = pos["symbol"]
            shares = pos["shares"]
            
            # Simple caching layer lookups for pricing metrics
            current_price = pos["avg_cost"]
            try:
                current_price = yf.Ticker(symbol).fast_info.last_price
            except:
                pass
            invested_value += (shares * current_price)

        total_net = u["cash"] + invested_value
        returns_pct = ((total_net - 10000.0) / 10000.0) * 100.0

        leaderboard.append({
            "name": u["display_name"],
            "handle": u["username"],
            "cash": u["cash"],
            "returns": returns_pct
        })

    conn.close()
    # Sort descending by high scoreboard returns metric outputs
    leaderboard = sorted(leaderboard, key=lambda x: x["returns"], reverse=True)
    return jsonify(leaderboard)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
