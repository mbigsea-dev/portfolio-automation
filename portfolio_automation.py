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
    "한미반도체": {"ticker": "042700", "shares": 5, "purchase_price": 212100, "country": "KR", "category": "반도체"},
}

# 목표 자산 배분 (%) - Claude 추천안
# 근거: AI/HBM 슈퍼사이클은 견조하나 2026 하반기 반도체 쏠림 완화 전망 반영,
#       KODEX 200으로 변동성 완충, 전력인프라는 AI 데이터센터 전력수요 테마 유지
TARGET_ALLOCATION = {
    "반도체": 40,
    "지수펀드": 30,
    "전력인프라": 10,
    "현금": 20,
}

CASH_AVAILABLE = 1083934  # 토스 계좌 실제 보유 현금

MONTHLY_INVESTMENT = 250000

# 손절 기준
STOP_LOSS_NORMAL = -15  # 일반 변동성 장
VOLATILITY_THRESHOLD = 25  # 최근 변동폭이 이 이상이면 "고변동성 장"으로 판단

# 공격적 리밸런싱 기준
AGGRESSIVE_THRESHOLD = 15
AGGRESSIVE_VOLATILITY_MIN = 20

# 보유 예외 종목 (연말까지 유지, 자동 손절 로직에서 제외)
HOLD_UNTIL_YEAREND = ["한국전력", "HLB"]

# ==================== 데이터 조회 함수 ====================

def get_korean_stock_price(ticker):
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - pd.Timedelta(days=10)).strftime("%Y%m%d")
        data = stock.get_market_ohlcv(start, end, ticker)
        data = data[data["종가"] > 0]
        if len(data) > 0:
            price = int(data["종가"].iloc[-1])
            change = float(data["등락률"].iloc[-1])
            if abs(change) > 50:
                print(f"경고: {ticker} 등락률 이상치 감지 ({change}%), 0으로 처리")
                change = 0.0
            return {"price": price, "change_pct": change}
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
    """
    섀넌 리밸런싱: 현금(실보유)+주식 총액을 분모로,
    TARGET_ALLOCATION(반도체40/지수30/전력10/현금20)을 그대로 목표비중으로 사용.
    """
    total = pv["총자산"] + pv["현금"]  # 전체 자산 = 주식 + 실제 현금

    category_analysis = []
    for cat, target_pct in TARGET_ALLOCATION.items():
        if cat == "현금":
            current_value = pv["현금"]
        else:
            current_value = pv["카테고리별"].get(cat, 0)
        target_value = total * (target_pct / 100)
        deviation = current_value - target_value
        deviation_pct = (current_value / total * 100) - target_pct if total > 0 else 0
        category_analysis.append({
            "카테고리": cat, "목표비중": target_pct,
            "현재비중": round(current_value / total * 100, 1) if total > 0 else 0,
            "편차": round(deviation_pct, 1), "편차금액": round(deviation),
        })

    return {
        "카테고리분석": category_analysis,
        "총자산포함현금": total,
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

def get_naver_news(ticker, name, max_items=2):
    """네이버 금융 종목 뉴스 헤드라인 수집"""
    try:
        url = f"https://finance.naver.com/item/news_news.naver?code={ticker}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        resp.encoding = "euc-kr"
        import re
        titles = re.findall(r'title="([^"]+)"', resp.text)
        titles = [t for t in titles if len(t) > 5 and "종목뉴스" not in t][:max_items]
        return titles
    except Exception as e:
        print(f"뉴스 조회 실패 {name}: {e}")
        return []

def collect_all_news(pv):
    """보유 종목별 최신 뉴스 수집 (수익률 상하위 위주로 압축)"""
    sorted_items = sorted(pv["상세"], key=lambda x: abs(x["일간등락"]), reverse=True)
    news_result = {}
    for item in sorted_items[:4]:  # 일간 변동폭 큰 상위 4종목만
        ticker = next(v["ticker"] for k, v in PORTFOLIO.items() if k == item["종목"])
        titles = get_naver_news(ticker, item["종목"])
        if titles:
            news_result[item["종목"]] = titles
    return news_result

def get_market_psychology_note(pv, shannon):
    """장 변화에 따른 심리적 대처법 안내 - 상황별 코멘트"""
    daily_pl_pct = sum(i["평가금"] * i["일간등락"] / 100 for i in pv["상세"]) / pv["총자산"] * 100 if pv["총자산"] > 0 else 0
    max_deviation = max([abs(c["편차"]) for c in shannon["카테고리분석"]], default=0)

    notes = []
    if daily_pl_pct <= -3:
        notes.append(
            "오늘 하락폭이 큽니다. 급락장에서는 '지금 팔아야 하나'라는 생각이 가장 위험한 신호입니다. "
            "섀넌 리밸런싱은 원래 하락을 저가 매수 기회로 바꾸도록 설계된 방법입니다. "
            "정해둔 손절 기준(-15%)에 도달하지 않았다면 오늘의 감정으로 매도 결정을 내리지 마세요."
        )
    elif daily_pl_pct >= 3:
        notes.append(
            "오늘 상승폭이 큽니다. 급등 후에는 '더 오를 것 같다'는 낙관에 휩쓸리기 쉽습니다. "
            "목표 비중을 초과한 종목이 있다면 일부 차익 실현을 원칙대로 검토하세요."
        )
    else:
        notes.append("오늘은 평소 수준의 변동성입니다. 별도 대응 없이 계획대로 유지하시면 됩니다.")

    if max_deviation > 15:
        notes.append(
            f"카테고리 편차가 {max_deviation:.1f}%p로 큰 상태입니다. "
            "한 번에 다 맞추려 하지 말고, 월 정기투자금과 여유 현금이 생길 때마다 "
            "편차가 큰 쪽부터 나눠서 채워가는 방식을 권합니다."
        )
    return notes
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
            "총자산": {"number": round(pv["총자산"] + pv["현금"])},
            "수익률": {"number": round(pv["수익률"], 2)},
            "일간수익": {"number": round(sum(i["평가금"] * i["일간등락"] / 100 for i in pv["상세"]))},
            "반도체비중": {"number": next((c["현재비중"] for c in shannon["카테고리분석"] if c["카테고리"] == "반도체"), 0)},
            "현금비중": {"number": next((c["현재비중"] for c in shannon["카테고리분석"] if c["카테고리"] == "현금"), 0)},
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

def send_telegram_message(pv, shannon, aggressive, stop_warnings, news, psychology_notes):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram 설정 없음, 스킵")
        return False

    today = datetime.now().strftime("%Y년 %m월 %d일 (%a)")
    daily_pl = sum(i["평가금"] * i["일간등락"] / 100 for i in pv["상세"])
    total_with_cash = pv["총자산"] + pv["현금"]

    msg = "=========================\n"
    msg += f"📊 Hyunwoo 포트폴리오 | {today}\n"
    msg += "=========================\n\n"

    # 1. 현황 요약
    msg += "💰 현황 요약\n"
    msg += "---------------------\n"
    msg += f"주식 평가금: {pv['총자산']:,.0f}원\n"
    msg += f"현금 보유: {pv['현금']:,.0f}원\n"
    msg += f"총자산: {total_with_cash:,.0f}원\n"
    msg += f"수익률: {pv['수익률']:.2f}% ({pv['수익']:,.0f}원)\n"
    msg += f"일간 수익: {daily_pl:,.0f}원\n\n"

    # 2. 섀넌 리밸런싱 (종목별 상세 포함, 현금 포함 총자산 기준)
    msg += "🎯 섀넌 리밸런싱 제안 (Claude 추천 배분 기준)\n"
    msg += "---------------------\n"
    for c in shannon["카테고리분석"]:
        emoji = "🔴" if abs(c["편차"]) > 5 else "🟢"
        msg += f"{emoji} {c['카테고리']} (목표 {c['목표비중']}%)\n"
        msg += f"  - 현재 {c['현재비중']}% | 편차 {c['편차']:+.1f}%\n"
        if c["카테고리"] == "현금":
            continue
        cat_items = [i for i in pv["상세"] if i["카테고리"] == c["카테고리"]]
        for item in cat_items:
            item_pct = round(item["평가금"] / total_with_cash * 100, 1) if total_with_cash > 0 else 0
            msg += f"    - {item['종목']}: {item_pct}% ({item['수익률']:+.1f}%) 현재가 {item['현재가']:,.0f}원\n"
    msg += "\n"

    # 3. 오늘의 액션 아이템 (바로 실행 가능하도록 구체적으로)
    msg += "🎯 오늘의 액션 아이템\n"
    msg += "---------------------\n"
    action_num = 1
    for c in shannon["카테고리분석"]:
        if c["카테고리"] == "현금":
            if c["편차"] < -5:
                msg += f"{action_num}. [현금 확보] 현금 비중 부족 (편차 {c['편차']:+.1f}%) → 매도 시 일부는 현금으로 남겨두기\n"
                action_num += 1
            continue
        if c["편차"] < -5:
            msg += f"{action_num}. [매수 검토] {c['카테고리']} 비중 부족 (편차 {c['편차']:+.1f}%) → 월 투자금 우선 배분\n"
            action_num += 1
        elif c["편차"] > 5:
            msg += f"{action_num}. [매도 검토] {c['카테고리']} 비중 초과 (편차 {c['편차']:+.1f}%) → 일부 차익 실현 고려\n"
            action_num += 1
    if action_num == 1:
        msg += f"{action_num}. [유지] 목표 배분과 크게 벗어나지 않음, 모니터링만 유지\n"
        action_num += 1
    msg += f"{action_num}. [정기투자] 월 {MONTHLY_INVESTMENT:,.0f}원 → 편차 가장 큰 카테고리에 우선 배분\n\n"

    # 4. 손절 경고
    if stop_warnings:
        msg += "🚨 손절 경고\n"
        msg += "---------------------\n"
        for w in stop_warnings:
            msg += f"🚨 {w}\n"
        msg += "(한국전력·HLB는 연말 보유 방침에 따라 제외)\n\n"

    # 5. 심리적 대처 안내
    msg += "🧠 오늘의 심리 코칭\n"
    msg += "---------------------\n"
    for note in psychology_notes:
        msg += f"{note}\n\n"

    # 6. 뉴스
    if news:
        msg += "📰 주요 뉴스 (변동폭 큰 종목 위주)\n"
        msg += "---------------------\n"
        for stock_name, titles in news.items():
            msg += f"[{stock_name}]\n"
            for t in titles:
                msg += f"  - {t}\n"
        msg += "\n"

    # 7. 공격적 리밸런싱 후보 (맨 아래)
    msg += "⚡ 공격적 리밸런싱 후보 (15%+ 변동)\n"
    msg += "---------------------\n"
    if aggressive:
        for c in aggressive:
            msg += f"⚡ {c['종목']}: {c['수익률']:+.1f}% → {c['제안']}\n"
    else:
        msg += "해당 종목 없음\n"
    msg += "\n"

    # 8. 매매 기록 안내 (자동 반영은 안 되지만 기록 유도)
    msg += "---------------------\n"
    msg += "💡 오늘 매수/매도하셨다면 이 방에 기록해두세요.\n"
    msg += "(자동 반영은 안 되니, GitHub의 portfolio_automation.py에서 수동으로 수정해주세요)"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

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
    news = collect_all_news(pv)
    psychology_notes = get_market_psychology_note(pv, shannon)

    save_to_notion(pv, shannon, aggressive, stop_warnings)
    send_telegram_message(pv, shannon, aggressive, stop_warnings, news, psychology_notes)

    print("완료")
