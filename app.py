"""
app.py — KINO Agent v3
- Disk storage (/data/kino.db)
- Max ιστορικό (όσο πάει πίσω το OPAP API)
- Διπλή πρόβλεψη: 6 και 8 αριθμοί
- Πλήρη στατιστικά
"""

from flask import Flask, jsonify, send_from_directory
import requests, json, os, threading, time, logging
from datetime import datetime, timedelta
from collections import Counter
import sqlite3

app = Flask(__name__, static_folder='static')
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kino")

DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
DB_PATH  = os.path.join(DATA_DIR, "kino.db")

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
        draw_time TEXT, numbers TEXT, fetched TEXT
    )""")
    c.commit(); c.close()
    log.info(f"DB ready at {DB_PATH}. Total: {count_draws()}")

def save_draw(draw_id, draw_time, numbers):
    c = sqlite3.connect(DB_PATH)
    c.execute("INSERT OR IGNORE INTO draws VALUES (?,?,?,?)",
              (draw_id, draw_time, json.dumps(numbers), datetime.now().isoformat()))
    c.commit(); c.close()

def get_draws(n=9999):
    c = sqlite3.connect(DB_PATH)
    rows = c.execute(
        "SELECT draw_id,draw_time,numbers FROM draws ORDER BY draw_id DESC LIMIT ?", (n,)
    ).fetchall()
    c.close()
    return list(reversed([{"draw_id":r[0],"draw_time":r[1],"numbers":json.loads(r[2])} for r in rows]))

def count_draws():
    c = sqlite3.connect(DB_PATH)
    n = c.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    c.close(); return n

def get_oldest_draw_id():
    c = sqlite3.connect(DB_PATH)
    row = c.execute("SELECT MIN(draw_id) FROM draws").fetchone()
    c.close()
    return row[0] if row and row[0] else None

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
    did  = last.get("drawId")
    dt   = str(last.get("drawTime", ""))
    nums = last.get("winningNumbers", {}).get("list", [])
    if did and nums:
        save_draw(did, dt, nums)
        return did
    return None

def fetch_by_date(date_str):
    """Φέρνει όλες τις κληρώσεις μιας ημέρας."""
    loaded = 0
    try:
        data = opap_get(f"https://api.opap.gr/draws/v3.0/1100/draw-date/{date_str}/{date_str}")
        for item in data.get("content", []):
            did  = item.get("drawId")
            nums = item.get("winningNumbers", {}).get("list", [])
            if did and nums:
                save_draw(did, str(item.get("drawTime", "")), nums)
                loaded += 1
    except Exception as e:
        log.warning(f"fetch_by_date {date_str}: {e}")
    return loaded

def fetch_history_max():
    """Φέρνει όσο ιστορικό γίνεται — αρχίζει από σήμερα και πάει πίσω."""
    today = datetime.now().date()
    total = 0
    consecutive_empty = 0
    d = 0
    while consecutive_empty < 5:  # σταματά όταν 5 μέρες σερί δεν βρει τίποτα
        dt = today - timedelta(days=d)
        ds = dt.strftime("%Y-%m-%d")
        n  = fetch_by_date(ds)
        if n > 0:
            total += n
            consecutive_empty = 0
            log.info(f"  {ds}: +{n} κληρώσεις (σύνολο: {total})")
        else:
            consecutive_empty += 1
        d += 1
        time.sleep(0.2)
        if d > 365:  # max 1 χρόνο πίσω
            break
    return total

# ─── PREDICTION ─────────────────────────────────────────────
def predict(draws, n=8):
    if len(draws) < 20:
        return list(range(1, n+1))
    recent = draws[-300:]  # χρησιμοποιεί τις τελευταίες 300
    freq = Counter(x for d in recent for x in d["numbers"])
    last_seen = {}
    for i, d in enumerate(recent):
        for x in d["numbers"]: last_seen[x] = i
    T = len(recent)
    gaps = {x: T - last_seen.get(x, 0) for x in range(1, 81)}
    mf = max(freq.values()) or 1
    mg = max(gaps.values()) or 1
    N  = n * 4

    hot  = sorted(range(1,81), key=lambda x: -freq.get(x,0))[:N]
    cold = sorted(range(1,81), key=lambda x: -gaps.get(x,0))[:N]
    bal  = sorted(range(1,81), key=lambda x: -(0.5*freq.get(x,0)/mf + 0.5*gaps.get(x,0)/mg))[:N]

    pair = Counter()
    for d in draws[-150:]:
        ns = set(d["numbers"])
        for h in hot[:12]:
            if h in ns:
                for x in d["numbers"]:
                    if x != h: pair[x] += 1
    pair_top = [x for x, _ in pair.most_common(N)]

    votes = Counter()
    for i, x in enumerate(hot):      votes[x] += (N-i) * 1.0
    for i, x in enumerate(cold):     votes[x] += (N-i) * 0.7
    for i, x in enumerate(bal):      votes[x] += (N-i) * 1.3
    for i, x in enumerate(pair_top): votes[x] += (N-i) * 0.9

    return sorted([x for x, _ in votes.most_common(n)])

def eval_stats(draws, n_pred=8, sample=300):
    """Αξιολογεί την απόδοση για N αριθμούς."""
    results = []
    indices = range(30, min(len(draws), 30 + sample))
    for i in indices:
        p    = predict(draws[:i], n=n_pred)
        hits = len(set(p) & set(draws[i]["numbers"]))
        results.append({
            "draw_id":   draws[i]["draw_id"],
            "hits":      hits,
            "predicted": p,
        })
    return results

def hits_distribution(stats):
    """Κατανομή hits 0,1,2,3,4+"""
    dist = {0:0, 1:0, 2:0, 3:0, 4:0}
    for r in stats:
        k = min(r["hits"], 4)
        dist[k] += 1
    total = len(stats) or 1
    return {k: round(v/total*100, 1) for k,v in dist.items()}

# ─── BACKGROUND LOOP ────────────────────────────────────────
_bootstrapped = False

def background_loop():
    global _bootstrapped
    log.info("=== Bootstrap start ===")
    try:
        current = count_draws()
        if current < 100:
            log.info("Fetching max history...")
            n = fetch_history_max()
            log.info(f"Bootstrap done: {n} new draws. Total: {count_draws()}")
        else:
            log.info(f"Already have {current} draws, skipping full bootstrap.")
            # Φέρνει τις τελευταίες 7 μέρες για να ενημερωθεί
            today = datetime.now().date()
            for d in range(7, -1, -1):
                ds = (today - timedelta(days=d)).strftime("%Y-%m-%d")
                fetch_by_date(ds)
                time.sleep(0.2)
    except Exception as e:
        log.error(f"Bootstrap error: {e}")
    _bootstrapped = True

    while True:
        try:
            did = fetch_latest()
            if did:
                log.info(f"New draw #{did}. Total: {count_draws()}")
        except Exception as e:
            log.warning(f"Fetch error: {e}")
        time.sleep(120)

# ─── API ────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/state")
def state():
    draws  = get_draws()
    pred6  = predict(draws, n=6)
    pred8  = predict(draws, n=8)
    stats6 = eval_stats(draws, n_pred=6, sample=300)
    stats8 = eval_stats(draws, n_pred=8, sample=300)
    last   = draws[-1] if draws else {}
    next_id = (last.get("draw_id") or 0) + 1

    def summary(stats):
        if not stats: return {}
        hits = [r["hits"] for r in stats]
        total = len(hits)
        return {
            "n":        total,
            "avg":      round(sum(hits)/total, 3),
            "max":      max(hits),
            "pct1":     round(sum(1 for h in hits if h>=1)/total*100, 1),
            "pct2":     round(sum(1 for h in hits if h>=2)/total*100, 1),
            "pct3":     round(sum(1 for h in hits if h>=3)/total*100, 1),
            "pct4":     round(sum(1 for h in hits if h>=4)/total*100, 1),
            "dist":     hits_distribution(stats),
            "recent":   stats[-50:],
        }

    return jsonify({
        "draws":        draws[-100:],
        "total_draws":  count_draws(),
        "next_draw_id": next_id,
        "bootstrapped": _bootstrapped,
        "pred6":        pred6,
        "pred8":        pred8,
        "stats6":       summary(stats6),
        "stats8":       summary(stats8),
        "server_time":  datetime.now().isoformat(),
    })

@app.route("/health")
def health():
    return jsonify({
        "status":       "ok",
        "draws":        count_draws(),
        "bootstrapped": _bootstrapped,
        "db_path":      DB_PATH,
    })

# ─── START ──────────────────────────────────────────────────
init_db()
threading.Thread(target=background_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
