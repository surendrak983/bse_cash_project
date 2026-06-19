#!/usr/bin/env python3
"""
Download BSE cash market EOD bhavcopy data and store it in SQLite only.
Only rows passing the default screeners are saved:
    last_price > 50
    volume > 50000

Daily use at 9 PM:
    python bse_cash.py

Optional backfill:
    python bse_cash.py --date 14-06-2026
    python bse_cash.py --start 01-06-2026 --end 15-06-2026
"""

from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data"
DB_PATH = DATA_DIR / "bse_cash_eod.db"

DATE_FORMAT = "%d-%m-%Y"
MIN_LTP = 50.0
MIN_VOLUME = 50_000

COLUMN_MAP = {
    "FinInstrmId": "fininstrm_id",
    "TckrSymb": "symbol",
    "ISIN": "isin",
    "OpnPric": "open_price",
    "HghPric": "high_price",
    "LwPric": "low_price",
    "ClsPric": "close_price",
    "LastPric": "last_price",
    "PrvsClsgPric": "prev_close",
    "TtlTradgVol": "volume",
    "TtlTrfVal": "turnover",
    "TtlNbOfTxsExctd": "trades",
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bse_cash_eod (
    trade_date TEXT NOT NULL,
    fininstrm_id INTEGER NOT NULL,
    symbol TEXT,
    isin TEXT,
    open_price REAL,
    high_price REAL,
    low_price REAL,
    close_price REAL,
    last_price REAL,
    prev_close REAL,
    volume INTEGER,
    turnover REAL,
    trades INTEGER,
    downloaded_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, fininstrm_id)
);
"""

UPSERT_SQL = """
INSERT OR REPLACE INTO bse_cash_eod (
    trade_date,
    fininstrm_id,
    symbol,
    isin,
    open_price,
    high_price,
    low_price,
    close_price,
    last_price,
    prev_close,
    volume,
    turnover,
    trades,
    downloaded_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, DATE_FORMAT)


def date_text(value: datetime) -> str:
    return value.strftime(DATE_FORMAT)


def build_url(trade_date: datetime) -> str:
    ymd = trade_date.strftime("%Y%m%d")
    return (
        "https://www.bseindia.com/download/BhavCopy/Equity/"
        f"BhavCopy_BSE_CM_0_0_0_{ymd}_F_0000.CSV"
    )


def create_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bse_cash_eod_symbol "
            "ON bse_cash_eod(symbol);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bse_cash_eod_trade_date "
            "ON bse_cash_eod(trade_date);"
        )


def download_csv(trade_date: datetime) -> str | None:
    url = build_url(trade_date)
    print(f"Downloading {date_text(trade_date)}")
    print(url)

    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "text/csv,*/*",
        },
    )

    try:
        with urlopen(request, timeout=60) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read()
    except HTTPError as exc:
        print(f"No file available: HTTP {exc.code}")
        return None
    except URLError as exc:
        print(f"Download failed: {exc.reason}")
        return None
    except TimeoutError:
        print("Download failed: timed out")
        return None

    text = raw.decode("utf-8-sig", errors="replace")
    if "text/html" in content_type.lower() or text.lstrip().lower().startswith("<!doctype"):
        print("No CSV returned by BSE")
        return None
    return text


def to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))


def to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def normalize_rows(
    csv_text: str,
    trade_date: datetime,
    min_ltp: float,
    min_volume: int,
) -> tuple[list[tuple], int]:
    reader = csv.DictReader(io.StringIO(csv_text))
    missing = [column for column in COLUMN_MAP if column not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"BSE CSV is missing expected columns: {', '.join(missing)}")

    stamp = datetime.now().isoformat(timespec="seconds")
    rows = []
    raw_count = 0
    for raw in reader:
        raw_count += 1
        last_price = to_float(raw.get("LastPric"))
        volume = to_int(raw.get("TtlTradgVol"))
        if last_price is None or volume is None:
            continue
        if last_price <= min_ltp or volume <= min_volume:
            continue

        rows.append(
            (
                date_text(trade_date),
                to_int(raw["FinInstrmId"]),
                raw.get("TckrSymb") or None,
                raw.get("ISIN") or None,
                to_float(raw.get("OpnPric")),
                to_float(raw.get("HghPric")),
                to_float(raw.get("LwPric")),
                to_float(raw.get("ClsPric")),
                last_price,
                to_float(raw.get("PrvsClsgPric")),
                volume,
                to_float(raw.get("TtlTrfVal")),
                to_int(raw.get("TtlNbOfTxsExctd")),
                stamp,
            )
        )
    return rows, raw_count


def save_rows(db_path: Path, trade_date: datetime, rows: list[tuple]) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM bse_cash_eod WHERE trade_date = ?;",
            (date_text(trade_date),),
        )
        if not rows:
            return 0
        cursor = conn.executemany(UPSERT_SQL, rows)
        return cursor.rowcount


def is_weekday(trade_date: datetime) -> bool:
    return trade_date.weekday() < 5


def process_date(
    trade_date: datetime,
    db_path: Path,
    min_ltp: float,
    min_volume: int,
) -> None:
    if not is_weekday(trade_date):
        print(f"Skipping weekend: {date_text(trade_date)}")
        return

    csv_text = download_csv(trade_date)
    if not csv_text:
        return

    rows, raw_count = normalize_rows(csv_text, trade_date, min_ltp, min_volume)
    written = save_rows(db_path, trade_date, rows)
    print(
        f"Screener       : last_price > {min_ltp:g}, "
        f"volume > {min_volume:,}"
    )
    print(f"Rows downloaded: {raw_count:,}")
    print(f"Rows qualified : {len(rows):,}")
    print(f"SQLite database: {db_path}")
    print(f"Rows written   : {written:,}")


def date_range(start: datetime, end: datetime):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Store BSE cash market EOD bhavcopy data in SQLite only."
    )
    parser.add_argument("--date", help="Single date in DD-MM-YYYY format")
    parser.add_argument("--start", help="Range start in DD-MM-YYYY format")
    parser.add_argument("--end", help="Range end in DD-MM-YYYY format")
    parser.add_argument(
        "--min-ltp",
        type=float,
        default=MIN_LTP,
        help=f"Store only rows with LTP greater than this value (default: {MIN_LTP:g})",
    )
    parser.add_argument(
        "--min-volume",
        type=int,
        default=MIN_VOLUME,
        help=(
            "Store only rows with volume greater than this value "
            f"(default: {MIN_VOLUME:,})"
        ),
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help=f"SQLite file path (default: {DB_PATH})",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    create_database(db_path)
    process_kwargs = {
        "db_path": db_path,
        "min_ltp": args.min_ltp,
        "min_volume": args.min_volume,
    }

    try:
        if args.date:
            process_date(parse_date(args.date), **process_kwargs)
        elif args.start and args.end:
            start = parse_date(args.start)
            end = parse_date(args.end)
            if end < start:
                raise ValueError("--end must be on or after --start")
            for trade_date in date_range(start, end):
                process_date(trade_date, **process_kwargs)
        elif args.start or args.end:
            raise ValueError("Use both --start and --end for a date range")
        else:
            process_date(datetime.now(), **process_kwargs)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
