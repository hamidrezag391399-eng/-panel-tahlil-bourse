from typing import Any

def num(v: Any):
    try:
        if v is None or v == "": return None
        return float(v)
    except (TypeError, ValueError):
        return None

def normalize_status(q):
    if q.get("is_buy_queue") is True:
        return "buy_queue", "صف خرید — امکان خرید عادی وجود ندارد"
    if q.get("is_sell_queue") is True:
        return "sell_queue", "صف فروش"
    bid, ask = num(q.get("best_bid_value")), num(q.get("best_ask_value"))
    if bid is not None and bid > 0 and ask in (None, 0):
        return "buy_queue", "صف خرید — امکان خرید عادی وجود ندارد"
    if ask is not None and ask > 0 and bid in (None, 0):
        return "sell_queue", "صف فروش"
    if num(q.get("last_price")) is None and num(q.get("close_price")) is None:
        return "unknown", "وضعیت معامله نامشخص"
    return "normal", "قابل معامله عادی"

def calculate_score(q):
    factors = [
        ("ارزش معاملات", 25, q.get("trade_value_score")),
        ("حجم معاملات", 15, q.get("volume_score")),
        ("ورود پول", 20, q.get("money_flow_score")),
        ("قدرت خریدار", 15, q.get("buyer_power_score")),
        ("روند قیمت", 15, q.get("price_trend_score")),
        ("وضعیت صف", 10, q.get("queue_score")),
    ]
    usable = [(n,w,num(v)) for n,w,v in factors if num(v) is not None]
    if not usable:
        return None, [{"name":n,"weight":w,"value":None} for n,w,_ in factors]
    total = sum(w for _,w,_ in usable)
    score = sum(w*max(0,min(100,v)) for _,w,v in usable) / total
    return round(score,1), [{"name":n,"weight":w,"value":num(v)} for n,w,v in factors]

def build_analysis(q):
    out=[]
    ch=num(q.get("change_percent"))
    flow=num(q.get("money_flow"))
    out.append("تغییر قیمت: " + (f"{ch:.2f}%" if ch is not None else "داده در دسترس نیست"))
    out.append("ارزش معاملات: " + ("دریافت شده" if num(q.get("trade_value")) is not None else "داده در دسترس نیست"))
    out.append("ورود/خروج پول: " + ("دریافت شده" if flow is not None else "داده در دسترس نیست"))
    if q["status"]=="buy_queue": out.append("صف خرید فعال است؛ خرید عادی در این وضعیت ممکن نیست.")
    elif q["status"]=="sell_queue": out.append("صف فروش فعال است.")
    elif q["status"]=="normal": out.append("نماد صف خرید/فروش فعال ندارد.")
    else: out.append("وضعیت معاملاتی به علت کمبود داده مشخص نیست.")
    return out

def enrich_quote(q):
    q["status"], q["status_text"] = normalize_status(q)
    last, prev = num(q.get("last_price")), num(q.get("previous_close"))
    if q.get("change_percent") is None and last is not None and prev not in (None,0):
        q["change_percent"] = round((last-prev)/prev*100,2)
    q["analysis_score"], q["score_factors"] = calculate_score(q)
    q["analysis"] = build_analysis(q)
    return q
