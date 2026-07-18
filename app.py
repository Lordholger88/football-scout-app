"""
Scout Index — local app server
--------------------------------
Serves a REST API + dark-themed frontend over the real, open
"transfermarkt-datasets" project (https://github.com/dcaribou/transfermarkt-datasets),
which legitimately scrapes and republishes Transfermarkt data on a weekly cadence.

On first run this script downloads four CSV files (players, market value
history, transfers, clubs) from that project's public data bucket into
./data/, then loads them into an in-memory DuckDB and serves everything
from http://localhost:5000.

No API key. No cost. Runs entirely on your machine after the first download.
"""

import os
import sys
import urllib.request
from pathlib import Path

import duckdb
from flask import Flask, jsonify, request, render_template

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

BASE_URL = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/"
FILES = ["players.csv.gz", "player_valuations.csv.gz", "transfers.csv.gz", "clubs.csv.gz"]


def download_if_missing():
    for fname in FILES:
        dest = DATA_DIR / fname
        if dest.exists() and dest.stat().st_size > 0:
            continue
        url = BASE_URL + fname
        print(f"Downloading {fname} ...")

        # Create a custom request containing a User-Agent header to prevent 403 Forbidden errors
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )

        try:
            # Open the URL and write out the chunks manually
            with urllib.request.urlopen(req) as response, open(dest, 'wb') as out_file:
                total_size = int(response.info().get('Content-Length', 0))
                done = 0
                block_size = 1024 * 64  # 64 KB chunks

                while True:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    done += len(buffer)
                    out_file.write(buffer)
                    
                    if total_size > 0:
                        pct = min(100, done * 100 // total_size)
                        sys.stdout.write(f"\r  {fname}: {pct}% ({done // 1_000_000}MB / {total_size // 1_000_000}MB)")
                        sys.stdout.flush()
            print()
        except Exception as e:
            print(f"\nFailed to download {fname}: {e}")
            print("Check your internet connection and try again. If this keeps failing, the")
            print("dataset's hosting may have moved — check https://github.com/dcaribou/transfermarkt-datasets")
            sys.exit(1)


download_if_missing()

print("Loading data into DuckDB (this takes a few seconds)...")
con = duckdb.connect(database=":memory:")
con.execute(f"CREATE TABLE players AS SELECT * FROM read_csv_auto('{DATA_DIR / 'players.csv.gz'}')")
con.execute(f"CREATE TABLE valuations AS SELECT * FROM read_csv_auto('{DATA_DIR / 'player_valuations.csv.gz'}')")
con.execute(f"CREATE TABLE transfers AS SELECT * FROM read_csv_auto('{DATA_DIR / 'transfers.csv.gz'}')")
con.execute(f"CREATE TABLE clubs AS SELECT * FROM read_csv_auto('{DATA_DIR / 'clubs.csv.gz'}')")
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