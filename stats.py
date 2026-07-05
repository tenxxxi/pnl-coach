"""통계 + 습관 진단(룰 기반) — 승률 개선이 목적."""
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from db import get_db

KST = timezone(timedelta(hours=9))


def _rows(user_id: int, since_ms: int):
    return get_db().execute(
        """SELECT * FROM positions WHERE user_id=? AND close_time>=?
           ORDER BY close_time""",
        (user_id, since_ms),
    ).fetchall()


def compute_stats(user_id: int, since_ms: int) -> dict:
    rows = _rows(user_id, since_ms)
    flows = get_db().execute(
        "SELECT kind, SUM(amount) s FROM cashflows WHERE user_id=? AND time>=? GROUP BY kind",
        (user_id, since_ms),
    ).fetchall()
    flow = {r["kind"]: float(r["s"] or 0) for r in flows}

    net = sum(r["realized"] for r in rows)
    fees = sum(r["fee"] or 0 for r in rows) + abs(flow.get("fee", 0))
    funding = sum(r["funding"] or 0 for r in rows) + flow.get("funding", 0)
    wins = [r for r in rows if r["realized"] > 0]
    losses = [r for r in rows if r["realized"] <= 0]
    win_sum = sum(r["realized"] for r in wins)
    loss_sum = sum(r["realized"] for r in losses)

    by_symbol = defaultdict(lambda: {"net": 0.0, "count": 0, "wins": 0})
    for r in rows:
        s = by_symbol[r["symbol"]]
        s["net"] += r["realized"]
        s["count"] += 1
        s["wins"] += 1 if r["realized"] > 0 else 0

    by_month = defaultdict(float)
    by_weekday = defaultdict(lambda: {"net": 0.0, "count": 0})
    by_hour = defaultdict(lambda: {"net": 0.0, "count": 0})
    daily = defaultdict(float)
    for r in rows:
        dt = datetime.fromtimestamp(r["close_time"] / 1000, KST)
        by_month[dt.strftime("%Y-%m")] += r["realized"]
        by_weekday[dt.weekday()]["net"] += r["realized"]
        by_weekday[dt.weekday()]["count"] += 1
        by_hour[dt.hour]["net"] += r["realized"]
        by_hour[dt.hour]["count"] += 1
        daily[dt.strftime("%Y-%m-%d")] += r["realized"]

    # 누적 곡선 (일 단위)
    curve, acc = [], 0.0
    for day in sorted(daily):
        acc += daily[day]
        curve.append({"date": day, "cum": round(acc, 2)})

    # 홀딩 시간 (open_time 있는 것만)
    hold_w = [ (r["close_time"] - r["open_time"]) / 60000
               for r in wins if r["open_time"] ]
    hold_l = [ (r["close_time"] - r["open_time"]) / 60000
               for r in losses if r["open_time"] ]

    # 선물지갑 이체 (USDT 기준 + 기타 통화 건수)
    xrows = get_db().execute(
        """SELECT direction, currency, SUM(amount) s, COUNT(*) c FROM transfers
           WHERE user_id=? AND time>=? GROUP BY direction, currency""",
        (user_id, since_ms),
    ).fetchall()
    t_in = sum(r["s"] for r in xrows if r["direction"] == "IN" and r["currency"] == "USDT")
    t_out = sum(r["s"] for r in xrows if r["direction"] == "OUT" and r["currency"] == "USDT")
    t_other = sum(r["c"] for r in xrows if r["currency"] != "USDT")
    transfers = {
        "in": round(t_in, 2),
        "out": round(t_out, 2),
        "net": round(t_in - t_out, 2),
        "withdrawn_net": round(t_out - t_in, 2),   # 현물로 순회수 (양수 = 뺀 게 많음)
        "count": sum(r["c"] for r in xrows),
        "other_currency_count": t_other,
    }

    return {
        "n": len(rows),
        "transfers": transfers,
        "retained": round(net - (t_out - t_in), 2),  # 순실현 중 선물지갑에 남은 증분
        "net": round(net, 2),
        "fees": round(fees, 2),
        "funding": round(funding, 2),
        "win_rate": round(len(wins) / len(rows) * 100, 1) if rows else 0,
        "profit_factor": round(win_sum / abs(loss_sum), 2) if loss_sum else None,
        "avg_win": round(win_sum / len(wins), 2) if wins else 0,
        "avg_loss": round(loss_sum / len(losses), 2) if losses else 0,
        "win_sum": round(win_sum, 2),
        "loss_sum": round(loss_sum, 2),
        "avg_hold_win_min": round(sum(hold_w) / len(hold_w), 1) if hold_w else None,
        "avg_hold_loss_min": round(sum(hold_l) / len(hold_l), 1) if hold_l else None,
        "symbols": sorted(
            [{"symbol": k, **{kk: round(vv, 2) if isinstance(vv, float) else vv
                              for kk, vv in v.items()}}
             for k, v in by_symbol.items()],
            key=lambda x: x["net"]),
        "monthly": [{"month": m, "net": round(by_month[m], 2)} for m in sorted(by_month)],
        "weekday": [{"weekday": d, "net": round(by_weekday[d]["net"], 2),
                     "count": by_weekday[d]["count"]} for d in sorted(by_weekday)],
        "hourly": [{"hour": h, "net": round(by_hour[h]["net"], 2),
                    "count": by_hour[h]["count"]} for h in sorted(by_hour)],
        "curve": curve,
        "exchanges": [dict(r) for r in get_db().execute(
            "SELECT exchange, status, last_sync, message FROM sync_log WHERE user_id=?",
            (user_id,)).fetchall()],
    }


def diagnose(user_id: int, since_ms: int, stats: dict) -> list[dict]:
    """룰 기반 습관 진단. [{severity, title, detail}]"""
    rows = _rows(user_id, since_ms)
    out = []
    if not rows:
        return [{"severity": "info", "title": "데이터 없음",
                 "detail": "거래소 키 등록 후 동기화하면 진단이 나옵니다."}]

    # 1. 심볼 난사
    syms = stats["symbols"]
    n_sym = len(syms)
    losers = [s for s in syms if s["net"] < 0]
    if n_sym >= 30:
        top3 = sum(s["net"] for s in syms[-3:])
        bleed = sum(s["net"] for s in losers)
        out.append({
            "severity": "critical", "title": f"심볼 난사 — {n_sym}개 종목 거래",
            "detail": (f"수익 상위 3종목이 {top3:+,.0f} USDT 버는 동안 "
                       f"손실 종목 {len(losers)}개가 {bleed:+,.0f} USDT 반납. "
                       "엣지가 확인된 소수 종목으로 줄이는 게 승률 개선 1순위."),
        })

    # 2. 물타기/역추세 홀딩 (손실 홀딩 > 익절 홀딩)
    hw, hl = stats.get("avg_hold_win_min"), stats.get("avg_hold_loss_min")
    if hw and hl and hl > hw * 2:
        out.append({
            "severity": "serious", "title": "손실 포지션을 더 오래 들고 있음",
            "detail": (f"익절 평균 {hw:.0f}분 vs 손절 평균 {hl:.0f}분. "
                       "이익은 빨리 자르고 손실은 버티는 전형적 처분효과. "
                       "진입 시 손절가 고정 권장."),
        })

    # 3. 복수매매 (손실 후 30분 내 재진입 빈도)
    revenge = 0
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if prev["realized"] < 0 and cur["open_time"] \
           and 0 <= cur["open_time"] - prev["close_time"] <= 30 * 60000:
            revenge += 1
    if rows and revenge / len(rows) > 0.25:
        out.append({
            "severity": "serious", "title": f"복수매매 의심 — 손실 직후 재진입 {revenge}회",
            "detail": "손실 후 30분 내 신규 진입 비중이 높음. 손실 후 강제 휴식 룰 권장.",
        })

    # 4. 과도 레버리지
    levs = [r["leverage"] for r in rows if r["leverage"]]
    if levs:
        high = [l for l in levs if l >= 100]
        if len(high) / len(levs) > 0.3:
            out.append({
                "severity": "serious",
                "title": f"고레버리지 비중 {len(high)/len(levs)*100:.0f}%",
                "detail": "100배 이상 진입이 많음. 수수료·슬리피지가 증폭돼 승률 필요치가 올라감.",
            })

    # 5. 시간대 약점
    hours = [h for h in stats["hourly"] if h["count"] >= 5]
    if hours:
        worst = min(hours, key=lambda h: h["net"])
        if worst["net"] < 0 and abs(worst["net"]) > abs(stats["net"]) * 0.3:
            out.append({
                "severity": "warning",
                "title": f"{worst['hour']}시(KST) 취약",
                "detail": (f"해당 시간대 순손익 {worst['net']:+,.0f} USDT ({worst['count']}건). "
                           "이 시간대 진입 중단만으로 개선 여지."),
            })

    # 6. 수수료+펀딩 비중
    cost = stats["fees"] - min(stats["funding"], 0)
    if stats["win_sum"] and cost > stats["win_sum"] * 0.2:
        out.append({
            "severity": "warning", "title": "비용이 총익의 20% 초과",
            "detail": f"수수료 {stats['fees']:,.0f} + 펀딩 부담. 지정가 주문 비중 늘릴 것.",
        })

    if not out:
        out.append({"severity": "good", "title": "뚜렷한 악습 미검출",
                    "detail": "표본이 쌓이면 진단이 정교해집니다."})
    return out
