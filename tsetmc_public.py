from datetime import datetime, timezone
import asyncio, httpx
from .base import MarketDataProvider

class TsetmcPublicProvider(MarketDataProvider):
    # Live TSETMC endpoints; no demo values are generated.
    def __init__(self, base_url, timeout=12):
        self.base=base_url.rstrip("/")
        self.client=httpx.AsyncClient(timeout=timeout,headers={"User-Agent":"Mozilla/5.0 Personal-Bourse-Dashboard/1.0"})

    async def _get(self,path):
        r=await self.client.get(self.base+path)
        r.raise_for_status()
        return r.json()

    async def search(self,query):
        data=await self._get(f"/api/Instrument/GetInstrumentSearch/{query}")
        return data.get("instrumentSearch",data.get("instrumentSearchDto",[]))

    async def _find(self,symbol):
        rows=await self.search(symbol)
        exact=[x for x in rows if x.get("lVal18AFC")==symbol]
        return (exact or rows)[:1]

    async def quotes(self,symbols):
        results=[]
        for symbol in symbols:
            matches=await self._find(symbol)
            if not matches:
                results.append({"symbol":symbol,"found":False,"error":"نماد یافت نشد."}); continue
            ins=matches[0]
            code=str(ins.get("insCode") or "")
            if not code:
                results.append({"symbol":symbol,"found":False,"error":"کد نماد دریافت نشد."}); continue
            try:
                info=await self._get(f"/api/ClosingPrice/GetClosingPriceInfo/{code}")
                cp=info.get("closingPriceInfo",info)
                try:
                    ct0=await self._get(f"/api/ClientType/GetClientType/{code}/1/0")
                    ct=ct0.get("clientType",ct0)
                except Exception:
                    ct={}
                results.append(self.map_quote(symbol,ins,cp,ct))
            except Exception as e:
                results.append({"symbol":symbol,"found":True,"error":f"خطا در دریافت داده بازار: {type(e).__name__}",
                                "updated_at":datetime.now(timezone.utc).isoformat()})
            await asyncio.sleep(.15)
        return results

    def map_quote(self,symbol,ins,cp,ct):
        def get(*keys):
            for k in keys:
                if cp.get(k) is not None:
                    try:return float(cp[k])
                    except: return None
            return None
        def f(v):
            try:return float(v)
            except:return None
        last=get("pDrCotVal","pl"); close=get("pClosing","pc"); prev=get("priceYesterday","py")
        flow=None
        if all(f(ct.get(k)) is not None for k in ["buy_I_Volume","sell_I_Volume"]):
            flow=f(ct["buy_I_Volume"])-f(ct["sell_I_Volume"])
        return {
            "symbol":symbol,"name":ins.get("lVal30") or symbol,"found":True,"ins_code":str(ins.get("insCode","")),
            "last_price":last,"close_price":close,"previous_close":prev,
            "change_percent":((last-prev)/prev*100) if last is not None and prev not in (None,0) else None,
            "volume":get("qTotTran5J"),"trade_value":get("qTotCap"),"trades":get("zTotTran"),
            "first_price":get("priceFirst","pf"),
            "buyers_count":f(ct.get("buy_CountI")),"sellers_count":f(ct.get("sell_CountI")),
            "buy_individual_volume":f(ct.get("buy_I_Volume")),
            "sell_individual_volume":f(ct.get("sell_I_Volume")),"money_flow":flow,
            "is_buy_queue":None,"is_sell_queue":None,"best_bid_value":None,"best_ask_value":None,
            "updated_at":datetime.now(timezone.utc).isoformat()
        }

    async def close(self): await self.client.aclose()
