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
from datetime import datetime, timezone, timedelta
import pandas as pd
import yfinance as yf

KST = timezone(timedelta(hours=9))

def now_kst():
    """GitHub Actions 서버는 UTC로 동작하므로, 한국 시간대로 명시 변환해서 사용"""
    return datetime.now(KST)

try:
    from pykrx import stock
except ImportError:
    print("pykrx 설치 필요: pip install pykrx")

# ==================== 환경변수 (GitHub Secrets에서 로드) ====================
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")  # 리포트 기록용 DB
NOTION_HOLDINGS_DATABASE_ID = os.environ.get("NOTION_HOLDINGS_DATABASE_ID")  # 보유종목 입력용 DB
NOTION_CASH_PAGE_ID = os.environ.get("NOTION_CASH_PAGE_ID")  # 현금 보유액 입력용 페이지
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ==================== 포트폴리오 설정 (Notion 연동 시 폴백용 기본값) ====================
# Notion 보유종목 DB가 설정되어 있으면 실행 시 그 값을 우선 사용합니다.
# Notion 조회가 실패하거나 설정이 없으면 아래 값을 그대로 사용합니다(안전장치).
# 매매(매수/매도) 시 이 블록의 shares, purchase_price만 수정하면 됩니다.
# 신규 종목 매수 시 같은 형식으로 한 줄 추가, 전량 매도 시 해당 줄 삭제하면 됩니다.
#
# 필드 설명:
#   ticker           : 종목코드
#   shares           : 보유 수량
#   purchase_price   : 1주 평균 매입가
#   country          : "KR"(pykrx) 또는 해외("yfinance")
#   category         : 반도체/지수펀드/전력인프라 (TARGET_ALLOCATION과 매칭)
#   stop_loss        : 개별 손절 기준(%). 미지정 시 STOP_LOSS_DEFAULT 적용
#   trailing_stop    : 트레일링 스탑 폭(%p). 미지정 시 트레일링 스탑 미적용
#   trailing_activation : 이 수익률(%) 이상 도달해야 트레일링 스탑 활성화 (HLB처럼 익절 구간에서만 걸고 싶을 때)
#   hold_until_yearend : True면 연말까지 손절 로직에서 제외 (기존 방침 유지 종목)
DEFAULT_PORTFOLIO = {
    "KODEX 200": {
        "ticker": "069500", "shares": 86, "purchase_price": 72276,
        "country": "KR", "category": "지수펀드",
        "stop_loss": -15,
    },
    "SK하이닉스": {
        "ticker": "000660", "shares": 4, "purchase_price": 2474500,
        "country": "KR", "category": "반도체",
        "stop_loss": -30, "trailing_stop": 15,
    },
    "한국전력": {
        "ticker": "015760", "shares": 137, "purchase_price": 44079,
        "country": "KR", "category": "위성종목",
        "hold_until_yearend": True,
    },
    "삼성전자": {
        "ticker": "005930", "shares": 13, "purchase_price": 327538,
        "country": "KR", "category": "반도체",
        "stop_loss": -30, "trailing_stop": 15,
    },
    "HLB": {
        "ticker": "028300", "shares": 75, "purchase_price": 42953,
        "country": "KR", "category": "위성종목",
        "hold_until_yearend": True, "trailing_stop": 10, "trailing_activation": 20,
    },
    "하이브": {
        "ticker": "352820", "shares": 5, "purchase_price": 170400,
        "country": "KR", "category": "위성종목",
    },
}

# 목표 자산 배분 (%) - Claude 추천안
# 근거: AI/HBM 슈퍼사이클은 견조하나 2026 하반기 반도체 쏠림 완화 전망 반영,
#       KODEX 200으로 변동성 완충, 전력인프라는 AI 데이터센터 전력수요 테마 유지,
#       ETC/기타는 하이브 등 소규모 개별 매수 종목을 위한 여유 슬롯(2026-08-01 신설)
# 목표 자산 배분 (%) - 2026-08-03 전면 개편
# 구조: 현금(안전판) / 지수펀드(KODEX200, 변동성 완충) / 반도체(핵심 성장축) /
#       위성종목(전력·바이오·엔터 등 테마 분산, 향후 조선·금융 등 추가 가능)
TARGET_ALLOCATION = {
    "지수펀드": 20,
    "반도체": 30,
    "위성종목": 30,
    "현금": 20,
}

DEFAULT_CASH_AVAILABLE = 3178446  # 토스 계좌 실제 보유 현금 (2026-08-01 기준, 하이브 신규매수 반영) - Notion 미설정시 폴백

# 실행 시점의 실제 포트폴리오/현금 (기본값은 DEFAULT_*, main()에서 Notion 로드 성공 시 덮어씀)
PORTFOLIO = DEFAULT_PORTFOLIO
CASH_AVAILABLE = DEFAULT_CASH_AVAILABLE

MONTHLY_INVESTMENT = 250000

# 손절 기준 참고
# - 개별 반도체주: 연변동성 45% 가정 시 -15%는 기업 이슈 없이도 75% 확률로 도달하는
#   노이즈 수준(자체 시뮬레이션 검증). -30%로 현실화해서 "진짜 지킬 수 있는 기준"으로 설정.
# - 지수펀드(KODEX 200): 분산자산이라 변동성이 낮음, -15% 유지.
STOP_LOSS_DEFAULT = -15  # PORTFOLIO에 stop_loss가 없는 종목의 기본값

VOLATILITY_THRESHOLD = 25  # 최근 변동폭이 이 이상이면 "고변동성 장"으로 판단

# 공격적 리밸런싱 기준
AGGRESSIVE_THRESHOLD = 15
AGGRESSIVE_VOLATILITY_MIN = 20

# 연말까지 보유 방침인 종목 목록 (PORTFOLIO의 hold_until_yearend 플래그로부터 자동 생성)
def get_hold_until_yearend():
    """연말까지 보유 방침인 종목 목록 (PORTFOLIO의 hold_until_yearend 플래그로부터 매번 계산)"""
    return [name for name, info in PORTFOLIO.items() if info.get("hold_until_yearend")]

def get_rebalance_band(target_pct, category=None):
    """
    5/25 Rule: 목표비중이 20% 이상이면 절대 5%p 밴드,
    20% 미만이면 목표치의 상대 25% 밴드를 적용.
    (Swedroe 5/25 Rule, Bogleheads 표준 관행 반영)

    보정: 반도체 카테고리는 내부 2종목(SK하이닉스/삼성전자)이
    같은 업종 매크로 요인에 함께 반응해 상관계수가 높다(추정 0.8+).
    분산투자처럼 보이지만 실질 리스크는 집중에 가까우므로,
    표준 밴드보다 타이트한 4%p를 적용해 더 자주 점검하도록 함.
    """
    if category == "반도체":
        return 4.0
    if target_pct >= 20:
        return 5.0
    return round(target_pct * 0.25, 1)

# ==================== 데이터 조회 함수 ====================

def get_korean_stock_price(ticker):
    try:
        end = now_kst().strftime("%Y%m%d")
        start = (now_kst() - pd.Timedelta(days=15)).strftime("%Y%m%d")
        data = stock.get_market_ohlcv(start, end, ticker)
        data = data[data["종가"] > 0]
        if len(data) == 0:
            return None

        price = int(data["종가"].iloc[-1])
        change = float(data["등락률"].iloc[-1])

        # 이상치 방어 1: 등락률 자체가 비정상(±50% 초과)이면 데이터 오류로 간주
        if abs(change) > 50:
            print(f"경고: {ticker} 등락률 이상치 감지 ({change}%), 이전 거래일 값으로 대체")
            if len(data) >= 2:
                price = int(data["종가"].iloc[-2])
                change = 0.0
            else:
                return None

        # 이상치 방어 2: 최근 5거래일 평균 대비 마지막 종가가 40% 이상 괴리되면
        # 데이터 소스 오류(미확정/캐시 오염) 가능성이 높으므로 직전 확정 종가로 대체
        if len(data) >= 5:
            recent_avg = data["종가"].iloc[-6:-1].mean()  # 최근 종가 제외 5일 평균
            if recent_avg > 0 and abs(price - recent_avg) / recent_avg > 0.40:
                print(f"경고: {ticker} 마지막 종가({price:,})가 최근5일평균({recent_avg:,.0f}) 대비 40% 이상 괴리, 데이터 재검증 필요 - 직전 거래일 값 사용")
                price = int(data["종가"].iloc[-2])
                change = float(data["등락률"].iloc[-2])

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
            end = now_kst().strftime("%Y%m%d")
            start = (now_kst() - pd.Timedelta(days=days*1.5)).strftime("%Y%m%d")
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
    5/25 Rule 적용: 목표 20% 이상은 절대 5%p, 20% 미만은 상대 25% 밴드로 이탈 여부 판단.
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
        band = get_rebalance_band(target_pct, category=cat)
        category_analysis.append({
            "카테고리": cat, "목표비중": target_pct,
            "현재비중": round(current_value / total * 100, 1) if total > 0 else 0,
            "편차": round(deviation_pct, 1), "편차금액": round(deviation),
            "밴드": band, "밴드이탈": abs(deviation_pct) > band,
        })

    return {
        "카테고리분석": category_analysis,
        "총자산포함현금": total,
    }

def calculate_share_level_suggestions(pv, shannon):
    """
    카테고리별 편차금액을, 해당 카테고리 내 종목들의 평가금 비중대로 비례 배분해서
    몇 주를 사고팔아야 하는지 구체적으로 계산.
    """
    suggestions = []
    for c in shannon["카테고리분석"]:
        if c["카테고리"] == "현금":
            continue
        if not c["밴드이탈"]:
            continue  # 5/25 Rule 밴드 이내는 액션 불필요

        cat_items = [i for i in pv["상세"] if i["카테고리"] == c["카테고리"]]
        cat_total_value = sum(i["평가금"] for i in cat_items)
        if cat_total_value == 0:
            continue

        for item in cat_items:
            if item["종목"] in get_hold_until_yearend():
                continue  # 연말 보유 방침 종목은 매수/매도 제안에서 제외
            weight = item["평가금"] / cat_total_value
            item_deviation_value = c["편차금액"] * weight
            shares_to_trade = round(item_deviation_value / item["현재가"]) if item["현재가"] > 0 else 0

            if shares_to_trade == 0:
                continue

            action = "매도" if shares_to_trade > 0 else "매수"
            suggestions.append({
                "카테고리": c["카테고리"], "종목": item["종목"],
                "액션": action, "주수": abs(shares_to_trade),
                "현재가": item["현재가"],
                "금액": abs(shares_to_trade) * item["현재가"],
            })
    return suggestions

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
    """손절 경고 체크 (연말 보유 예외 종목 제외, 종목별 손절 기준은 PORTFOLIO에서 개별 조회)"""
    warnings = []
    for item in pv["상세"]:
        if item["종목"] in get_hold_until_yearend():
            continue
        stock_info = PORTFOLIO.get(item["종목"], {})
        threshold = stock_info.get("stop_loss", STOP_LOSS_DEFAULT)
        if item["수익률"] <= threshold:
            warnings.append(f"{item['종목']}: {item['수익률']:.1f}% (기준 {threshold}% 도달)")
    return warnings

def is_quarterly_review_window():
    """
    분기 시작일(1/1, 4/1, 7/1, 10/1)로부터 3영업일 이내인지 확인.
    이 기간에는 손절 대상 종목의 기준 자체를 재검토하라는 안내를 추가로 띄움.
    """
    today = now_kst()
    quarter_starts = [(1, 1), (4, 1), (7, 1), (10, 1)]
    for month, day in quarter_starts:
        q_start = datetime(today.year, month, day, tzinfo=KST)
        delta = (today - q_start).days
        if 0 <= delta <= 3:
            return True
    return False

def get_quarterly_review_note(stop_warnings):
    """분기 재검토 시점이고 손절 대상이 있으면 재검토 안내 문구 생성"""
    if not stop_warnings or not is_quarterly_review_window():
        return None
    return (
        "분기 시작 시점입니다. 손절 기준에 걸린 종목들을 오늘 감정적으로 판단하지 말고, "
        "다음 관점에서 한 번씩 재검토해보세요: "
        "1) 이 종목을 지금 처음 본다면 이 가격에 사겠는가, "
        "2) 손실 원인이 종목 개별 이슈인가 업종 전체 흐름인가, "
        "3) 손절 기준 자체가 지금 변동성 국면에서도 여전히 타당한가."
    )

# ==================== 트레일링 스탑 (최고 수익률 추적) ====================

PEAK_RECORD_FILE = "peak_profit_record.json"

def load_peak_records():
    """종목별 역대 최고 수익률 기록 로드 (파일이 없으면 빈 딕셔너리)"""
    try:
        with open(PEAK_RECORD_FILE, "r", encoding="utf-8") as f:
            import json
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"최고 수익률 기록 로드 실패: {e}")
        return {}

def save_peak_records(records):
    """종목별 역대 최고 수익률 기록 저장"""
    try:
        import json
        with open(PEAK_RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"최고 수익률 기록 저장 실패: {e}")

def check_trailing_stop(pv):
    """
    트레일링 스탑 체크: 종목별 역대 최고 수익률을 기록해두고,
    PORTFOLIO에 설정된 trailing_stop 폭(%p)만큼 하락하면 익절 경고를 발생.
    trailing_activation이 설정된 종목은 그 수익률을 넘은 적이 있어야만 활성화됨
    (예: HLB는 +20% 이상 도달했던 적이 있어야 트레일링 스탑 작동).
    """
    records = load_peak_records()
    trailing_warnings = []

    for item in pv["상세"]:
        name = item["종목"]
        cur_profit = item["수익률"]
        stock_info = PORTFOLIO.get(name, {})

        prev_peak = records.get(name, cur_profit)
        new_peak = max(prev_peak, cur_profit)
        records[name] = new_peak

        drop_pct = stock_info.get("trailing_stop")
        if drop_pct is None:
            continue  # 트레일링 스탑 미적용 종목

        activation = stock_info.get("trailing_activation")
        if activation is not None and new_peak < activation:
            continue  # 활성화 조건(예: +20%) 미달성 시 트레일링 스탑 비활성

        drawdown_from_peak = new_peak - cur_profit
        if drawdown_from_peak >= drop_pct:
            trailing_warnings.append(
                f"{name}: 최고 수익률 {new_peak:+.1f}% 대비 {drawdown_from_peak:.1f}%p 하락 "
                f"(현재 {cur_profit:+.1f}%, 트레일링 스탑 {drop_pct}%p 도달)"
            )

    save_peak_records(records)
    return trailing_warnings

# ==================== Notion에서 보유종목/현금 읽어오기 ====================

def load_portfolio_from_notion():
    """
    Notion 보유종목 DB에서 종목 정보를 읽어와 PORTFOLIO 형식으로 변환.
    실패하거나 미설정이면 None을 반환 (호출부에서 DEFAULT_PORTFOLIO로 폴백).

    Notion DB 속성 요구사항:
      종목명(Title), 티커(Text), 수량(Number), 평단가(Number), 카테고리(Select),
      연말보유(Checkbox), 손절기준(Number, 선택), 트레일링스탑(Number, 선택), 트레일링활성화(Number, 선택)
    """
    if not NOTION_TOKEN or not NOTION_HOLDINGS_DATABASE_ID:
        return None

    url = f"https://api.notion.com/v1/databases/{NOTION_HOLDINGS_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    try:
        resp = requests.post(url, headers=headers, json={})
        if resp.status_code != 200:
            print(f"Notion 보유종목 조회 실패: {resp.status_code} {resp.text}")
            return None

        results = resp.json().get("results", [])
        portfolio = {}
        for page in results:
            props = page.get("properties", {})

            name_list = props.get("종목명", {}).get("title", [])
            if not name_list:
                continue
            name = name_list[0]["text"]["content"].strip()
            if not name:
                continue

            ticker_list = props.get("티커", {}).get("rich_text", [])
            ticker = ticker_list[0]["text"]["content"].strip() if ticker_list else ""

            shares = props.get("수량", {}).get("number")
            purchase_price = props.get("평단가", {}).get("number")
            if shares is None or purchase_price is None or not ticker:
                print(f"경고: Notion '{name}' 항목에 필수값 누락, 건너뜀")
                continue

            category = props.get("카테고리", {}).get("select", {})
            category = category.get("name", "ETC/기타") if category else "ETC/기타"

            hold_flag = props.get("연말보유", {}).get("checkbox", False)
            stop_loss = props.get("손절기준", {}).get("number")
            trailing_stop = props.get("트레일링스탑", {}).get("number")
            trailing_activation = props.get("트레일링활성화", {}).get("number")

            entry = {
                "ticker": ticker, "shares": int(shares), "purchase_price": int(purchase_price),
                "country": "KR", "category": category,
            }
            if hold_flag:
                entry["hold_until_yearend"] = True
            if stop_loss is not None:
                entry["stop_loss"] = stop_loss
            if trailing_stop is not None:
                entry["trailing_stop"] = trailing_stop
            if trailing_activation is not None:
                entry["trailing_activation"] = trailing_activation

            portfolio[name] = entry

        if not portfolio:
            print("Notion 보유종목 DB가 비어있음, 폴백 사용")
            return None

        print(f"Notion에서 보유종목 {len(portfolio)}개 로드 완료")
        return portfolio
    except Exception as e:
        print(f"Notion 보유종목 조회 중 오류: {e}, 폴백 사용")
        return None

def load_cash_from_notion():
    """
    Notion 페이지에서 현금 보유액을 읽어옴 (페이지 본문 첫 줄이 숫자여야 함).
    실패하거나 미설정이면 None을 반환 (호출부에서 DEFAULT_CASH_AVAILABLE로 폴백).
    """
    if not NOTION_TOKEN or not NOTION_CASH_PAGE_ID:
        return None

    url = f"https://api.notion.com/v1/blocks/{NOTION_CASH_PAGE_ID}/children"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
    }

    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"Notion 현금 조회 실패: {resp.status_code} {resp.text}")
            return None

        blocks = resp.json().get("results", [])
        for block in blocks:
            block_type = block.get("type")
            rich_text = block.get(block_type, {}).get("rich_text", [])
            if not rich_text:
                continue
            text = rich_text[0]["text"]["content"].strip().replace(",", "").replace("원", "")
            if text.isdigit():
                cash = int(text)
                print(f"Notion에서 현금 {cash:,}원 로드 완료")
                return cash
        print("Notion 현금 페이지에서 숫자를 찾지 못함, 폴백 사용")
        return None
    except Exception as e:
        print(f"Notion 현금 조회 중 오류: {e}, 폴백 사용")
        return None

# ==================== Notion 저장 ====================

def save_to_notion(pv, shannon, aggressive, stop_warnings, trailing_warnings):
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        print("Notion 설정 없음, 스킵")
        return False

    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    rebalancing_text = " / ".join([f"{c['카테고리']}: {c['편차']:+.1f}% (밴드±{c['밴드']}%p, {'이탈' if c['밴드이탈'] else '정상'})" for c in shannon["카테고리분석"]])
    aggressive_text = " / ".join([f"{c['종목']}({c['수익률']:+.1f}%): {c['제안']}" for c in aggressive]) if aggressive else "해당 없음"
    stop_loss_text = " / ".join(stop_warnings) if stop_warnings else "없음"
    trailing_text = " / ".join(trailing_warnings) if trailing_warnings else "없음"

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "날짜": {"title": [{"text": {"content": now_kst().strftime("%Y-%m-%d")}}]},
            "총자산": {"number": round(pv["총자산"] + pv["현금"])},
            "수익률": {"number": round(pv["수익률"], 2)},
            "일간수익": {"number": round(sum(i["평가금"] * i["일간등락"] / 100 for i in pv["상세"]))},
            "반도체비중": {"number": next((c["현재비중"] for c in shannon["카테고리분석"] if c["카테고리"] == "반도체"), 0)},
            "현금비중": {"number": next((c["현재비중"] for c in shannon["카테고리분석"] if c["카테고리"] == "현금"), 0)},
            "리밸런싱제안": {"rich_text": [{"text": {"content": rebalancing_text[:2000]}}]},
            "공격리밸런싱대상": {"rich_text": [{"text": {"content": aggressive_text[:2000]}}]},
            "손절경고": {"rich_text": [{"text": {"content": stop_loss_text[:2000]}}]},
            "트레일링스탑": {"rich_text": [{"text": {"content": trailing_text[:2000]}}]},
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

def send_telegram_message(pv, shannon, aggressive, stop_warnings, trailing_warnings, news, psychology_notes):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram 설정 없음, 스킵")
        return False

    today = now_kst().strftime("%Y년 %m월 %d일 (%a)")
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
        emoji = "🔴" if c["밴드이탈"] else "🟢"
        msg += f"{emoji} {c['카테고리']} (목표 {c['목표비중']}%, 밴드 ±{c['밴드']}%p)\n"
        msg += f"  - 현재 {c['현재비중']}% | 편차 {c['편차']:+.1f}%\n"
        if c["카테고리"] == "현금":
            continue
        cat_items = [i for i in pv["상세"] if i["카테고리"] == c["카테고리"]]
        for item in cat_items:
            item_pct = round(item["평가금"] / total_with_cash * 100, 1) if total_with_cash > 0 else 0
            msg += f"    - {item['종목']}: {item_pct}% ({item['수익률']:+.1f}%) 현재가 {item['현재가']:,.0f}원\n"
    msg += "\n"

    # 3. 오늘의 액션 아이템 (구체적 매수/매도 주식수 제시)
    msg += "🎯 오늘의 액션 아이템\n"
    msg += "---------------------\n"
    share_suggestions = calculate_share_level_suggestions(pv, shannon)
    action_num = 1
    if share_suggestions:
        for s in share_suggestions:
            msg += f"{action_num}. [{s['액션']} 검토] {s['종목']} {s['주수']}주 (약 {s['금액']:,.0f}원, 현재가 {s['현재가']:,.0f}원)\n"
            action_num += 1
    cash_cat = next((c for c in shannon["카테고리분석"] if c["카테고리"] == "현금"), None)
    if cash_cat and cash_cat["밴드이탈"] and cash_cat["편차"] < 0:
        msg += f"{action_num}. [현금 확보] 현금 비중 부족 (편차 {cash_cat['편차']:+.1f}%) → 매도 시 일부는 현금으로 남겨두기\n"
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
        hold_list = get_hold_until_yearend()
        if hold_list:
            msg += f"({'·'.join(hold_list)}는 연말 보유 방침에 따라 제외)\n"
        quarterly_note = get_quarterly_review_note(stop_warnings)
        if quarterly_note:
            msg += f"\n📅 분기 재검토: {quarterly_note}\n"
        msg += "\n"

    # 4-1. 트레일링 스탑 (익절 타이밍 자동 방어)
    if trailing_warnings:
        msg += "📈 트레일링 스탑 발동 (익절 검토)\n"
        msg += "---------------------\n"
        for w in trailing_warnings:
            msg += f"📈 {w}\n"
        msg += "최고점 대비 정해둔 폭만큼 빠졌습니다. 추가 하락 전 일부 익절을 검토하세요.\n\n"

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

    # 7. 매매 기록 안내 (자동 반영은 안 되지만 기록 유도)
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

def main():
    global PORTFOLIO, CASH_AVAILABLE

    print(f"포트폴리오 자동화 시작: {now_kst()}")

    # Notion에서 보유종목/현금 로드 시도, 실패하면 DEFAULT_* 값 그대로 사용(안전장치)
    notion_portfolio = load_portfolio_from_notion()
    if notion_portfolio:
        PORTFOLIO = notion_portfolio
    notion_cash = load_cash_from_notion()
    if notion_cash is not None:
        CASH_AVAILABLE = notion_cash

    pv = calculate_portfolio_value()
    shannon = calculate_shannon_rebalancing(pv)
    aggressive = calculate_aggressive_rebalancing(pv)
    stop_warnings = check_stop_loss(pv)
    trailing_warnings = check_trailing_stop(pv)
    news = collect_all_news(pv)
    psychology_notes = get_market_psychology_note(pv, shannon)

    save_to_notion(pv, shannon, aggressive, stop_warnings, trailing_warnings)
    send_telegram_message(pv, shannon, aggressive, stop_warnings, trailing_warnings, news, psychology_notes)

    print("완료")

if __name__ == "__main__":
    main()
