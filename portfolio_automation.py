#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hyunwoo 포트폴리오 자동화 시스템 v2.0
- 매일 아침 7시 실행 (GitHub Actions)
- pykrx (한국주식), yfinance (해외주식)
- 섀넌 리밸런싱 (안정형) + 공격적 리밸런싱 (15% 기준, 변동성 자동 선정)
- Notion 데이터베이스 저장 + Telegram 메시지 발송
"""

import os
import requests
from datetime import datetime
import pandas as pd
import yfinance as yf

try:
    from pykrx import stock
except ImportError:
    print("pykrx 설치 필요: pip install pykrx")

# ==================== 환경변수 (GitHub Secrets에서 로드) ====================
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ==================== 포트폴리오 설정 ====================
PORTFOLIO = {
    "KODEX 200": {"ticker": "069500", "shares": 86, "purchase_price": 72276, "country": "KR", "category": "지수펀드"},
    "SK하이닉스": {"ticker": "000660", "shares": 4, "purchase_price": 2474500, "country": "KR", "category": "반도체"},
    "한국전력": {"ticker": "015760", "shares": 137, "purchase_price": 44079, "country": "KR", "category": "전력인프라"},
    "삼성전자": {"ticker": "005930", "shares": 13, "purchase_price": 327538, "country": "KR", "category": "반도체"},
    "HLB": {"ticker": "028300", "shares": 75, "purchase_price": 42953, "country": "KR", "category": "반도체"},
    "한미반도체": {"ticker": "042600", "shares": 5, "purchase_price": 212100, "country": "KR", "category": "반도체"},
}

# 목표 자산 배분 (%)  - 반도체 50% 유지 (2027년까지 반도체 강세 전망)
TARGET_ALLOCATION = {
    "현금": 30,
    "반도체": 50,
    "지수펀드": 15,
    "전력인프라": 5,
}

CASH_AVAILABLE = 30000000
MONTHLY_INVESTMENT = 250000

# 손절 기준
STOP_LOSS_NORMAL = -15  # 일반 변동성 장
VOLATILITY_THRESHOLD = 25  # 최근 변동폭이 이 이상이면 "고변동성 장"으로 판단

# 공격적 리밸런싱 기준
AGGRESSIVE_THRESHOLD = 15  # ±15% 변동 시 공격적 매매 후보
AGGRESSIVE_VOLATILITY_MIN = 20  # 변동성(최근 등락폭) 상위 종목 자동 선정 기준

# 보유 예외 종목 (연말까지 유지, 자동 손절 로직에서 제외)
HOLD_UNTIL_YEAREND = ["한국전력", "HLB"]

# ==================== 데이터 조회 함수 ====================

def get_korean_stock_price(ticker):
    try:
        today = datetime.now().strftime("%Y%m%d")
        data = stock.get_market_ohlcv(today, today, ticker)
        if len(data) > 0:
            return {"price": int(data["종가"].iloc[-1]), "change_pct": float(data["등락률"].iloc[-1])}
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
    return None

def get_foreign_stock_price(ticker):
    try:
        data = yf.Ticker(ticker)
        hist = data.history(period="2d")
        if len(hist) >= 2:
            price = hist["Close"].iloc[-1]
            prev = hist["Close"].iloc[-2]
            change_pct = (price - prev) / prev * 100
            return {"price": float(price), "change_pct": float(change_pct)}
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
    return None

def get_recent_volatility(ticker, country, days=20):
    """최근 N일간 최고-최저 변동폭(%) 계산 - 공격적 리밸런싱 종목 자동 선정용"""
    try:
        if country == "KR":
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - pd.Timedelta(days=days*1.5)).strftime("%Y%m%d")
            data = stock.get_market_ohlcv(start, end, ticker)
            if len(data) > 0:
                high = data["고가"].max()
                low = data["저가"].min()
                return float((high - low) / low * 100)
        else:
            data = yf.Ticker(ticker).history(period=f"{days}d")
            if len(data) > 0:
                high = data["High"].max()
                low = data["Low"].min()
                return float((high - low) / low * 100)
    except Exception as e:
        print(f"Volatility calc error {ticker}: {e}")
    return 0.0

# ==================== 포트폴리오 계산 ====================

def calculate_portfolio_value():
    total_value = 0
    total_cost = 0
    portfolio_data = []
    category_totals = {}

    for name, info in PORTFOLIO.items():
        price_data = get_korean_stock_price(info["ticker"]) if info["country"] == "KR" else get_foreign_stock_price(info["ticker"])
        if not price_data:
            continue

        current_price = price_data["price"]
        current_value = current_price * info["shares"]
        cost = info["purchase_price"] * info["shares"]
        profit = current_value - cost
        profit_rate = (profit / cost * 100) if cost > 0 else 0
        volatility = get_recent_volatility(info["ticker"], info["country"])

        total_value += current_value
        total_cost += cost
        cat = info["category"]
        category_totals[cat] = category_totals.get(cat, 0) + current_value

        portfolio_data.append({
            "종목": name, "현재가": current_price, "보유수": info["shares"],
            "평가금": current_value, "원금": cost, "수익금": profit,
            "수익률": profit_rate, "일간등락": price_data["change_pct"],
            "변동성20일": volatility, "카테고리": cat,
        })

    total_profit = total_value - total_cost
    total_profit_rate = (total_profit / total_cost * 100) if total_cost > 0 else 0

    return {
        "총자산": total_value, "원금": total_cost, "수익": total_profit,
        "수익률": total_profit_rate, "현금": CASH_AVAILABLE,
        "상세": portfolio_data, "카테고리별": category_totals,
    }

def calculate_shannon_rebalancing(pv):
    """섀넌 리밸런싱: 현금:주식 3:7 + 카테고리별 목표 대비 편차"""
    total = pv["총자산"] + pv["현금"]
    target_cash = total * 0.30
    target_stocks = total * 0.70

    category_analysis = []
    for cat, target_pct in TARGET_ALLOCATION.items():
        if cat == "현금":
            continue
        target_value = total * (target_pct / 100)
        current_value = pv["카테고리별"].get(cat, 0)
        deviation = current_value - target_value
        deviation_pct = (current_value / total * 100) - target_pct
        category_analysis.append({
            "카테고리": cat, "목표비중": target_pct,
            "현재비중": round(current_value / total * 100, 1),
            "편차": round(deviation_pct, 1), "편차금액": round(deviation),
        })

    return {
        "목표현금": target_cash, "현재현금": pv["현금"], "편차현금": pv["현금"] - target_cash,
        "목표주식": target_stocks, "현재주식": pv["총자산"], "편차주식": pv["총자산"] - target_stocks,
        "카테고리분석": category_analysis,
    }

def calculate_aggressive_rebalancing(pv):
    """공격적 리밸런싱: 변동성 상위 종목 중 ±15% 이상 움직인 종목 자동 선정"""
    candidates = []
    for item in pv["상세"]:
        is_volatile = item["변동성20일"] >= AGGRESSIVE_VOLATILITY_MIN
        is_moved = abs(item["수익률"]) >= AGGRESSIVE_THRESHOLD
        if is_volatile or is_moved:
            action = "매수 검토 (과매도 반등 가능)" if item["수익률"] <= -AGGRESSIVE_THRESHOLD else \
                     "매도 검토 (과열 구간)" if item["수익률"] >= AGGRESSIVE_THRESHOLD else \
                     "관찰 (고변동성)"
            candidates.append({
                "종목": item["종목"], "수익률": round(item["수익률"], 1),
                "변동성20일": round(item["변동성20일"], 1), "제안": action,
            })

    candidates.sort(key=lambda x: x["변동성20일"], reverse=True)
    return candidates

def check_stop_loss(pv):
    """손절 경고 체크 (연말 보유 예외 종목 제외)"""
    warnings = []
    for item in pv["상세"]:
        if item["종목"] in HOLD_UNTIL_YEAREND:
            continue
        if item["수익률"] <= STOP_LOSS_NORMAL:
            warnings.append(f"{item['종목']}: {item['수익률']:.1f}% (기준 {STOP_LOSS_NORMAL}% 도달)")
    return warnings

# ==================== Notion 저장 ====================

def save_to_notion(pv, shannon, aggressive, stop_warnings):
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        print("Notion 설정 없음, 스킵")
        return False

    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    rebalancing_text = " / ".join([f"{c['카테고리']}: {c['편차']:+.1f}%" for c in shannon["카테고리분석"]])
    aggressive_text = " / ".join([f"{c['종목']}({c['수익률']:+.1f}%): {c['제안']}" for c in aggressive]) if aggressive else "해당 없음"
    stop_loss_text = " / ".join(stop_warnings) if stop_warnings else "없음"

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "날짜": {"title": [{"text": {"content": datetime.now().strftime("%Y-%m-%d")}}]},
            "총자산": {"number": round(pv["총자산"])},
            "수익률": {"number": round(pv["수익률"], 2)},
            "일간수익": {"number": round(sum(i["평가금"] * i["일간등락"] / 100 for i in pv["상세"]))},
            "반도체비중": {"number": next((c["현재비중"] for c in shannon["카테고리분석"] if c["카테고리"] == "반도체"), 0)},
            "현금비중": {"number": round(pv["현금"] / (pv["총자산"] + pv["현금"]) * 100, 1)},
            "리밸런싱제안": {"rich_text": [{"text": {"content": rebalancing_text[:2000]}}]},
            "공격리밸런싱대상": {"rich_text": [{"text": {"content": aggressive_text[:2000]}}]},
            "손절경고": {"rich_text": [{"text": {"content": stop_loss_text[:2000]}}]},
        }
    }

    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code == 200:
        print("Notion 저장 완료")
        return True
    else:
        print(f"Notion 저장 실패: {resp.status_code} {resp.text}")
        return False

# ==================== Telegram 발송 ====================

def send_telegram_message(pv, shannon, aggressive, stop_warnings):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram 설정 없음, 스킵")
        return False

    today = datetime.now().strftime("%Y-%m-%d (%a)")
    daily_pl = sum(i["평가금"] * i["일간등락"] / 100 for i in pv["상세"])

    msg = f"*포트폴리오 아침 보고서* | {today}\n\n"
    msg += f"총자산: {pv['총자산']:,.0f}원\n"
    msg += f"수익률: {pv['수익률']:.2f}% ({pv['수익']:,.0f}원)\n"
    msg += f"일간: {daily_pl:,.0f}원\n\n"

    msg += "*섀넌 리밸런싱 (카테고리별 편차)*\n"
    for c in shannon["카테고리분석"]:
        emoji = "🔴" if abs(c["편차"]) > 5 else "🟢"
        msg += f"{emoji} {c['카테고리']}: {c['현재비중']}% (목표 {c['목표비중']}%, 편차 {c['편차']:+.1f}%)\n"
    msg += "\n"

    msg += "*공격적 리밸런싱 후보 (15%+ 변동)*\n"
    if aggressive:
        for c in aggressive:
            msg += f"⚡ {c['종목']}: {c['수익률']:+.1f}% → {c['제안']}\n"
    else:
        msg += "해당 종목 없음\n"
    msg += "\n"

    if stop_warnings:
        msg += "*손절 경고*\n"
        for w in stop_warnings:
            msg += f"🚨 {w}\n"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

    if resp.status_code == 200:
        print("Telegram 발송 완료")
        return True
    else:
        print(f"Telegram 발송 실패: {resp.status_code} {resp.text}")
        return False

# ==================== 메인 실행 ====================

if __name__ == "__main__":
    print(f"포트폴리오 자동화 시작: {datetime.now()}")

    pv = calculate_portfolio_value()
    shannon = calculate_shannon_rebalancing(pv)
    aggressive = calculate_aggressive_rebalancing(pv)
    stop_warnings = check_stop_loss(pv)

    save_to_notion(pv, shannon, aggressive, stop_warnings)
    send_telegram_message(pv, shannon, aggressive, stop_warnings)

    print("완료")
