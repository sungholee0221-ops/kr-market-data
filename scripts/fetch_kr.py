"""Korean equities daily OHLCV (adjusted) for KOSPI top 200 + KOSDAQ top 150 by market cap, plus KOSPI/KOSDAQ indices.

Runs in GitHub Actions. Source: Naver Finance public chart/listing endpoints (no login). Tried in order, first that works:
  universe : m.stock.naver.com/api/stocks/marketValue/{KOSPI|KOSDAQ}  →  finance.naver.com/sise/sise_market_sum.naver (HTML)
  daily    : fchart.stock.naver.com/sise.nhn (XML, adjusted)  →  api.stock.naver.com/chart/domestic/item/{code}/day (JSON)
If nothing answers, the run writes probe.json and keeps previous data: nothing is circumvented.

Layout written to $OUT_DIR (published as the `data` branch):
  universe.csv            code, name, market, rank, market_cap_eok, added (KST date)
  1d/<code>.parquet       date, open, high, low, close, volume
  index/KOSPI.parquet, index/KOSDAQ.parquet
  meta.json, probe.json
Full re-download of daily history every run (adjusted prices can change after splits/rights issues).
Universe: once added, kept. New names are added only when the month changes (and on the first run).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

OUT = Path(os.environ.get("OUT_DIR", "out"))
PREV = Path(os.environ.get("PREV_DIR", "prev"))
TOP = {"KOSPI": int(os.environ.get("TOP_KOSPI", "200")), "KOSDAQ": int(os.environ.get("TOP_KOSDAQ", "150"))}
START = os.environ.get("START", "2005-01-01")
COUNT = 7000  # trading days requested from fchart (≈ 28 years, more than START needs)
KST = timezone(timedelta(hours=9))
EX_NAME = re.compile(r"(스팩|리츠|REIT|인프라|우$|우B$|우C$|\d우$|우\(전환\)$)")
ETF_PREFIX = ("KODEX", "TIGER", "KBSTAR", "RISE", "ACE", "SOL", "HANARO", "ARIRANG", "KOSEF", "PLUS", "1Q", "TIME", "KIWOOM",
              "BNK", "WON", "TREX", "FOCUS", "마이다스", "히어로즈", "에셋플러스", "UNICORN", "파워", "TRUSTON", "DAISHIN343", "VITA")
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (kr-market-data; GitHub Actions)", "Referer": "https://finance.naver.com/"})


def now_kst() -> datetime:
    return datetime.now(KST)


# ------------------------------------------------------------------ universe
def _num(x) -> float:
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return float("nan")


def universe_mobile(market: str, n: int) -> list[dict]:
    rows, page = [], 1
    while len(rows) < n * 2 and page <= 20:
        r = S.get(f"https://m.stock.naver.com/api/stocks/marketValue/{market}", params={"page": page, "pageSize": 100},
                  timeout=20)
        r.raise_for_status()
        js = r.json()
        st = js.get("stocks") or []
        if not st:
            break
        for s in st:
            rows.append({"code": s.get("itemCode"), "name": s.get("stockName"), "type": s.get("stockEndType", "stock"),
                         "market_cap_eok": _num(s.get("marketValue"))})
        page += 1
    return rows


def universe_html(market: str, n: int) -> list[dict]:
    sosok = 0 if market == "KOSPI" else 1
    rows, page = [], 1
    while len(rows) < n * 2 and page <= 20:
        r = S.get("https://finance.naver.com/sise/sise_market_sum.naver", params={"sosok": sosok, "page": page}, timeout=20)
        r.raise_for_status()
        html = r.content.decode("euc-kr", "replace")
        found = re.findall(r'code=(\w{6})" class="tltle">([^<]+)</a>', html)
        if not found:
            break
        rows += [{"code": c, "name": nm.strip(), "type": "stock", "market_cap_eok": float("nan")} for c, nm in found]
        page += 1
    return rows


def pick(rows: list[dict], n: int) -> list[dict]:
    out = []
    for s in rows:
        code, name = s["code"] or "", s["name"] or ""
        if s["type"] != "stock" or not re.fullmatch(r"\d{5}0", code) or EX_NAME.search(name) or name.startswith(ETF_PREFIX):
            continue
        out.append(s)
        if len(out) >= n:
            break
    return out


def build_universe(probe: dict) -> pd.DataFrame | None:
    today = now_kst().strftime("%Y-%m-%d")
    prev_u = pd.read_csv(PREV / "universe.csv", dtype={"code": str}) if (PREV / "universe.csv").exists() else None
    prev_meta = json.loads((PREV / "meta.json").read_text()) if (PREV / "meta.json").exists() else {}
    month = now_kst().strftime("%Y-%m")
    if prev_u is not None and prev_meta.get("universe_month") == month:
        return prev_u
    fresh = []
    for mkt, n in TOP.items():
        got = None
        for fn in (universe_mobile, universe_html):
            try:
                got = pick(fn(mkt, n), n)
                probe[f"universe_{mkt}_{fn.__name__}"] = len(got)
                if len(got) >= n * 0.9:
                    break
            except Exception as e:  # noqa: BLE001
                probe[f"universe_{mkt}_{fn.__name__}"] = str(e)[:200]
        for i, s in enumerate(got or []):
            fresh.append({"code": s["code"], "name": s["name"], "market": mkt, "rank": i + 1,
                          "market_cap_eok": s["market_cap_eok"], "added": today})
    if not fresh:
        return prev_u
    f = pd.DataFrame(fresh)
    if prev_u is None:
        return f
    keep = prev_u.set_index("code")
    for r in f.itertuples():
        if r.code in keep.index:
            keep.loc[r.code, ["name", "rank", "market_cap_eok"]] = [r.name, r.rank, r.market_cap_eok]
    new = f[~f["code"].isin(keep.index)]
    return pd.concat([keep.reset_index(), new], ignore_index=True)


# ------------------------------------------------------------------ daily
def daily_fchart(symbol: str) -> pd.DataFrame:
    r = S.get("https://fchart.stock.naver.com/sise.nhn",
              params={"symbol": symbol, "timeframe": "day", "count": COUNT, "requestType": 0}, timeout=30)
    r.raise_for_status()
    items = re.findall(r'data="([^"]+)"', r.text)
    rows = [x.split("|") for x in items]
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df


def daily_api(symbol: str) -> pd.DataFrame:
    end = now_kst().strftime("%Y%m%d") + "2359"
    kind = "index" if symbol in ("KOSPI", "KOSDAQ") else "item"
    r = S.get(f"https://api.stock.naver.com/chart/domestic/{kind}/{symbol}/day",
              params={"startDateTime": START.replace("-", "") + "0000", "endDateTime": end}, timeout=30)
    r.raise_for_status()
    js = r.json()
    df = pd.DataFrame([{"date": x["localDate"], "open": x["openPrice"], "high": x["highPrice"], "low": x["lowPrice"],
                        "close": x["closePrice"], "volume": x.get("accumulatedTradingVolume", 0)} for x in js])
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df


def clean(df: pd.DataFrame) -> pd.DataFrame:
    for k in ("open", "high", "low", "close", "volume"):
        df[k] = pd.to_numeric(df[k], errors="coerce").astype(float)
    df = df[(df["date"] >= START) & (df["close"] > 0)].dropna(subset=["close"]).copy()
    # 거래 정지·옛 데이터의 0 시가/고가/저가 → 종가로
    for k in ("open", "high", "low"):
        df.loc[df[k] <= 0, k] = df.loc[df[k] <= 0, "close"]
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "high", "low", "close"]].min(axis=1)
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def fetch(symbol: str, order: list) -> tuple[pd.DataFrame | None, str]:
    err = ""
    for fn in order:
        try:
            df = clean(fn(symbol))
            if len(df):
                return df, fn.__name__
        except Exception as e:  # noqa: BLE001
            err = f"{fn.__name__}: {str(e)[:120]}"
    return None, err


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    probe: dict = {"run_kst": now_kst().isoformat(timespec="seconds")}
    for name, fn in (("fchart", daily_fchart), ("api_stock", daily_api)):
        try:
            d = fn("005930")
            probe[f"daily_{name}"] = {"ok": True, "rows": len(d), "first": str(d["date"].min().date()),
                                      "last": str(d["date"].max().date())}
        except Exception as e:  # noqa: BLE001
            probe[f"daily_{name}"] = {"ok": False, "error": str(e)[:200]}
    order = [fn for name, fn in (("fchart", daily_fchart), ("api_stock", daily_api)) if probe[f"daily_{name}"]["ok"]]
    u = build_universe(probe)
    (OUT / "probe.json").write_text(json.dumps(probe, ensure_ascii=False, indent=1))
    if not order or u is None:
        # nothing reachable: keep previous data as is
        if PREV.exists():
            for p in PREV.iterdir():
                if p.name not in (".git", "probe.json"):
                    (shutil.copytree if p.is_dir() else shutil.copy2)(p, OUT / p.name)
        print("no source reachable", probe)
        return 0
    (OUT / "1d").mkdir(exist_ok=True)
    (OUT / "index").mkdir(exist_ok=True)
    fails, src_count = {}, {}
    for sym in ("KOSPI", "KOSDAQ"):
        df, src = fetch(sym, order)
        if df is None:
            fails[sym] = src
            if (PREV / "index" / f"{sym}.parquet").exists():
                shutil.copy2(PREV / "index" / f"{sym}.parquet", OUT / "index" / f"{sym}.parquet")
            continue
        df.to_parquet(OUT / "index" / f"{sym}.parquet", index=False)
    for code in u["code"]:
        df, src = fetch(code, order)
        if df is None:
            fails[code] = src
            if (PREV / "1d" / f"{code}.parquet").exists():
                shutil.copy2(PREV / "1d" / f"{code}.parquet", OUT / "1d" / f"{code}.parquet")
            continue
        src_count[src] = src_count.get(src, 0) + 1
        df.to_parquet(OUT / "1d" / f"{code}.parquet", index=False)
        time.sleep(0.15)
    u.to_csv(OUT / "universe.csv", index=False)
    kospi = OUT / "index" / "KOSPI.parquet"
    last = str(pd.read_parquet(kospi)["date"].max().date()) if kospi.exists() else None
    meta = {"status": "ok" if not fails else "partial", "last_daily_close": last, "universe": len(u),
            "universe_month": now_kst().strftime("%Y-%m"), "sources": src_count, "failures": fails,
            "updated_kst": now_kst().isoformat(timespec="seconds"), "start": START}
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print(json.dumps(meta, ensure_ascii=False)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
