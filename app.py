"""
Scout Index — app server
--------------------------
Serves a REST API + dark-themed frontend over the real, open
"transfermarkt-datasets" project (https://github.com/dcaribou/transfermarkt-datasets),
which legitimately scrapes and republishes Transfermarkt data on a weekly cadence.

Two data modes, auto-detected at startup:

  1. TRIMMED (Vercel-friendly): if ./data_trimmed/*.parquet exists (built with
     trimmer.py), those files are loaded directly. No network call, no download —
     safe for serverless cold starts. This is what you deploy.

  2. FULL (local dev): otherwise, downloads the full dataset (players, market
     value history, transfers, clubs) into ./data/ on first run, same as before.
     Use this locally to build the trimmed dataset via trimmer.py.
"""

import os
from datetime import date
from pathlib import Path

import duckdb
import requests
from flask import Flask, jsonify, request, render_template

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = "llama-3.3-70b-versatile"

ROOT = Path(__file__).parent
TRIMMED_DIR = ROOT / "data_trimmed"
FULL_DIR = ROOT / "data"

FILES = ["players", "player_valuations", "transfers", "clubs"]
TABLE_NAMES = {"players": "players", "player_valuations": "valuations", "transfers": "transfers", "clubs": "clubs"}

con = duckdb.connect(database=":memory:")

trimmed_files_present = all((TRIMMED_DIR / f"{f}.parquet").exists() for f in FILES)

if trimmed_files_present:
    print(f"Loading trimmed dataset from {TRIMMED_DIR}/ (no download needed)...")
    for fname in FILES:
        table = TABLE_NAMES[fname]
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet('{TRIMMED_DIR / (fname + '.parquet')}')")
else:
    import sys
    import urllib.request

    print("No trimmed dataset found — falling back to full download mode.")
    print("(Run trimmer.py after this finishes to build a small, deployable dataset.)")
    FULL_DIR.mkdir(exist_ok=True)
    BASE_URL = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/"

    def download_if_missing():
        for fname in FILES:
            dest = FULL_DIR / f"{fname}.csv.gz"
            if dest.exists() and dest.stat().st_size > 0:
                continue
            url = BASE_URL + dest.name
            print(f"Downloading {dest.name} ...")

            def _progress(block_num, block_size, total_size):
                if total_size <= 0:
                    return
                done = block_num * block_size
                pct = min(100, done * 100 // total_size)
                sys.stdout.write(f"\r  {dest.name}: {pct}% ({done // 1_000_000}MB / {total_size // 1_000_000}MB)")
                sys.stdout.flush()

            try:
                urllib.request.urlretrieve(url, dest, _progress)
                print()
            except Exception as e:
                print(f"\nFailed to download {dest.name}: {e}")
                print("Check your internet connection and try again. If this keeps failing, the")
                print("dataset's hosting may have moved — check https://github.com/dcaribou/transfermarkt-datasets")
                sys.exit(1)

    download_if_missing()
    print("Loading data into DuckDB (this takes a few seconds)...")
    for fname in FILES:
        table = TABLE_NAMES[fname]
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto('{FULL_DIR / (fname + '.csv.gz')}')")

player_count = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
print(f"Loaded {player_count:,} players. Starting server...")

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def stats():
    row = con.execute("""
        SELECT
            (SELECT COUNT(*) FROM players) AS total_players,
            (SELECT COUNT(DISTINCT country_of_citizenship) FROM players WHERE country_of_citizenship IS NOT NULL) AS total_nationalities,
            (SELECT COUNT(*) FROM players WHERE country_of_citizenship = 'France') AS france_count,
            (SELECT COUNT(*) FROM players WHERE market_value_in_eur IS NOT NULL) AS valued_players
    """).fetchone()
    return jsonify({
        "total_players": row[0],
        "total_nationalities": row[1],
        "france_count": row[2],
        "valued_players": row[3],
    })


@app.route("/api/players")
def list_players():
    """Paginated / searchable / sortable player list."""
    search = request.args.get("q", "").strip()
    position = request.args.get("position", "").strip()
    nationality = request.args.get("nationality", "").strip()
    sort = request.args.get("sort", "value_desc")
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = int(request.args.get("offset", 0))

    where = ["market_value_in_eur IS NOT NULL"]
    params = []
    if search:
        where.append("(LOWER(name) LIKE ? OR LOWER(current_club_name) LIKE ? OR LOWER(country_of_citizenship) LIKE ?)")
        like = f"%{search.lower()}%"
        params += [like, like, like]
    if position:
        where.append("position = ?")
        params.append(position)
    if nationality:
        where.append("country_of_citizenship = ?")
        params.append(nationality)

    order_map = {
        "value_desc": "market_value_in_eur DESC NULLS LAST",
        "value_asc": "market_value_in_eur ASC NULLS LAST",
        "name": "name ASC",
        "age_asc": "date_of_birth DESC NULLS LAST",
        "age_desc": "date_of_birth ASC NULLS LAST",
    }
    order_by = order_map.get(sort, order_map["value_desc"])

    where_clause = " AND ".join(where)
    query = f"""
        SELECT
            player_id, name, current_club_name, country_of_citizenship,
            position, sub_position, market_value_in_eur, highest_market_value_in_eur,
            date_of_birth, image_url
        FROM players
        WHERE {where_clause}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """
    rows = con.execute(query, params + [limit, offset]).fetchall()
    cols = [d[0] for d in con.description]
    results = [dict(zip(cols, r)) for r in rows]
    for r in results:
        r["date_of_birth"] = str(r["date_of_birth"]) if r["date_of_birth"] else None

    total = con.execute(f"SELECT COUNT(*) FROM players WHERE {where_clause}", params).fetchone()[0]
    return jsonify({"results": results, "total": total})


@app.route("/api/filters")
def filters():
    positions = [r[0] for r in con.execute(
        "SELECT DISTINCT position FROM players WHERE position IS NOT NULL ORDER BY 1"
    ).fetchall()]
    nationalities = [r[0] for r in con.execute(
        "SELECT country_of_citizenship, COUNT(*) c FROM players WHERE country_of_citizenship IS NOT NULL "
        "GROUP BY 1 ORDER BY c DESC LIMIT 60"
    ).fetchall()]
    return jsonify({"positions": positions, "nationalities": nationalities})


@app.route("/api/player/<int:player_id>")
def player_detail(player_id):
    row = con.execute("""
        SELECT player_id, name, first_name, last_name, current_club_name,
               country_of_citizenship, country_of_birth, city_of_birth,
               date_of_birth, position, sub_position, foot, height_in_cm,
               market_value_in_eur, highest_market_value_in_eur,
               agent_name, contract_expiration_date, image_url, url
        FROM players WHERE player_id = ?
    """, [player_id]).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    cols = [d[0] for d in con.description]
    data = dict(zip(cols, row))
    data["date_of_birth"] = str(data["date_of_birth"]) if data["date_of_birth"] else None
    data["contract_expiration_date"] = str(data["contract_expiration_date"]) if data["contract_expiration_date"] else None
    return jsonify(data)


@app.route("/api/player/<int:player_id>/value-history")
def value_history(player_id):
    rows = con.execute("""
        SELECT date, market_value_in_eur
        FROM valuations
        WHERE player_id = ?
        ORDER BY date ASC
    """, [player_id]).fetchall()
    return jsonify([{"date": str(r[0]), "value": r[1]} for r in rows])


@app.route("/api/player/<int:player_id>/transfers")
def player_transfers(player_id):
    rows = con.execute("""
        SELECT transfer_date, transfer_season, from_club_name, to_club_name,
               transfer_fee, market_value_in_eur
        FROM transfers
        WHERE player_id = ?
        ORDER BY transfer_date DESC
    """, [player_id]).fetchall()
    return jsonify([{
        "date": str(r[0]), "season": r[1], "from_club": r[2], "to_club": r[3],
        "fee": r[4], "value_at_time": r[5]
    } for r in rows])


@app.route("/api/ask", methods=["POST"])
def ask_ai():
    """Free-text Q&A over the top 1000 most valuable players, via Groq (free tier)."""
    body = request.get_json(force=True, silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    if not GROQ_API_KEY:
        return jsonify({
            "error": "AI isn't configured on the server yet — set the GROQ_API_KEY environment variable "
                     "(get a free key at console.groq.com/keys) and redeploy."
        }), 503

    rows = con.execute("""
        SELECT name, position, current_club_name, country_of_citizenship,
               market_value_in_eur, date_of_birth
        FROM players
        WHERE market_value_in_eur IS NOT NULL
        ORDER BY market_value_in_eur DESC
        LIMIT 1000
    """).fetchall()

    today = date.today()
    lines = []
    for name, pos, club, nat, val, dob in rows:
        age = "-"
        if dob:
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        lines.append(f"{name} | {pos or '-'} | {club or '-'} | {nat or '-'} | age {age} | €{(val or 0)/1_000_000:.0f}m")
    dataset_text = "\n".join(lines)

    system_prompt = (
        "You are a football transfer market analyst. Answer the question using ONLY the dataset below "
        "(the current top 1000 most valuable players, one per line: name | position | club | nationality | "
        "age | market value). Be precise and concise, show your counting when relevant, and if the question "
        "can't be answered from this data say so plainly.\n\nDATASET:\n" + dataset_text
    )

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question},
                ],
                "max_tokens": 500,
                "temperature": 0.2,
            },
            timeout=25,
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        return jsonify({"answer": answer})
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Could not reach the AI service: {e}"}), 502


@app.route("/api/club/<int:club_id>")
def club_detail(club_id):
    row = con.execute("SELECT * FROM clubs WHERE club_id = ?", [club_id]).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    cols = [d[0] for d in con.description]
    return jsonify(dict(zip(cols, row)))


if __name__ == "__main__":
    print("\nOpen http://localhost:5000 in your browser.\n")
    app.run(debug=False, port=5000)
