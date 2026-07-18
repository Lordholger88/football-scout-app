"""
trimmer.py — build a small, Vercel-friendly dataset from the full transfermarkt-datasets CSVs.

Run this ONCE, locally, after app.py has already done its normal full download into ./data/.
It reads the full players / valuations / transfers / clubs files and writes trimmed versions
to ./data_trimmed/ containing:

  - the top N players by current market value (default 10,000)
  - their COMPLETE market value history (every valuation date, not trimmed) — the graph
    needs the full time series for each kept player, so only the player list is trimmed,
    not each player's history
  - their complete transfer history
  - every club referenced by those players or their transfers

Output is Parquet (columnar, compact, fast to load — DuckDB reads it natively with no
CSV parsing overhead).

Usage:
    python trimmer.py                  # top 10,000 players (default)
    python trimmer.py --top 5000       # a different cutoff
    python trimmer.py --source data --out data_trimmed
"""

import argparse
import sys
from pathlib import Path

import duckdb

DEFAULT_SOURCE = Path(__file__).parent / "data"
DEFAULT_OUT = Path(__file__).parent / "data_trimmed"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top", type=int, default=10000, help="Number of top players to keep, by market value (default: 10000)")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Folder containing the full players/valuations/transfers/clubs .csv.gz files")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Folder to write trimmed .parquet files to")
    args = parser.parse_args()

    players_src = args.source / "players.csv.gz"
    valuations_src = args.source / "player_valuations.csv.gz"
    transfers_src = args.source / "transfers.csv.gz"
    clubs_src = args.source / "clubs.csv.gz"

    missing = [f for f in [players_src, valuations_src, transfers_src, clubs_src] if not f.exists()]
    if missing:
        print("Missing source file(s):")
        for f in missing:
            print(f"  {f}")
        print("\nRun `python app.py` once first and let the initial download finish, then run this script.")
        sys.exit(1)

    args.out.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    print(f"Reading full dataset from {args.source}/ ...")
    con.execute(f"CREATE TABLE players AS SELECT * FROM read_csv_auto('{players_src}')")
    con.execute(f"CREATE TABLE valuations AS SELECT * FROM read_csv_auto('{valuations_src}')")
    con.execute(f"CREATE TABLE transfers AS SELECT * FROM read_csv_auto('{transfers_src}')")
    con.execute(f"CREATE TABLE clubs AS SELECT * FROM read_csv_auto('{clubs_src}')")

    # IMPORTANT: each CSV above gets its column types independently inferred by DuckDB.
    # If e.g. players.player_id comes out as BIGINT but valuations.player_id comes out as
    # VARCHAR (easy to happen with auto-inference across separate files), every JOIN below
    # silently returns zero matching rows — no error, just an empty result. That produces
    # exactly "no graph, no transfer history" with no clue why. Force them all to the same
    # type up front so the joins are guaranteed to work.
    print("Normalizing id column types before joining (players/valuations/transfers/clubs)...")
    id_columns = {
        "players": ["player_id", "current_club_id"],
        "valuations": ["player_id", "current_club_id"],
        "transfers": ["player_id", "from_club_id", "to_club_id"],
        "clubs": ["club_id"],
    }
    for table, cols in id_columns.items():
        existing_cols = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
        for col in cols:
            if col in existing_cols:
                con.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE BIGINT USING TRY_CAST({col} AS BIGINT)")

    total = con.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    print(f"Full dataset has {total:,} players. Trimming to top {args.top:,} by market value...\n")

    con.execute(f"""
        CREATE TABLE top_players AS
        SELECT * FROM players
        WHERE market_value_in_eur IS NOT NULL
        ORDER BY market_value_in_eur DESC
        LIMIT {args.top}
    """)
    kept = con.execute("SELECT COUNT(*) FROM top_players").fetchone()[0]
    lowest = con.execute("SELECT MIN(market_value_in_eur) FROM top_players").fetchone()[0]
    print(f"  Players kept:        {kept:,}  (cutoff market value: €{lowest/1_000_000:.1f}m)")

    con.execute("""
        CREATE TABLE top_valuations AS
        SELECT v.* FROM valuations v
        JOIN top_players p ON p.player_id = v.player_id
    """)
    vcount = con.execute("SELECT COUNT(*) FROM top_valuations").fetchone()[0]
    print(f"  Value-history rows:  {vcount:,}  (full history per kept player — this is the graph data)")
    if vcount < kept:
        print("  ⚠️  WARNING: fewer value-history rows than kept players — something's off with the join")
        print("      or the source data. Spot-check a known player_id in both players.csv.gz and")
        print("      player_valuations.csv.gz to confirm the id values actually line up.")

    con.execute("""
        CREATE TABLE top_transfers AS
        SELECT t.* FROM transfers t
        JOIN top_players p ON p.player_id = t.player_id
    """)
    tcount = con.execute("SELECT COUNT(*) FROM top_transfers").fetchone()[0]
    print(f"  Transfer records:    {tcount:,}")
    if tcount == 0:
        print("  ⚠️  WARNING: zero transfer records matched. Not necessarily a bug — some players")
        print("      genuinely have no recorded transfers — but if this is 0 for the WHOLE top")
        print("      10,000, that's a join problem worth double-checking.")

    con.execute("""
        CREATE TABLE relevant_club_ids AS
        SELECT DISTINCT current_club_id AS club_id FROM top_players WHERE current_club_id IS NOT NULL
        UNION
        SELECT DISTINCT from_club_id AS club_id FROM top_transfers WHERE from_club_id IS NOT NULL
        UNION
        SELECT DISTINCT to_club_id AS club_id FROM top_transfers WHERE to_club_id IS NOT NULL
    """)
    con.execute("""
        CREATE TABLE top_clubs AS
        SELECT c.* FROM clubs c
        JOIN relevant_club_ids r ON r.club_id = c.club_id
    """)
    ccount = con.execute("SELECT COUNT(*) FROM top_clubs").fetchone()[0]
    print(f"  Clubs kept:          {ccount:,}")

    print(f"\nWriting trimmed parquet files to {args.out}/ ...")
    con.execute(f"COPY top_players TO '{args.out / 'players.parquet'}' (FORMAT PARQUET)")
    con.execute(f"COPY top_valuations TO '{args.out / 'player_valuations.parquet'}' (FORMAT PARQUET)")
    con.execute(f"COPY top_transfers TO '{args.out / 'transfers.parquet'}' (FORMAT PARQUET)")
    con.execute(f"COPY top_clubs TO '{args.out / 'clubs.parquet'}' (FORMAT PARQUET)")

    total_size = sum(f.stat().st_size for f in args.out.glob("*.parquet"))
    print(f"\nDone. Combined size: {total_size / 1_000_000:.1f} MB")
    print(f"Trimmed files are in {args.out}/")
    print("app.py already knows to load from data_trimmed/ automatically if it finds files there —")
    print("commit that folder to your repo and deploy. No download step will run on the server.")


if __name__ == "__main__":
    main()
