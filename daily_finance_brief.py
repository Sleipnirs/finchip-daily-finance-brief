#!/usr/bin/env python3
"""
daily-finance-brief — FinChip Skill
===================================
Generates a daily global-finance briefing in English:

  Section 1  One-page dashboard of key asset moves
             (equity indices, blue chips, FX, commodities, crypto, rates & vol)
  Section 2  Top-10 global political / economic news of the day
  Section 3  Analysis paragraph (<= 500 words), Claude-generated if
             ANTHROPIC_API_KEY is set, otherwise rule-based fallback.

Outputs:  report.md  (always)  +  report.pdf  (unless --format md)

Invocation (the "call skill" API — input is a JSON file, string, or stdin):
    python daily_finance_brief.py                                # defaults
    python daily_finance_brief.py examples/input_empty.json
    python daily_finance_brief.py '{"format":"md","demo":true}'
    echo '{"format":"pdf"}' | python daily_finance_brief.py -
    python daily_finance_brief.py --serve --port 8787            # HTTP mode
        GET  /run?format=both&demo=0 -> JSON {md_path, pdf_path, generated_at}
        GET  /report.md  |  /report.pdf  |  /health

Dependencies: requests, feedparser, reportlab   (yfinance optional, preferred)
No API keys required. ANTHROPIC_API_KEY is optional (better analysis).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional

import requests

# ----------------------------------------------------------------------------
# Configuration: instruments & sources
# ----------------------------------------------------------------------------

UA = {"User-Agent": "finchip-daily-brief/1.0 (+https://finchip.ai)"}

# (yahoo_symbol, display_name, stooq_symbol_or_None)
INSTRUMENTS: dict[str, list[tuple[str, str, Optional[str]]]] = {
    "Equity Indices": [
        ("^GSPC",     "S&P 500",        "^spx"),
        ("^IXIC",     "Nasdaq Comp.",   "^ndq"),
        ("^DJI",      "Dow Jones",      "^dji"),
        ("^FTSE",     "FTSE 100",       "^ftm"),
        ("^GDAXI",    "DAX 40",         "^dax"),
        ("^N225",     "Nikkei 225",     "^nkx"),
        ("^HSI",      "Hang Seng",      None),
        ("000001.SS", "Shanghai Comp.", None),
    ],
    "Blue Chips": [
        ("AAPL",  "Apple",     "aapl.us"),
        ("MSFT",  "Microsoft", "msft.us"),
        ("NVDA",  "NVIDIA",    "nvda.us"),
        ("AMZN",  "Amazon",    "amzn.us"),
        ("GOOGL", "Alphabet",  "googl.us"),
        ("META",  "Meta",      "meta.us"),
        ("TSM",   "TSMC",      "tsm.us"),
        ("JPM",   "JPMorgan",  "jpm.us"),
    ],
    "FX": [
        ("DX-Y.NYB", "US Dollar Index", None),
        ("EURUSD=X", "EUR/USD",         "eurusd"),
        ("USDJPY=X", "USD/JPY",         "usdjpy"),
        ("GBPUSD=X", "GBP/USD",         "gbpusd"),
        ("USDCNY=X", "USD/CNY",         "usdcny"),
    ],
    "Commodities": [
        ("GC=F", "Gold",        "xauusd"),
        ("SI=F", "Silver",      "xagusd"),
        ("CL=F", "WTI Crude",   "cl.f"),
        ("BZ=F", "Brent Crude", None),
        ("HG=F", "Copper",      "hg.f"),
    ],
    "Rates & Volatility": [
        ("^TNX", "US 10Y Yield (%)", "10usy.b"),
        ("^VIX", "VIX",              None),
    ],
}

# CoinGecko ids -> display names
CRYPTO = [
    ("bitcoin",  "Bitcoin (BTC)"),
    ("ethereum", "Ethereum (ETH)"),
    ("solana",   "Solana (SOL)"),
    ("binancecoin", "BNB"),
]

NEWS_FEEDS = [
    # (url, source_name, source_weight)
    ("https://feeds.content.dowjones.io/public/rss/mw_topstories",      "MarketWatch", 1.0),
    ("https://www.cnbc.com/id/100003114/device/rss/rss.html",           "CNBC Top News", 1.0),
    ("https://www.cnbc.com/id/20910258/device/rss/rss.html",            "CNBC Economy", 1.1),
    ("https://feeds.bbci.co.uk/news/business/rss.xml",                  "BBC Business", 1.0),
    ("https://www.theguardian.com/business/economics/rss",              "Guardian Economics", 0.9),
    ("https://news.google.com/rss/search?q=(federal+reserve+OR+ECB+OR+inflation+OR+tariff+OR+treasury+yields)&hl=en-US&gl=US&ceid=US:en",
                                                                        "Google News (Macro)", 0.9),
    ("https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
                                                                        "Google News (Business)", 0.8),
]

# keyword -> importance weight for news ranking
NEWS_KEYWORDS = {
    "federal reserve": 3.0, "fed ": 2.5, "fomc": 3.0, "rate cut": 3.0, "rate hike": 3.0,
    "interest rate": 2.5, "inflation": 2.5, "cpi": 2.5, "ppi": 2.0, "gdp": 2.5,
    "ecb": 2.5, "boj": 2.5, "bank of japan": 2.5, "pboc": 2.5, "bank of england": 2.0,
    "treasury": 2.0, "yield": 2.0, "bond": 1.5, "recession": 2.5, "stimulus": 2.0,
    "tariff": 2.8, "trade war": 2.8, "sanction": 2.3, "opec": 2.3, "oil": 1.5,
    "china": 1.8, "election": 1.8, "white house": 1.8, "congress": 1.5, "imf": 1.8,
    "earnings": 1.5, "default": 2.2, "bailout": 2.2, "layoff": 1.5, "jobs report": 2.5,
    "unemployment": 2.2, "nonfarm": 2.5, "crypto": 1.5, "bitcoin": 1.5, "etf": 1.5,
    "sec ": 1.8, "regulation": 1.5, "war": 2.0, "ceasefire": 2.0, "geopolit": 2.2,
    "dollar": 1.8, "currency": 1.5, "stock market": 1.5, "sell-off": 2.2, "rally": 1.8,
    "ai ": 1.5, "chip": 1.5, "semiconductor": 1.8,
}

ANTHROPIC_MODEL = os.environ.get("DFB_ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ----------------------------------------------------------------------------
# Data structures
# ----------------------------------------------------------------------------

@dataclass
class Quote:
    name: str
    price: Optional[float] = None
    change_pct: Optional[float] = None   # 1-day % change
    ok: bool = False

@dataclass
class NewsItem:
    title: str
    source: str
    link: str
    published: str
    score: float = 0.0

@dataclass
class Brief:
    generated_at: str
    market: dict[str, list[Quote]] = field(default_factory=dict)
    news: list[NewsItem] = field(default_factory=list)
    analysis: str = ""
    analysis_engine: str = "rule-based"
    warnings: list[str] = field(default_factory=list)

# ----------------------------------------------------------------------------
# Market data fetchers
# ----------------------------------------------------------------------------

def _fetch_yfinance(symbols: list[str]) -> dict[str, tuple[float, float]]:
    """Return {symbol: (last_price, pct_change)} using yfinance if available."""
    out: dict[str, tuple[float, float]] = {}
    try:
        import yfinance as yf  # optional dependency
    except ImportError:
        return out
    try:
        data = yf.download(symbols, period="5d", interval="1d",
                           progress=False, group_by="ticker", threads=True)
        for sym in symbols:
            try:
                closes = (data[sym]["Close"] if len(symbols) > 1 else data["Close"]).dropna()
                if len(closes) >= 2:
                    last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                    out[sym] = (last, (last / prev - 1.0) * 100.0)
            except Exception:
                continue
    except Exception:
        pass
    # per-symbol retry for anything the batch missed (e.g. sqlite cache
    # "database is locked" under threaded download)
    for sym in symbols:
        if sym in out:
            continue
        try:
            time.sleep(0.3)
            closes = yf.Ticker(sym).history(period="5d", interval="1d")["Close"].dropna()
            if len(closes) >= 2:
                last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                out[sym] = (last, (last / prev - 1.0) * 100.0)
        except Exception:
            continue
    return out


def _fetch_stooq(stooq_sym: str) -> Optional[tuple[float, float]]:
    """Free CSV endpoint, no key. Returns (last, pct_change) or None."""
    url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"
    try:
        r = requests.get(url, headers=UA, timeout=10)
        text = r.text.strip()
        if not text.lower().startswith("date,"):
            return None            # rate-limit page / HTML error, not CSV
        rows = [ln.split(",") for ln in text.splitlines()[1:] if ln]
        closes = [float(row[4]) for row in rows[-3:] if len(row) >= 5]
        if len(closes) >= 2:
            last, prev = closes[-1], closes[-2]
            return last, (last / prev - 1.0) * 100.0
    except Exception:
        return None
    return None


def _fetch_coingecko() -> dict[str, tuple[float, float]]:
    ids = ",".join(cid for cid, _ in CRYPTO)
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd&include_24hr_change=true")
    out: dict[str, tuple[float, float]] = {}
    try:
        r = requests.get(url, headers=UA, timeout=15)
        j = r.json()
        for cid, _name in CRYPTO:
            if cid in j and "usd" in j[cid]:
                out[cid] = (float(j[cid]["usd"]),
                            float(j[cid].get("usd_24h_change") or 0.0))
    except Exception:
        pass
    return out


def fetch_market(brief: Brief) -> None:
    all_yahoo = [sym for grp in INSTRUMENTS.values() for sym, _, _ in grp]
    yq = _fetch_yfinance(all_yahoo)
    if not yq:
        brief.warnings.append("yfinance unavailable or blocked; using Stooq fallback where possible")

    for group, items in INSTRUMENTS.items():
        quotes: list[Quote] = []
        for sym, name, stooq_sym in items:
            q = Quote(name=name)
            if sym in yq:
                q.price, q.change_pct, q.ok = yq[sym][0], yq[sym][1], True
            elif stooq_sym:
                res = _fetch_stooq(stooq_sym)
                if res:
                    q.price, q.change_pct, q.ok = res[0], res[1], True
            if not q.ok:
                brief.warnings.append(f"no data: {name} ({sym})")
            quotes.append(q)
        brief.market[group] = quotes

    cg = _fetch_coingecko()
    crypto_quotes: list[Quote] = []
    for cid, name in CRYPTO:
        q = Quote(name=name)
        if cid in cg:
            q.price, q.change_pct, q.ok = cg[cid][0], cg[cid][1], True
        else:
            brief.warnings.append(f"no data: {name}")
        crypto_quotes.append(q)
    brief.market["Crypto"] = crypto_quotes

# ----------------------------------------------------------------------------
# News
# ----------------------------------------------------------------------------

def fetch_news(brief: Brief, top_n: int = 10) -> None:
    try:
        import feedparser
    except ImportError:
        brief.warnings.append("feedparser not installed; news section empty")
        return

    now = time.time()
    seen_titles: set[str] = set()
    items: list[NewsItem] = []

    for url, source, src_weight in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=UA)
        except Exception:
            brief.warnings.append(f"feed failed: {source}")
            continue
        for e in feed.entries[:25]:
            title = html.unescape(getattr(e, "title", "")).strip()
            if not title:
                continue
            key = re.sub(r"\W+", "", title.lower())[:80]
            if key in seen_titles:
                continue
            seen_titles.add(key)

            published = getattr(e, "published", "") or getattr(e, "updated", "")
            ts = None
            for attr in ("published_parsed", "updated_parsed"):
                if getattr(e, attr, None):
                    ts = time.mktime(getattr(e, attr))
                    break
            age_h = (now - ts) / 3600.0 if ts else 24.0
            if age_h > 36:            # stale -> skip
                continue
            recency = max(0.0, 1.5 - age_h / 24.0)      # 0..1.5

            low = " " + title.lower() + " "
            kw = sum(w for k, w in NEWS_KEYWORDS.items() if k in low)

            items.append(NewsItem(
                title=title, source=source,
                link=getattr(e, "link", ""), published=published,
                score=(kw + 0.5) * src_weight + recency,
            ))

    items.sort(key=lambda x: x.score, reverse=True)
    # keep at most 2 per source for diversity
    per_src: dict[str, int] = {}
    picked: list[NewsItem] = []
    for it in items:
        if per_src.get(it.source, 0) >= 3:
            continue
        per_src[it.source] = per_src.get(it.source, 0) + 1
        picked.append(it)
        if len(picked) == top_n:
            break
    brief.news = picked
    if not picked:
        brief.warnings.append("no fresh news items collected")

# ----------------------------------------------------------------------------
# Analysis (Claude if key present, otherwise rule-based)
# ----------------------------------------------------------------------------

def _market_digest(brief: Brief) -> str:
    lines = []
    for group, quotes in brief.market.items():
        parts = [f"{q.name} {q.change_pct:+.2f}%" for q in quotes if q.ok and q.change_pct is not None]
        if parts:
            lines.append(f"{group}: " + ", ".join(parts))
    if brief.news:
        lines.append("Top headlines: " + " | ".join(n.title for n in brief.news[:6]))
    return "\n".join(lines)


def _claude_analysis(brief: Brief) -> Optional[str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "You are a sell-side macro strategist. Based ONLY on the snapshot below, "
        "write ONE cohesive analysis section of AT MOST 500 words in English for a daily "
        "briefing: what drove today's moves, cross-asset signals (risk-on/off, rates vs. "
        "equities vs. dollar vs. gold vs. crypto), and 2-3 things to watch tomorrow. "
        "No bullet points, no headers, no preamble — just the paragraphs.\n\n"
        + _market_digest(brief)
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": 900,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        j = r.json()
        text = " ".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text").strip()
        return text or None
    except Exception:
        brief.warnings.append("Claude analysis call failed; using rule-based fallback")
        return None


def _rule_based_analysis(brief: Brief) -> str:
    def chg(group: str, name: str) -> Optional[float]:
        for q in brief.market.get(group, []):
            if q.name == name and q.ok:
                return q.change_pct
        return None

    all_q = [q for qs in brief.market.values() for q in qs if q.ok and q.change_pct is not None]
    if not all_q:
        return ("Market data could not be retrieved for this session, so no cross-asset "
                "read is available. Refer to the headlines above for the day's key drivers.")

    movers = sorted(all_q, key=lambda q: abs(q.change_pct), reverse=True)[:3]
    eq = [q.change_pct for q in brief.market.get("Equity Indices", []) if q.ok]
    eq_avg = sum(eq) / len(eq) if eq else 0.0
    vix = chg("Rates & Volatility", "VIX")
    tnx = chg("Rates & Volatility", "US 10Y Yield (%)")
    dxy = chg("FX", "US Dollar Index")
    gold = chg("Commodities", "Gold")
    btc = chg("Crypto", "Bitcoin (BTC)")

    risk = 0.0
    risk += 1 if eq_avg > 0.15 else (-1 if eq_avg < -0.15 else 0)
    if vix is not None:  risk += -1 if vix > 3 else (1 if vix < -3 else 0)
    if btc is not None:  risk += 0.5 if btc > 1 else (-0.5 if btc < -1 else 0)
    if gold is not None: risk += -0.5 if gold > 0.8 else 0
    tone = "risk-on" if risk >= 1 else ("risk-off" if risk <= -1 else "mixed")

    p1 = (f"Global markets closed the session with a {tone} tone. Major equity indices "
          f"averaged {eq_avg:+.2f}% on the day, with the largest single moves coming from "
          + ", ".join(f"{m.name} ({m.change_pct:+.2f}%)" for m in movers) + ". ")
    p2 = ""
    if tnx is not None or dxy is not None:
        p2 = ("In rates and currencies, the US 10-year yield moved "
              f"{tnx:+.2f}% " if tnx is not None else "") + \
             (f"while the dollar index changed {dxy:+.2f}%. " if dxy is not None else "")
    p3 = ""
    if gold is not None or btc is not None:
        p3 = ("Across alternative stores of value, gold "
              f"{'gained' if (gold or 0) >= 0 else 'fell'} {abs(gold or 0):.2f}% " if gold is not None else "") + \
             (f"and bitcoin moved {btc:+.2f}% over 24 hours, " if btc is not None else "") + \
             "keeping the hedging complex consistent with the broader tone. "
    p4 = ("Headlines centered on " + "; ".join(n.title for n in brief.news[:3]) + ". "
          if brief.news else "")
    p5 = ("Watch tomorrow: follow-through in the day's biggest movers, any central-bank "
          "commentary that could reprice rate expectations, and whether volatility confirms "
          "or fades today's direction.")
    return (p1 + p2 + p3 + p4 + p5).strip()


def build_analysis(brief: Brief) -> None:
    text = _claude_analysis(brief)
    if text:
        brief.analysis, brief.analysis_engine = text, f"claude ({ANTHROPIC_MODEL})"
    else:
        brief.analysis, brief.analysis_engine = _rule_based_analysis(brief), "rule-based"

# ----------------------------------------------------------------------------
# Renderers
# ----------------------------------------------------------------------------

def _fmt_price(q: Quote) -> str:
    if not q.ok or q.price is None:
        return "—"
    p = q.price
    return f"{p:,.4f}" if p < 10 else (f"{p:,.2f}" if p < 100000 else f"{p:,.0f}")


def _fmt_chg(q: Quote) -> str:
    return f"{q.change_pct:+.2f}%" if (q.ok and q.change_pct is not None) else "—"


def render_markdown(brief: Brief) -> str:
    d = brief.generated_at
    out = [f"# Daily Global Finance Brief", f"*Generated {d} (UTC) — FinChip Skill `daily-finance-brief`*", ""]
    out += ["## 1 · Global Asset Dashboard", ""]
    for group, quotes in brief.market.items():
        out += [f"### {group}", "", "| Asset | Last | 1D Change |", "|---|---:|---:|"]
        for q in quotes:
            arrow = "" if not q.ok else ("🔺 " if (q.change_pct or 0) > 0 else ("🔻 " if (q.change_pct or 0) < 0 else "▪️ "))
            out.append(f"| {q.name} | {_fmt_price(q)} | {arrow}{_fmt_chg(q)} |")
        out.append("")
    out += ["## 2 · Top 10 Global Political & Economic News", ""]
    if brief.news:
        for i, n in enumerate(brief.news, 1):
            out.append(f"{i}. **{n.title}** — *{n.source}*" + (f" ([link]({n.link}))" if n.link else ""))
    else:
        out.append("_No fresh headlines were collected in this run._")
    out += ["", "## 3 · Analysis", "", brief.analysis, "",
            f"---", f"*Analysis engine: {brief.analysis_engine}. Sources: Yahoo Finance / Stooq, "
            f"CoinGecko, MarketWatch, CNBC, BBC, The Guardian, Google News. "
            f"Informational only — not investment advice.*"]
    if brief.warnings:
        out += ["", "<details><summary>Run warnings</summary>", ""]
        out += [f"- {w}" for w in brief.warnings] + ["", "</details>"]
    return "\n".join(out) + "\n"


def render_pdf(brief: Brief, path: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate,
                                    Paragraph, Spacer, Table, TableStyle,
                                    NextPageTemplate, PageBreak)

    AZURE, COBALT, GOLD = colors.HexColor("#2F9BFF"), colors.HexColor("#0A5BE0"), colors.HexColor("#D99E1A")
    SKY, INK, UP, DOWN = colors.HexColor("#EAF4FE"), colors.HexColor("#0F2440"), colors.HexColor("#0B8A3E"), colors.HexColor("#C0392B")

    styles = getSampleStyleSheet()
    s_title = ParagraphStyle("t", parent=styles["Title"], fontSize=19, textColor=COBALT,
                             alignment=0, spaceAfter=1)
    s_sub   = ParagraphStyle("s", parent=styles["Normal"], fontSize=8.5, textColor=colors.HexColor("#5B7898"))
    s_grp   = ParagraphStyle("g", parent=styles["Normal"], fontSize=9.5, textColor=colors.white,
                             backColor=COBALT, leading=13, leftIndent=0)
    s_h2    = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, textColor=COBALT, spaceBefore=4)
    s_body  = ParagraphStyle("b", parent=styles["Normal"], fontSize=9.5, leading=13.5, textColor=INK)
    s_news  = ParagraphStyle("n", parent=styles["Normal"], fontSize=9.5, leading=13, textColor=INK, spaceAfter=4)
    s_foot  = ParagraphStyle("f", parent=styles["Normal"], fontSize=7, textColor=colors.HexColor("#7A8CA3"))

    def header(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(SKY)
        canvas.rect(0, A4[1] - 16 * mm, A4[0], 16 * mm, stroke=0, fill=1)
        canvas.setFillColor(GOLD)
        canvas.rect(0, A4[1] - 16.8 * mm, A4[0], 0.8 * mm, stroke=0, fill=1)
        canvas.setFillColor(COBALT); canvas.setFont("Helvetica-Bold", 11)
        canvas.drawString(14 * mm, A4[1] - 10.5 * mm, "FinChip · Daily Global Finance Brief")
        canvas.setFillColor(INK); canvas.setFont("Helvetica", 8)
        canvas.drawRightString(A4[0] - 14 * mm, A4[1] - 10.5 * mm, f"{brief.generated_at} UTC")
        canvas.restoreState()

    doc = BaseDocTemplate(path, pagesize=A4,
                          leftMargin=14 * mm, rightMargin=14 * mm,
                          topMargin=20 * mm, bottomMargin=12 * mm)
    # page 1: two-column dashboard; page 2+: single column
    colw = (A4[0] - 28 * mm - 6 * mm) / 2
    f1 = Frame(14 * mm, 12 * mm, colw, A4[1] - 34 * mm, id="c1")
    f2 = Frame(14 * mm + colw + 6 * mm, 12 * mm, colw, A4[1] - 34 * mm, id="c2")
    full = Frame(14 * mm, 12 * mm, A4[0] - 28 * mm, A4[1] - 34 * mm, id="full")
    doc.addPageTemplates([PageTemplate(id="dash", frames=[f1, f2], onPage=header),
                          PageTemplate(id="text", frames=[full], onPage=header)])

    story = [Paragraph("Global Asset Dashboard", s_title),
             Paragraph("Section 1 · One-day moves across major asset classes", s_sub),
             Spacer(1, 4)]

    def qtable(group: str, quotes: list[Quote]):
        data = [[Paragraph(f"<b>{group}</b>", s_grp), "", ""]]
        for q in quotes:
            c = UP if (q.ok and (q.change_pct or 0) > 0) else (DOWN if (q.ok and (q.change_pct or 0) < 0) else INK)
            data.append([Paragraph(q.name, s_body), _fmt_price(q), _fmt_chg(q)])
        t = Table(data, colWidths=[colw * 0.52, colw * 0.26, colw * 0.22])
        style = [
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), COBALT),
            ("BACKGROUND", (0, 1), (-1, -1), colors.white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SKY]),
            ("FONTSIZE", (1, 1), (-1, -1), 9),
            ("FONTNAME", (1, 1), (-1, -1), "Helvetica"),
            ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ("TEXTCOLOR", (1, 1), (1, -1), INK),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, GOLD),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#BFD9F5")),
        ]
        for i, q in enumerate(quotes, start=1):
            c = UP if (q.ok and (q.change_pct or 0) > 0) else (DOWN if (q.ok and (q.change_pct or 0) < 0) else INK)
            style.append(("TEXTCOLOR", (2, i), (2, i), c))
            style.append(("FONTNAME", (2, i), (2, i), "Helvetica-Bold"))
        t.setStyle(TableStyle(style))
        return t

    for group, quotes in brief.market.items():
        story += [qtable(group, quotes), Spacer(1, 7)]

    story += [NextPageTemplate("text"), PageBreak(),
              Paragraph("Top 10 Global Political &amp; Economic News", s_h2), Spacer(1, 2)]
    if brief.news:
        for i, n in enumerate(brief.news, 1):
            story.append(Paragraph(
                f"<b>{i}.</b> {html.escape(n.title)} "
                f'<font size="8" color="#5B7898">— {html.escape(n.source)}</font>', s_news))
    else:
        story.append(Paragraph("No fresh headlines were collected in this run.", s_body))

    story += [Spacer(1, 8), Paragraph("Analysis", s_h2), Spacer(1, 2)]
    for para in brief.analysis.split("\n\n"):
        story += [Paragraph(html.escape(para), s_body), Spacer(1, 4)]

    story += [Spacer(1, 10), Paragraph(
        f"Analysis engine: {brief.analysis_engine}. Sources: Yahoo Finance / Stooq, CoinGecko, "
        "MarketWatch, CNBC, BBC, The Guardian, Google News. Informational only — not investment advice.",
        s_foot)]
    doc.build(story)

# ----------------------------------------------------------------------------
# Demo fixtures (offline test mode)
# ----------------------------------------------------------------------------

def load_demo(brief: Brief) -> None:
    fx = {
        "Equity Indices": [("S&P 500", 6489.22, 0.84), ("Nasdaq Comp.", 21440.15, 1.22),
                           ("Dow Jones", 45102.60, 0.41), ("FTSE 100", 8890.34, -0.18),
                           ("DAX 40", 24310.55, 0.35), ("Nikkei 225", 42780.90, 1.05),
                           ("Hang Seng", 24890.44, -0.62), ("Shanghai Comp.", 3455.87, -0.21)],
        "Blue Chips": [("Apple", 244.31, 0.62), ("Microsoft", 512.44, 1.10),
                       ("NVIDIA", 176.02, 2.85), ("Amazon", 231.77, 0.95),
                       ("Alphabet", 201.15, 0.44), ("Meta", 742.60, -0.35),
                       ("TSMC", 228.90, 1.75), ("JPMorgan", 291.33, 0.28)],
        "FX": [("US Dollar Index", 97.42, -0.33), ("EUR/USD", 1.1842, 0.31),
               ("USD/JPY", 143.85, -0.42), ("GBP/USD", 1.3722, 0.18), ("USD/CNY", 7.1420, -0.05)],
        "Commodities": [("Gold", 3348.50, 0.72), ("Silver", 37.05, 1.34),
                        ("WTI Crude", 66.42, -1.85), ("Brent Crude", 68.51, -1.62), ("Copper", 5.12, 0.88)],
        "Rates & Volatility": [("US 10Y Yield (%)", 4.34, -1.10), ("VIX", 16.42, -4.20)],
        "Crypto": [("Bitcoin (BTC)", 108420.0, 2.15), ("Ethereum (ETH)", 2588.4, 3.42),
                   ("Solana (SOL)", 152.7, 4.10), ("BNB", 662.3, 1.22)],
    }
    for group, rows in fx.items():
        brief.market[group] = [Quote(name=n, price=p, change_pct=c, ok=True) for n, p, c in rows]
    demo_news = [
        ("US June payrolls beat expectations, tempering hopes for a September rate cut", "CNBC Economy"),
        ("Treasury yields slip as markets weigh mixed signals from Fed officials", "MarketWatch"),
        ("EU and US negotiators race to close framework trade deal before tariff deadline", "BBC Business"),
        ("Congress passes sweeping tax-and-spending bill; deficit projections in focus", "Google News (Macro)"),
        ("Oil falls ahead of OPEC+ meeting expected to approve another supply increase", "MarketWatch"),
        ("Dollar heads for worst first half in decades as reserve managers diversify", "Guardian Economics"),
        ("Nvidia nears record valuation as AI capex cycle shows no sign of slowing", "CNBC Top News"),
        ("China services PMI cools, adding pressure for further stimulus in H2", "Google News (Business)"),
        ("Bitcoin tops $108,000 as spot-ETF inflows accelerate for a third week", "CNBC Top News"),
        ("ECB minutes show growing debate over the pace of further easing", "Guardian Economics"),
    ]
    brief.news = [NewsItem(title=t, source=s, link="", published="") for t, s in demo_news]
    brief.warnings.append("DEMO MODE: fixture data, not live quotes")

# ----------------------------------------------------------------------------
# Skill entrypoints
# ----------------------------------------------------------------------------

def run_skill(fmt: str = "both", out_dir: str = "./out", demo: bool = False,
              news_count: int = 10) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    brief = Brief(generated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"))
    if demo:
        load_demo(brief)
    else:
        fetch_market(brief)
        fetch_news(brief, top_n=news_count)
    build_analysis(brief)

    result = {"generated_at": brief.generated_at, "warnings": brief.warnings}
    if fmt in ("md", "both"):
        md_path = os.path.join(out_dir, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(render_markdown(brief))
        result["md_path"] = md_path
    if fmt in ("pdf", "both"):
        pdf_path = os.path.join(out_dir, "report.pdf")
        render_pdf(brief, pdf_path)
        result["pdf_path"] = pdf_path
    return result


def serve(port: int, out_dir: str) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            try:
                if u.path == "/run":
                    fmt = parse_qs(u.query).get("format", ["both"])[0]
                    demo = parse_qs(u.query).get("demo", ["0"])[0] == "1"
                    res = run_skill(fmt=fmt, out_dir=out_dir, demo=demo)
                    body = json.dumps(res).encode()
                    self.send_response(200); self.send_header("Content-Type", "application/json")
                elif u.path in ("/report.md", "/report.pdf"):
                    p = os.path.join(out_dir, u.path.lstrip("/"))
                    with open(p, "rb") as f:
                        body = f.read()
                    ctype = "text/markdown" if p.endswith(".md") else "application/pdf"
                    self.send_response(200); self.send_header("Content-Type", ctype)
                elif u.path == "/health":
                    body = b'{"ok":true,"skill":"daily-finance-brief"}'
                    self.send_response(200); self.send_header("Content-Type", "application/json")
                else:
                    body = b'{"error":"not found"}'
                    self.send_response(404); self.send_header("Content-Type", "application/json")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):  # quiet
            pass

    print(f"daily-finance-brief serving on :{port}  (GET /run, /report.md, /report.pdf, /health)")
    HTTPServer(("0.0.0.0", port), H).serve_forever()


DEFAULT_INPUT = {"format": "both", "out": "./out", "demo": False, "news_count": 10}


def _load_input(arg: Optional[str]) -> dict:
    """Accept: nothing (defaults) | path to .json file | inline JSON string | '-' (stdin)."""
    cfg = dict(DEFAULT_INPUT)
    if not arg:
        return cfg
    if arg == "-":
        cfg.update(json.load(sys.stdin) or {})
    elif os.path.isfile(arg):
        with open(arg, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    else:
        cfg.update(json.loads(arg))
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(
        description="FinChip skill: daily global finance brief",
        epilog=("Examples:  python daily_finance_brief.py examples/input_empty.json | "
                "python daily_finance_brief.py '{\"format\":\"md\",\"demo\":true}' | "
                "python daily_finance_brief.py --serve --port 8787"))
    ap.add_argument("input", nargs="?", default=None,
                    help="path to input .json, inline JSON string, or '-' for stdin (omit for defaults)")
    ap.add_argument("--serve", action="store_true", help="run as HTTP endpoint instead of one-shot")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    if args.serve:
        out_dir = _load_input(args.input).get("out", "./out")
        serve(args.port, out_dir)
        return 0
    try:
        cfg = _load_input(args.input)
        res = run_skill(fmt=cfg.get("format", "both"),
                        out_dir=cfg.get("out", "./out"),
                        demo=bool(cfg.get("demo", False)),
                        news_count=int(cfg.get("news_count", 10)))
        print(json.dumps(res, indent=2))
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
