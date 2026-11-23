# kr-market-data

Korean equities daily OHLCV (adjusted), collected by GitHub Actions. Market data only.

- Universe: KOSPI top 200 + KOSDAQ top 150 by market cap (ETF/ETN, preferred shares, SPACs, REITs excluded). Once added, kept; new names monthly. Current-constituent universe → survivorship bias.
- History: 2005-01 onward. Indices: KOSPI, KOSDAQ.
- Source: Naver Finance public chart endpoints (no login). If unreachable, the run records `probe.json` and keeps previous data.
- Schedule: weekdays 16:47 KST. Full re-download each run (adjustments for splits/rights issues).

Data is on the `data` branch:

```
universe.csv                 code, name, market, rank, market_cap_eok, added
1d/<code>.parquet            date, open, high, low, close, volume
index/KOSPI.parquet, index/KOSDAQ.parquet
meta.json, probe.json
```

Raw file: `https://raw.githubusercontent.com/sungholee0221-ops/kr-market-data/data/1d/005930.parquet`

Prices from Naver Finance; for personal research. No investment advice.
