from pathlib import Path
import json, os, sqlite3
from datetime import datetime, timezone
from contextlib import asynccontextmanager
import asyncio
import httpx
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from starlette.middleware.sessions import SessionMiddleware

class Settings(BaseSettings):
    data_provider: str = "tsetmc_public"
    tsetmc_base_url: str = "https://cdn.tsetmc.com"
    app_username: str = "admin"
    app_password: str = "change-me"
    session_secret: str = "change-me"
    request_timeout: float = 20

settings = Settings()
DB = Path("data/app.db"); DB.parent.mkdir(exist_ok=True)
security = HTTPBasic()

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT,payload TEXT)")
    c.commit(); c.close()

class TsetmcPublicProvider:
    def __init__(self, base_url, timeout=20):
        self.base = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent":"Mozilla/5.0 Personal-Bourse-Dashboard/1.0"})
    async def _get(self, path):
        r = await self.client.get(self.base + path)
        r.raise_for_status()
        return r.json()
    async def search(self, query):
        data = await self._get(f"/api/Instrument/GetInstrumentSearch/{query}")
        return data.get("instrumentSearch", data.get("instrumentSearchDto", []))
    async def _find(self, symbol):
        rows = await self.search(symbol)
        exact = [x for x in rows if x.get("lVal18AFC") == symbol]
        return (exact or rows)[:1]
    async def quotes(self, symbols):
        results = []
        for symbol in symbols:
            matches = await self._find(symbol)
            if not matches:
                results.append({"symbol":symbol,"found":False,"error":"نماد یافت نشد."})
                continue
            ins = matches[0]
            code = str(ins.get("insCode",""))
            try:
                cp = await self._get(f"/api/ClosingPrice/GetClosingPriceInfo/{code}")
                ct = await self._get(f"/api/ClientType/GetClientType/{code}/1/0")
            except Exception:
                results.append({"symbol":symbol,"found":False,"error":"دریافت اطلاعات نماد ناموفق بود."})
                continue
            dto = cp.get("closingPriceInfo", cp.get("closingPriceInfoDto", cp))
            if isinstance(dto, list): dto = dto[0] if dto else {}
            if not isinstance(dto, dict): dto = {}
            if isinstance(ct, dict):
                ct = ct.get("clientType", ct.get("clientTypeDto", ct))
            if isinstance(ct, list): ct = ct[0] if ct else {}
            if not isinstance(ct, dict): ct = {}
            def f(v):
                try:
                    return float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None
            def get(*keys):
                for k in keys:
                    if k in dto and dto[k] not in (None, ""): return f(dto[k])
                return None
            last = get("pClosing","pDrCotVal")
            close = get("pClosing","pClosingPrice")
            prev = get("priceYesterday","pPriceYesterday")
            flow = None
            if ct:
                flow = f(ct.get("buy_I_Volume")) - f(ct.get("sell_I_Volume")) if f(ct.get("buy_I_Volume")) is not None and f(ct.get("sell_I_Volume")) is not None else None
            results.append({
                "symbol":symbol,"name":ins.get("lVal30") or symbol,"found":True,"ins_code":code,
                "last_price":last,"close_price":close,"previous_close":prev,
                "change_percent":((last-prev)/prev*100) if last is not None and prev not in (None,0) else None,
                "volume":get("qTotTran5J"),"trade_value":get("qTotCap"),"trades":get("zTotTran"),
                "first_price":get("priceFirst","pf"),
                "buyers_count":f(ct.get("buy_CountI")),"sellers_count":f(ct.get("sell_CountI")),
                "buy_individual_volume":f(ct.get("buy_I_Volume")),
                "sell_individual_volume":f(ct.get("sell_I_Volume")),"money_flow":flow,
                "is_buy_queue":None,"is_sell_queue":None,"best_bid_value":None,"best_ask_value":None,
                "updated_at":datetime.now(timezone.utc).isoformat()
            })
        return results
    async def close(self): await self.client.aclose()

def num(v):
    try:
        if v is None or v == "": return None
        return float(v)
    except (TypeError, ValueError): return None

def normalize_status(q):
    if q.get("is_buy_queue") is True: return "buy_queue", "صف خرید — امکان خرید عادی وجود ندارد"
    if q.get("is_sell_queue") is True: return "sell_queue", "صف فروش"
    bid, ask = num(q.get("best_bid_value")), num(q.get("best_ask_value"))
    if bid is not None and bid > 0 and ask in (None, 0): return "buy_queue", "صف خرید — امکان خرید عادی وجود ندارد"
    if ask is not None and ask > 0 and bid in (None, 0): return "sell_queue", "صف فروش"
    if num(q.get("last_price")) is None and num(q.get("close_price")) is None: return "unknown", "وضعیت معامله نامشخص"
    return "normal", "قابل معامله عادی"

def calculate_score(q):
    factors=[("ارزش معاملات",25,q.get("trade_value_score")),("حجم معاملات",15,q.get("volume_score")),
             ("ورود پول",20,q.get("money_flow_score")),("قدرت خریدار",15,q.get("buyer_power_score")),
             ("روند قیمت",15,q.get("price_trend_score")),("وضعیت صف",10,q.get("queue_score"))]
    available=[(n,w,num(v)) for n,w,v in factors if num(v) is not None]
    if not available: return None, [{"name":n,"weight":w,"score":None} for n,w,_ in factors]
    total=sum(w for _,w,_ in available)
    score=sum(s*w for _,w,s in available)/total
    return round(score,1), [{"name":n,"weight":w,"score":s} for n,w,s in factors]

def build_analysis(q):
    out=[]
    ch=num(q.get("change_percent")); flow=num(q.get("money_flow"))
    out.append("تغییر قیمت: " + (f"{ch:.2f}٪" if ch is not None else "داده در دسترس نیست"))
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

HTML = '<!doctype html>\n<html lang="fa" dir="rtl">\n<head>\n<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n<meta name="theme-color" content="#070b14"><title>📊 پنل شخصی تحلیل بورس ایران</title>\n<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">\n<link rel="stylesheet" href="/static/styles.css">\n</head>\n<body>\n<div class="ambient a1"></div><div class="ambient a2"></div>\n<div class="shell">\n<header class="hero">\n  <div class="brand"><div class="brand-mark">📊</div><div><div class="eyebrow">PERSONAL MARKET INTELLIGENCE</div><h1>پنل شخصی تحلیل بورس ایران</h1><p>سیستم تحلیل و فیلتر هوشمند سهام بورس ایران</p></div></div>\n  <div class="hero-actions"><span class="live-pill"><i></i> بازار زنده</span><button id="refresh" class="glass-btn">↻ بروزرسانی</button></div>\n</header>\n<section class="panel command-panel">\n  <div class="section-head"><div><span class="mini-label">WATCHLIST</span><h2>نمادهای موردنظر</h2></div><span class="hint">چند نماد را با فاصله یا Enter وارد کنید</span></div>\n  <textarea id="symbols" placeholder="فولاد   فملی   شستا   خودرو   وبملت   ذوب"></textarea><div id="tags" class="tags"></div>\n  <div class="actions"><button id="analyze" class="primary">تأیید و تحلیل <span>→</span></button><button id="clear" class="ghost">پاک کردن همه</button></div>\n  <div id="loading" class="loading hidden"><span class="spinner"></span>در حال دریافت اطلاعات بازار…</div><div id="error" class="error hidden"></div>\n</section>\n<section id="summary" class="cards"></section>\n<section class="panel market-panel">\n <div class="section-head"><div><span class="mini-label">MARKET SCREENER</span><h2>نمای بازار</h2></div><span class="hint">مرتب\u200cسازی بر اساس ارزش معاملات</span></div>\n <div class="filters"><button data-filter="all" class="filter active">همه</button><button data-filter="buy_queue" class="filter">🟢 صف خرید</button><button data-filter="sell_queue" class="filter">🔴 صف فروش</button><button data-filter="normal" class="filter">قابل معامله</button><button data-filter="positive" class="filter">مثبت\u200cها</button><button data-filter="negative" class="filter">منفی\u200cها</button><button data-filter="money_in" class="filter">ورود پول</button><button data-filter="money_out" class="filter">خروج پول</button></div>\n <div class="table-wrap"><table><thead><tr><th>رتبه</th><th>نماد</th><th>قیمت</th><th>تغییر</th><th>حجم</th><th>ارزش معاملات</th><th>ورود/خروج پول</th><th>وضعیت</th><th>قابلیت معامله</th><th>امتیاز</th><th></th></tr></thead><tbody id="rows"></tbody></table></div>\n</section>\n<div class="analysis-title"><div><span class="mini-label">AI-STYLE INSIGHTS</span><h2>تحلیل نمادها</h2></div></div><div id="analysis" class="analysis-grid"></div>\n<footer><div>مالک و توسعه\u200cدهنده: <strong>حمیدرضا قاسمی</strong></div><div id="updated">آخرین بروزرسانی: —</div></footer>\n</div><script src="/static/app.js"></script>\n</body></html>\n'
JS = 'const $=s=>document.querySelector(s);let data=[],currentFilter="all";\nfunction symbols(){return [...new Set($("#symbols").value.split(/[\\n,\\s]+/).map(x=>x.trim()).filter(Boolean))]}\nfunction fmt(n){return n==null||Number.isNaN(Number(n))?"داده در دسترس نیست":new Intl.NumberFormat("fa-IR").format(n)}\nfunction money(n){if(n==null)return"داده در دسترس نیست";n=Number(n);if(n>=1e12)return(n/1e12).toLocaleString("fa-IR",{maximumFractionDigits:2})+" همت";if(n>=1e9)return(n/1e9).toLocaleString("fa-IR",{maximumFractionDigits:2})+" میلیارد";if(n>=1e6)return(n/1e6).toLocaleString("fa-IR",{maximumFractionDigits:2})+" میلیون";return fmt(n)}\nfunction renderTags(){$("#tags").innerHTML=symbols().map(s=>`<span class="tag">${s}<button data-remove="${s}">×</button></span>`).join("");document.querySelectorAll("[data-remove]").forEach(b=>b.onclick=()=>{$("#symbols").value=symbols().filter(x=>x!==b.dataset.remove).join("\\n");renderTags()})}\n$("#symbols").addEventListener("input",renderTags);\nfunction renderSummary(s){$("#summary").innerHTML=[["تعداد نمادها",`${s.count} نماد`],["بیشترین ارزش معاملات",money(s.top_trade_value)],["نماد برتر",s.top_symbol||"—"],["تعداد صف خرید",s.buy_queue],["تعداد صف فروش",s.sell_queue],["قابل معامله",s.tradable]].map(x=>`<div class="card"><div class="k">${x[0]}</div><div class="v">${x[1]}</div></div>`).join("")}\nfunction filtered(){return data.filter(r=>currentFilter==="all"?true:currentFilter==="positive"?Number(r.change_percent)>0:currentFilter==="negative"?Number(r.change_percent)<0:currentFilter==="money_in"?Number(r.money_flow)>0:currentFilter==="money_out"?Number(r.money_flow)<0:r.status===currentFilter)}\nfunction renderTable(){$("#rows").innerHTML=filtered().map(r=>{let ch=r.change_percent,flow=r.money_flow;return`<tr><td>${r.rank}</td><td><b>${r.symbol}</b></td><td>${fmt(r.last_price)}</td><td class="${ch>0?"pos":ch<0?"neg":""}">${ch==null?"داده در دسترس نیست":Number(ch).toFixed(2)+"%"}</td><td>${fmt(r.volume)}</td><td>${money(r.trade_value)}</td><td class="${flow>0?"pos":flow<0?"neg":""}">${flow==null?"داده در دسترس نیست":money(flow)}</td><td>${r.status_text||"داده در دسترس نیست"}</td><td>${r.status==="buy_queue"?"امکان خرید عادی وجود ندارد":r.status==="sell_queue"?"صف فروش":r.status==="normal"?"قابل معامله":"نامشخص"}</td><td>${r.analysis_score==null?"—":r.analysis_score+"/100"}</td><td><button data-star="${r.symbol}">⭐</button></td></tr>`}).join("");document.querySelectorAll("[data-star]").forEach(b=>b.onclick=async()=>{await fetch("/api/watchlist/"+encodeURIComponent(b.dataset.star),{method:"POST"});b.textContent="✓"})}\nfunction renderAnalysis(){$("#analysis").innerHTML=filtered().map(r=>`<article class="analysis-card"><h3>${r.symbol} — تحلیل هوشمند</h3><div class="score">${r.analysis_score==null?"امتیاز: داده کافی نیست":"امتیاز: "+r.analysis_score+" / 100"}</div><ul>${(r.analysis||["داده در دسترس نیست"]).map(x=>`<li>${x}</li>`).join("")}</ul><details><summary>جزئیات فرمول امتیاز</summary><p>فقط عوامل دارای داده استفاده می\u200cشوند و وزن\u200cها مجدداً نرمال می\u200cشوند.</p><ul>${(r.score_factors||[]).map(f=>`<li>${f.name}: وزن ${f.weight}% — ${f.value==null?"داده در دسترس نیست":f.value}</li>`).join("")}</ul></details></article>`).join("")}\nfunction err(x){$("#error").textContent=x;$("#error").classList.remove("hidden")}\nasync function run(url="/api/analyze"){if(!symbols().length)return err("حداقل یک نماد وارد کنید.");$("#loading").classList.remove("hidden");$("#error").classList.add("hidden");try{let res=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({symbols:symbols()})});let out=await res.json();if(!res.ok)throw Error(out.detail||"خطا");data=out.items||[];renderSummary(out.summary);renderTable();renderAnalysis();$("#updated").textContent="آخرین بروزرسانی: "+new Date().toLocaleTimeString("fa-IR")}catch(e){err(e.message)}finally{$("#loading").classList.add("hidden")}\n$("#analyze").onclick=()=>run();$("#refresh").onclick=()=>run("/api/refresh");$("#clear").onclick=()=>{$("#symbols").value="";data=[];renderTags();$("#summary").innerHTML="";$("#rows").innerHTML="";$("#analysis").innerHTML=""};document.querySelectorAll(".filter").forEach(b=>b.onclick=()=>{document.querySelectorAll(".filter").forEach(x=>x.classList.remove("active"));b.classList.add("active");currentFilter=b.dataset.filter;renderTable();renderAnalysis()});renderTags();\n'
CSS = ':root{font-family:Vazirmatn,system-ui,sans-serif;color-scheme:dark;--bg:#070b14;--panel:rgba(14,20,34,.78);--panel2:#10182a;--line:rgba(255,255,255,.08);--text:#f4f7ff;--muted:#8f9bb2;--green:#35e39a;--red:#ff637b;--yellow:#ffd166;--blue:#7c9cff;--cyan:#59d8ff}*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;min-height:100vh;color:var(--text);background:radial-gradient(900px 500px at 85% -10%,rgba(75,104,255,.18),transparent 60%),radial-gradient(700px 450px at 10% 30%,rgba(30,220,170,.08),transparent 65%),var(--bg)}body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);background-size:42px 42px;mask-image:linear-gradient(to bottom,#000,transparent 90%)}.ambient{position:fixed;width:260px;height:260px;border-radius:50%;filter:blur(80px);opacity:.12;pointer-events:none}.a1{background:#5577ff;top:15%;right:-80px}.a2{background:#00d9a0;bottom:5%;left:-90px}.shell{max-width:1500px;margin:auto;padding:28px 20px 36px;position:relative}.hero{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:22px;padding:10px 2px}.brand{display:flex;align-items:center;gap:16px}.brand-mark{width:58px;height:58px;border:1px solid var(--line);border-radius:18px;display:grid;place-items:center;font-size:27px;background:linear-gradient(145deg,rgba(124,156,255,.2),rgba(53,227,154,.08));box-shadow:0 12px 40px rgba(0,0,0,.28)}.eyebrow,.mini-label{font-size:10px;letter-spacing:2px;color:#72809b;font-weight:800}.hero h1{font-size:clamp(1.45rem,4vw,2.15rem);margin:5px 0 2px;letter-spacing:-.7px}.hero p{margin:0;color:var(--muted);font-size:.9rem}.hero-actions{display:flex;align-items:center;gap:10px}.live-pill{padding:8px 12px;border-radius:999px;background:rgba(53,227,154,.08);border:1px solid rgba(53,227,154,.18);color:#b8f7db;font-size:.78rem}.live-pill i{display:inline-block;width:7px;height:7px;background:var(--green);border-radius:50%;margin-left:6px;box-shadow:0 0 12px var(--green)}button{font:inherit;cursor:pointer;border-radius:12px;padding:10px 15px;border:1px solid var(--line);color:var(--text);background:rgba(255,255,255,.045);transition:.2s ease}.glass-btn:hover,.ghost:hover,.filter:hover{background:rgba(255,255,255,.08);transform:translateY(-1px)}.panel,.card,.analysis-card{background:linear-gradient(145deg,rgba(18,27,46,.84),rgba(10,15,27,.84));border:1px solid var(--line);border-radius:22px;backdrop-filter:blur(18px);box-shadow:0 20px 60px rgba(0,0,0,.2)}.panel{padding:20px;margin-bottom:18px}.section-head,.analysis-title{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:14px}.section-head h2,.analysis-title h2{font-size:1.1rem;margin:4px 0 0}.hint{font-size:.75rem;color:var(--muted)}textarea{width:100%;min-height:120px;border:1px solid var(--line);border-radius:16px;background:rgba(4,8,16,.7);color:var(--text);padding:15px;font:inherit;resize:vertical;outline:none;transition:.2s}textarea:focus{border-color:rgba(124,156,255,.55);box-shadow:0 0 0 4px rgba(124,156,255,.08)}.actions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}.primary{background:linear-gradient(135deg,#266cff,#674bff);border-color:rgba(130,160,255,.5);font-weight:800;box-shadow:0 10px 30px rgba(65,95,255,.2)}.primary span{margin-right:8px}.primary:hover{transform:translateY(-1px);filter:brightness(1.08)}.tags{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.tag{background:rgba(124,156,255,.08);border:1px solid rgba(124,156,255,.16);border-radius:999px;padding:7px 11px}.tag button{border:0;background:none;padding:0 0 0 5px;color:var(--muted)}.loading{margin-top:13px;color:var(--yellow);display:flex;align-items:center;gap:8px}.spinner{width:15px;height:15px;border:2px solid rgba(255,209,102,.25);border-top-color:var(--yellow);border-radius:50%;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}.hidden{display:none!important}.error{margin-top:12px;color:var(--red)}.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:18px}.card{margin:0;padding:16px;position:relative;overflow:hidden}.card:after{content:"";position:absolute;width:70px;height:70px;border-radius:50%;background:rgba(124,156,255,.08);left:-25px;bottom:-30px}.card .k{color:var(--muted);font-size:.78rem}.card .v{font-size:1.25rem;font-weight:900;margin-top:8px}.filters{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:14px}.filter{font-size:.78rem;padding:8px 12px}.filter.active{border-color:rgba(124,156,255,.6);background:rgba(124,156,255,.13);box-shadow:0 0 0 1px rgba(124,156,255,.18)}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:16px}table{width:100%;border-collapse:collapse;min-width:1050px;background:rgba(3,7,14,.2)}th,td{padding:13px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{color:#73809a;font-size:.72rem;font-weight:800;background:rgba(255,255,255,.025)}tbody tr{transition:.15s}tbody tr:hover{background:rgba(124,156,255,.045)}tbody tr:last-child td{border-bottom:0}.pos{color:var(--green)}.neg{color:var(--red)}.analysis-title{margin:28px 2px 14px}.analysis-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.analysis-card{padding:18px}.score{font-size:1.65rem;font-weight:900}footer{display:flex;justify-content:space-between;gap:12px;color:#68758c;padding:22px 3px 5px;font-size:.75rem}footer strong{color:#b9c4d8}@media(max-width:1000px){.cards{grid-template-columns:repeat(3,1fr)}.analysis-grid{grid-template-columns:1fr}@media(max-width:620px){.shell{padding:14px 10px 28px}.hero{align-items:flex-start;flex-direction:column;padding:4px 2px}.hero-actions{width:100%;justify-content:space-between}.brand-mark{width:48px;height:48px;border-radius:15px;font-size:22px}.hero h1{font-size:1.35rem}.hero p{font-size:.78rem}.panel{padding:14px;border-radius:18px}.cards{grid-template-columns:repeat(2,1fr);gap:9px}.card{padding:13px}.card .v{font-size:1.05rem}.section-head{align-items:flex-start;flex-direction:column}.hint{font-size:.7rem}.actions button{flex:1}.filters{overflow:auto;flex-wrap:nowrap;padding-bottom:3px}.filter{flex:0 0 auto}footer{flex-direction:column}\n'

PAGE = HTML.replace('<link rel="stylesheet" href="/static/styles.css">', f"<style>{CSS}</style>").replace("</body>", f"<script>{JS}</script></body>")

def get_provider():
    return TsetmcPublicProvider(settings.tsetmc_base_url, settings.request_timeout)

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="پنل شخصی تحلیل بورس ایران", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax")

def auth(c: HTTPBasicCredentials = Depends(security)):
    if c.username != settings.app_username or c.password != settings.app_password:
        raise HTTPException(401,"نام کاربری یا رمز عبور نادرست است.",headers={"WWW-Authenticate":"Basic"})
    return c.username

class SymbolsIn(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=100)

@app.get("/", response_class=HTMLResponse)
async def index(): return PAGE

@app.get("/health")
async def health(): return {"ok":True,"provider":settings.data_provider}

@app.get("/api/stocks/search")
async def search(q: str, _=Depends(auth)):
    p=get_provider()
    try: return {"items":await p.search(q.strip())}
    finally: await p.close()

async def run_analysis(symbols):
    clean=list(dict.fromkeys(s.strip() for s in symbols if s.strip()))
    p=get_provider()
    try: rows=await p.quotes(clean)
    except Exception as e: raise HTTPException(503,"⚠️ اتصال به منبع داده برقرار نشد.") from e
    finally: await p.close()
    items=[enrich_quote(x) for x in rows if x.get("found")]
    items.sort(key=lambda x:(x.get("trade_value") is not None,x.get("trade_value") or -1),reverse=True)
    for i,x in enumerate(items,1): x["rank"]=i
    summary={"count":len(items),"top_symbol":items[0]["symbol"] if items else None,
             "top_trade_value":items[0].get("trade_value") if items else None,
             "buy_queue":sum(x.get("status")=="buy_queue" for x in items),
             "sell_queue":sum(x.get("status")=="sell_queue" for x in items),
             "tradable":sum(x.get("status")=="normal" for x in items)}
    return {"items":items,"summary":summary}

@app.post("/api/analyze")
async def analyze(body: SymbolsIn,_=Depends(auth)):
    out=await run_analysis(body.symbols)
    c=conn(); c.execute("INSERT INTO history(created_at,payload) VALUES(?,?)",(datetime.now(timezone.utc).isoformat(),json.dumps(out,ensure_ascii=False))); c.commit(); c.close()
    return out

@app.post("/api/refresh")
async def refresh(body: SymbolsIn,_=Depends(auth)): return await analyze(body,_)

@app.get("/api/watchlist")
async def watchlist(_=Depends(auth)):
    c=conn(); items=[r["symbol"] for r in c.execute("SELECT symbol FROM watchlist ORDER BY symbol")]; c.close(); return {"items":items}

@app.post("/api/watchlist/{symbol}")
async def add(symbol:str,_=Depends(auth)):
    c=conn(); c.execute("INSERT OR IGNORE INTO watchlist(symbol) VALUES(?)",(symbol.strip(),)); c.commit(); c.close(); return {"ok":True}

@app.delete("/api/watchlist/{symbol}")
async def remove(symbol:str,_=Depends(auth)):
    c=conn(); c.execute("DELETE FROM watchlist WHERE symbol=?",(symbol.strip(),)); c.commit(); c.close(); return {"ok":True}

@app.get("/api/history")
async def history(_=Depends(auth)):
    c=conn(); items=[dict(r) for r in c.execute("SELECT id,created_at,payload FROM history ORDER BY id DESC LIMIT 30")]; c.close(); return {"items":items}
