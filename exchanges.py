"""거래소 어댑터 — 청산 포지션을 공통 스키마로 정규화.

공통 리턴 (positions):
  {ext_id, symbol, side(long|short), open_time, close_time, volume,
   leverage, open_price, close_price, realized, fee, funding}
realized = 순실현(수수료·펀딩 반영된 값이 있으면 그 값). 단위 USDT.

MEXC  — contract.mexc.com 직접 서명 (검증 완료).
Binance/Bybit/Bitget/Gate — ccxt 암시적 엔드포인트. 실키 검증 전이므로
오류는 sync_log로 노출하고 죽지 않는다.
"""
import hashlib
import hmac
import time

import requests

SUPPORTED = ["mexc", "binance", "bybit", "bitget", "gate"]

MS_DAY = 86_400_000


# ---------------------------------------------------------------- MEXC
class MexcDirect:
    BASE = "https://contract.mexc.com"

    def __init__(self, key, secret, passphrase=None):
        self.key, self.secret = key, secret

    def _get(self, path, params=""):
        req_time = str(int(time.time() * 1000))
        sig = hmac.new(
            self.secret.encode(),
            (self.key + req_time + params).encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "ApiKey": self.key,
            "Request-Time": req_time,
            "Signature": sig,
            "Content-Type": "application/json",
        }
        url = self.BASE + path + (("?" + params) if params else "")
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        d = r.json()
        if not d.get("success"):
            raise RuntimeError(f"MEXC {path}: {d.get('code')} {d.get('message')}")
        return d.get("data")

    def _pull_all(self, path):
        out, page = [], 1
        while True:
            data = self._get(path, f"page_num={page}&page_size=100")
            rows = data.get("resultList") if isinstance(data, dict) else (data or [])
            rows = rows or []
            out.extend(rows)
            if isinstance(data, dict):
                if page >= int(data.get("totalPage") or 1):
                    break
            elif len(rows) < 100:
                break
            page += 1
            time.sleep(0.25)
        return out

    def fetch(self, since_ms):
        hist = self._pull_all("/api/v1/private/position/list/history_positions")
        positions = []
        for h in hist:
            close_time = h.get("updateTime") or h["createTime"]
            if close_time < since_ms:
                continue
            positions.append({
                "ext_id": h["positionId"],
                "symbol": h["symbol"],
                "side": "long" if h.get("positionType") == 1 else "short",
                "open_time": h.get("createTime"),
                "close_time": close_time,
                "volume": h.get("closeVol"),
                "leverage": h.get("leverage"),
                "open_price": h.get("openAvgPrice"),
                "close_price": h.get("closeAvgPrice"),
                "realized": float(h.get("realised") or 0),
                "fee": float(h.get("totalFee") or 0),
                "funding": float(h.get("holdFee") or 0),
            })
        return positions, []  # 수수료·펀딩이 포지션에 포함 → cashflow 없음


# ---------------------------------------------------------------- ccxt 공통
def _ccxt(exchange, key, secret, passphrase=None):
    import ccxt
    cls = {"binance": "binanceusdm", "bybit": "bybit",
           "bitget": "bitget", "gate": "gate"}[exchange]
    args = {"apiKey": key, "secret": secret, "enableRateLimit": True,
            "options": {"defaultType": "swap"}}
    if passphrase:
        args["password"] = passphrase
    return getattr(ccxt, cls)(args)


# ---------------------------------------------------------------- Binance
class BinanceAdapter:
    """USDⓈ-M income 기록 기반.
    REALIZED_PNL 이벤트 = 청산 1건으로 취급(포지션 단위 아님 — 근사),
    COMMISSION/FUNDING_FEE 는 cashflow로 별도 저장."""

    def __init__(self, key, secret, passphrase=None):
        self.ex = _ccxt("binance", key, secret)

    def _income(self, income_type, since_ms):
        out, start = [], since_ms
        while True:
            batch = self.ex.fapiPrivateGetIncome({
                "incomeType": income_type, "startTime": start, "limit": 1000,
            })
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 1000:
                break
            start = int(batch[-1]["time"]) + 1
            time.sleep(0.3)
        return out

    def fetch(self, since_ms):
        positions = []
        for r in self._income("REALIZED_PNL", since_ms):
            positions.append({
                "ext_id": f"rpnl-{r.get('tranId') or r['time']}-{r.get('tradeId','')}",
                "symbol": r.get("symbol") or "?",
                "side": None,
                "open_time": None,
                "close_time": int(r["time"]),
                "volume": None, "leverage": None,
                "open_price": None, "close_price": None,
                "realized": float(r["income"]),
                "fee": 0.0, "funding": 0.0,
            })
        flows = []
        for kind, itype in (("fee", "COMMISSION"), ("funding", "FUNDING_FEE")):
            for r in self._income(itype, since_ms):
                flows.append({
                    "ext_id": f"{kind}-{r.get('tranId') or r['time']}-{r.get('tradeId','')}",
                    "kind": kind, "symbol": r.get("symbol"),
                    "amount": float(r["income"]), "time": int(r["time"]),
                })
        return positions, flows


# ---------------------------------------------------------------- Bybit
class BybitAdapter:
    """v5 closed-pnl (linear). closedPnl = 수수료 반영 순실현."""

    def __init__(self, key, secret, passphrase=None):
        self.ex = _ccxt("bybit", key, secret)

    def fetch(self, since_ms):
        positions = []
        end = int(time.time() * 1000)
        start = since_ms
        while start < end:  # closed-pnl 은 조회창 7일 제한
            win_end = min(start + 7 * MS_DAY - 1, end)
            cursor = ""
            while True:
                params = {"category": "linear", "startTime": start,
                          "endTime": win_end, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                res = self.ex.privateGetV5PositionClosedPnl(params)
                result = res.get("result") or {}
                for r in result.get("list") or []:
                    positions.append({
                        "ext_id": r.get("orderId") or f"{r['symbol']}-{r['updatedTime']}",
                        "symbol": r["symbol"],
                        # closed-pnl의 side = 청산 주문 방향 → 포지션은 반대
                        "side": "long" if r.get("side") == "Sell" else "short",
                        "open_time": int(r.get("createdTime") or 0) or None,
                        "close_time": int(r.get("updatedTime") or r.get("createdTime")),
                        "volume": float(r.get("qty") or 0),
                        "leverage": float(r.get("leverage") or 0) or None,
                        "open_price": float(r.get("avgEntryPrice") or 0) or None,
                        "close_price": float(r.get("avgExitPrice") or 0) or None,
                        "realized": float(r.get("closedPnl") or 0),
                        "fee": abs(float(r.get("openFee") or 0)) + abs(float(r.get("closeFee") or 0)),
                        "funding": 0.0,
                    })
                cursor = result.get("nextPageCursor") or ""
                if not cursor:
                    break
                time.sleep(0.25)
            start = win_end + 1
        return positions, []


# ---------------------------------------------------------------- Bitget
class BitgetAdapter:
    """v2 mix history-position. netProfit = 수수료·펀딩 반영 순실현."""

    def __init__(self, key, secret, passphrase=None):
        self.ex = _ccxt("bitget", key, secret, passphrase)

    def fetch(self, since_ms):
        positions, id_less = [], None
        while True:
            params = {"productType": "USDT-FUTURES", "startTime": str(since_ms),
                      "endTime": str(int(time.time() * 1000)), "limit": "100"}
            if id_less:
                params["idLessThan"] = id_less
            res = self.ex.privateMixGetV2MixPositionHistoryPosition(params)
            data = res.get("data") or {}
            rows = data.get("list") or []
            for r in rows:
                ext = r.get("positionId") or f"{r.get('symbol')}-{r.get('utime') or r.get('uTime')}"
                fee = abs(float(r.get("openFee") or 0)) + abs(float(r.get("closeFee") or 0))
                positions.append({
                    "ext_id": ext,
                    "symbol": r.get("symbol") or "?",
                    "side": "long" if (r.get("holdSide") == "long") else "short",
                    "open_time": int(r.get("ctime") or r.get("cTime") or 0) or None,
                    "close_time": int(r.get("utime") or r.get("uTime") or r.get("ctime") or r.get("cTime")),
                    "volume": float(r.get("closeTotalPos") or r.get("openTotalPos") or 0) or None,
                    "leverage": None,
                    "open_price": float(r.get("openAvgPrice") or 0) or None,
                    "close_price": float(r.get("closeAvgPrice") or 0) or None,
                    "realized": float(r.get("netProfit") or r.get("pnl") or 0),
                    "fee": fee,
                    "funding": float(r.get("totalFunding") or 0),
                })
            end_id = data.get("endId")
            if not rows or not end_id or len(rows) < 100:
                break
            id_less = end_id
            time.sleep(0.25)
        return positions, []


# ---------------------------------------------------------------- Gate
class GateAdapter:
    """futures position_close (usdt settle)."""

    def __init__(self, key, secret, passphrase=None):
        self.ex = _ccxt("gate", key, secret)

    def fetch(self, since_ms):
        positions, offset = [], 0
        since_s = since_ms // 1000
        while True:
            rows = self.ex.privateFuturesGetSettlePositionClose({
                "settle": "usdt", "limit": 100, "offset": offset,
                "from": since_s,
            })
            if not rows:
                break
            for r in rows:
                t_ms = int(float(r["time"]) * 1000)
                pnl = float(r.get("pnl") or 0)
                fee = abs(float(r.get("pnl_fee") or 0))
                funding = float(r.get("pnl_fund") or 0)
                positions.append({
                    "ext_id": f"{r.get('contract')}-{r['time']}-{offset}-{r.get('accum_size','')}",
                    "symbol": r.get("contract") or "?",
                    "side": r.get("side") or None,
                    "open_time": int(float(r.get("first_open_time") or 0) * 1000) or None,
                    "close_time": t_ms,
                    "volume": float(r.get("accum_size") or 0) or None,
                    "leverage": None,
                    "open_price": None, "close_price": None,
                    "realized": pnl,
                    "fee": fee,
                    "funding": funding,
                })
            if len(rows) < 100:
                break
            offset += len(rows)
            time.sleep(0.25)
        return positions, []


ADAPTERS = {
    "mexc": MexcDirect,
    "binance": BinanceAdapter,
    "bybit": BybitAdapter,
    "bitget": BitgetAdapter,
    "gate": GateAdapter,
}


def fetch_closed(exchange: str, key: str, secret: str, passphrase: str | None,
                 since_ms: int):
    """(positions, cashflows) 리턴. exchange ∈ SUPPORTED."""
    adapter = ADAPTERS[exchange](key, secret, passphrase)
    return adapter.fetch(since_ms)
