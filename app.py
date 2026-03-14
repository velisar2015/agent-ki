"""
app.py — KINO Agent για Render.com
Τρέχει 24/7 στο cloud, αποθηκεύει δεδομένα, σερβίρει dashboard.
"""

from flask import Flask, jsonify, send_from_directory
import requests
import json
import sqlite3
import os
import threading
import time
import logging
from datetime import datetime, timedelta
from collections import Counter

app = Flask(__name__, static_folder='static')

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kino")

# Render δίνει persistent disk στο /data — αλλιώς tmp
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
DB_PATH  = os.path.join(DATA_DIR, "kino.db")

PREDICT_N = 8
OPAP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Referer': 'https://www.opap.gr/',
    'Origin': 'https://www.opap.gr',
}

# ─── DATABASE ───────────────────────────────────────────────
def init_db():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS draws (
        draw_id INTEGER PRIMARY KEY,
        draw_time TEXT,
        numbers TEXT,
        fetched TEXT
    )""")
    c.commit()
    c.close()

def save_draw(draw_id, draw_time, numbers):
    c = sqlite3.connect(DB_PATH)
    c.execute("INSERT OR IGNORE INTO draws VALUES (?,?,?,?)",
              (draw_id, draw_time, json.dumps(numbers), datetime.now().isoformat()))
    c.commit()
    c.close()

def get_draws(n=500):
    c = sqlite3.connect(DB_PATH)
    rows = c.execute(
        "SELECT draw_id,draw_time,numbers FROM draws ORDER BY draw_id DESC LIMIT ?", (n,)
    ).fetchall()
    c.close()
    return list(reversed([
        {"draw_id": r[0], "draw_time": r[1], "numbers": json.loads(r[2])}
        for r in rows
    ]))

def count_draws():
    c = sqlite3.connect(DB_PATH)
    n = c.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    c.close()
    return n

# ─── OPAP FETCH ─────────────────────────────────────────────
def opap_get(url):
    s = requests.Session()
    s.headers.update(OPAP_HEADERS)
    r = s.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_latest():
    data = opap_get("https://api.opap.gr/draws/v3.0/1100/last-result-and-active")
    last = data.get("last", {})
    did   = last.get("drawId")
    dt    = str(last.get("drawTime", ""))
    nums  = last.get("winningNumbers", {}).get("list", [])
    if did and nums:
        save_draw(did, dt, nums)
        return did
    return None

def fetch_history(days=7):
    loaded = 0
    today = datetime.now().date()
    for d in range(days, -1, -1):
        ds = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        try:
            data = opap_get(f"https://api.opap.gr/draws/v3.0/1100/draw-date/{ds}/{ds}")
            for item in data.get("content", []):
                did  = item.get("drawId")
                nums = item.get("winningNumbers", {}).get("list", [])
                if did and nums:
                    save_draw(did, str(item.get("drawTime", "")), nums)
                    loaded += 1
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"History {ds}: {e}")
    return loaded

# ─── PREDICTION ─────────────────────────────────────────────
def predict(draws, n=PREDICT_N):
    if len(draws) < 20:
        return list(range(1, n + 1))
    recent = draws[-200:]
    freq = Counter(x for d in recent for x in d["numbers"])
    last_seen = {}
    for i, d in enumerate(recent):
        for x in d["numbers"]: last_seen[x] = i
    T = len(recent)
    gaps = {x: T - last_seen.get(x, 0) for x in range(1, 81)}
    mf = max(freq.values()) or 1
    mg = max(gaps.values()) or 1

    N = n * 3
    hot  = sorted(range(1,81), key=lambda x: -freq.get(x,0))[:N]
    cold = sorted(range(1,81), key=lambda x: -gaps.get(x,0))[:N]
    bal  = sorted(range(1,81), key=lambda x: -(0.5*freq.get(x,0)/mf + 0.5*gaps.get(x,0)/mg))[:N]

    # Pair boost
    top_hot = hot[:10]
    pair = Counter()
    for d in draws[-100:]:
        ns = set(d["numbers"])
        for h in top_hot:
            if h in ns:
                for x in d["numbers"]:
                    if x != h: pair[x] += 1
    pair_top = [x for x, _ in pair.most_common(N)]

    votes = Counter()
    for i, x in enumerate(hot):      votes[x] += (N - i) * 1.0
    for i, x in enumerate(cold):     votes[x] += (N - i) * 0.7
    for i, x in enumerate(bal):      votes[x] += (N - i) * 1.3
    for i, x in enumerate(pair_top): votes[x] += (N - i) * 0.9

    return sorted([x for x, _ in votes.most_common(n)])

def eval_stats(draws, sample=200):
    results = []
    indices = range(25, min(len(draws), 25 + sample))
    for i in indices:
        p    = predict(draws[:i])
        hits = len(set(p) & set(draws[i]["numbers"]))
        results.append({"draw_id": draws[i]["draw_id"], "hits": hits, "predicted": p})
    return results

# ─── BACKGROUND LOOP ────────────────────────────────────────
def background_loop():
    log.info("Bootstrap: fetching history...")
    if count_draws() < 50:
        n = fetch_history(7)
        log.info(f"Loaded {n} draws. Total: {count_draws()}")
    while True:
        try:
            did = fetch_latest()
            if did:
                log.info(f"Draw #{did} saved. Total: {count_draws()}")
        except Exception as e:
            log.warning(f"Fetch error: {e}")
        time.sleep(120)

# ─── API ROUTES ─────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/state")
def state():
    draws  = get_draws(500)
    pred   = predict(draws)
    stats  = eval_stats(draws, sample=200)
    last   = draws[-1] if draws else {}
    return jsonify({
        "draws":       draws[-100:],
        "predictions": pred,
        "next_draw_id": (draws[-1]["draw_id"] if draws else 0) + 1,
        "results":     stats[-200:],
        "total":       count_draws(),
        "server_time": datetime.now().isoformat(),
    })

@app.route("/api/latest")
def latest():
    draws = get_draws(1)
    return jsonify(draws[-1] if draws else {})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "draws": count_draws()})

# ─── START ──────────────────────────────────────────────────
# Εκτελείται και με gunicorn (module level)
init_db()
_bg = threading.Thread(target=background_loop, daemon=True)
_bg.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
