# BSE Cash EOD SQLite Loader

This compact module downloads the BSE cash market EOD bhavcopy and stores screened rows in SQLite only.

Default screeners:

```text
last_price > 50
volume > 50000
```

## Daily run

Run this at 9 PM:

```powershell
python bse_cash.py
```

The default database is:

```text
Data\bse_cash_eod.db
```

## Backfill examples

```powershell
python bse_cash.py --date 14-06-2026
python bse_cash.py --start 01-06-2026 --end 15-06-2026
```

## Screener overrides

```powershell
python bse_cash.py --min-ltp 100 --min-volume 100000
```

## Windows Task Scheduler

Use `run_daily.bat` as the scheduled action. Set the task time to 9:00 PM.
