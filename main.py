from pathlib import Path
import json, os, sqlite3
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from starlette.middleware.sessions import SessionMiddleware
from .providers.tsetmc_public import TsetmcPublicProvider
from .providers.official_tsetmc import OfficialTsetmcProvider
from .services.analysis import enrich_quote

class Settings(BaseSettings):
    data_provider:str="tsetmc_public"
    tsetmc_base_url:str="https://cdn.tsetmc.com"
    app_username:str="admin"
    app_password:str="change-me"
    session_secret:str="change-me"
    request_timeout:float=12
settings=Settings()
DB=Path("data/app.db"); DB.parent.mkdir(exist_ok=True)
security=HTTPBasic()

def conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init_db():
    c=conn()
    c.execute("CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT,payload TEXT)")
    c.commit(); c.close()
def get_provider():
    if settings.data_provider=="official_tsetmc": return OfficialTsetmcProvider()
    return TsetmcPublicProvider(settings.tsetmc_base_url,settings.request_timeout)

@asynccontextmanager
async def lifespan(app):
    init_db(); yield

app=FastAPI(title="پنل شخصی تحلیل بورس ایران",lifespan=lifespan)
app.add_middleware(SessionMiddleware,secret_key=settings.session_secret,same_site="lax")
app.mount("/static",StaticFiles(directory="app/static"),name="static")

def auth(c:HTTPBasicCredentials=Depends(security)):
    if c.username!=settings.app_username or c.password!=settings.app_password:
        raise HTTPException(401,"نام کاربری یا رمز عبور نادرست است.",headers={"WWW-Authenticate":"Basic"})
    return c.username

class SymbolsIn(BaseModel):
    symbols:list[str]=Field(min_length=1,max_length=100)

@app.get("/")
async def index(): return FileResponse("app/static/index.html")
@app.get("/health")
async def health(): return {"ok":True,"provider":settings.data_provider}

@app.get("/api/stocks/search")
async def search(q:str,_=Depends(auth)):
    p=get_provider()
    try:return {"items":await p.search(q.strip())}
    finally:await p.close()

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
async def analyze(body:SymbolsIn,_=Depends(auth)):
    out=await run_analysis(body.symbols)
    c=conn(); c.execute("INSERT INTO history(created_at,payload) VALUES(?,?)",(datetime.now(timezone.utc).isoformat(),json.dumps(out,ensure_ascii=False))); c.commit(); c.close()
    return out

@app.post("/api/refresh")
async def refresh(body:SymbolsIn,_=Depends(auth)): return await analyze(body,_)

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
