"""Lightweight live dashboard web server (Python stdlib only, no extra deps).

A background thread runs the trading engine (live forward or accelerated replay) and
records the equity curve; an HTTP server serves a single self-contained HTML page that
polls /api/state once per second and renders profit, per-coin positions, leverage,
liquidations, an equity chart, a session timer, and the activity log. Bind is
localhost-only. Works with the multi-coin/leverage PortfolioEngine and the single-coin
SimEngine.
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Monitor:
    def __init__(self, engine, info: dict, max_points: int = 3000):
        self.engine = engine
        self.info = info
        self.equity_curve: list[list] = []
        self._max = max_points
        self.lock = threading.Lock()
        self.session_total: int = 0  # seconds; 0 = unlimited
        self.session_start: float | None = None

    def start_session(self):
        self.session_start = time.time()

    def record(self):
        with self.lock:
            self.equity_curve.append([
                datetime.now(timezone.utc).strftime("%H:%M:%S"),
                round(self.engine.equity(), 2),
            ])
            if len(self.equity_curve) > self._max:
                self.equity_curve = self.equity_curve[-self._max:]

    def _session(self):
        if self.session_total and self.session_start:
            elapsed = time.time() - self.session_start
            remaining = max(0.0, self.session_total - elapsed)
            return {"total": self.session_total, "elapsed": round(elapsed), "remaining": round(remaining), "active": remaining > 0}
        return None

    def snapshot(self) -> dict:
        e = self.engine
        # engine.lock (held by the loop thread during a mutation) makes this a consistent,
        # crash-free read; self.lock guards the equity_curve. Order is always
        # engine-then-monitor, the only place both are taken, so no deadlock is possible.
        with e.lock, self.lock:
            trades = list(e.trades)
            wins = sum(1 for t in trades if t.pnl > 0)
            losses = sum(1 for t in trades if t.pnl <= 0)
            fees = sum(t.fees for t in trades)
            recent_trades = [
                {"entry": round(t.entry_price, 2), "exit": round(t.exit_price, 2),
                 "pnl": round(t.pnl, 2), "reason": t.exit_reason}
                for t in trades[-12:]
            ]
            base = {
                "strategy": self.info.get("strategy"),
                "timeframe": self.info.get("timeframe"),
                "mode": self.info.get("mode"),
                "status": self.info.get("status", ""),
                "initial_capital": e.cfg.initial_capital,
                "equity": round(e.equity(), 2),
                "profit": round(e.equity() - e.cfg.initial_capital, 2),
                "total_return_pct": round(e.total_return() * 100, 2),
                "cash": round(e.cash, 2),
                "realized_pnl": round(e.realized_pnl, 2),
                "unrealized": round(e.unrealized(), 2),
                "trades": len(trades),
                "wins": wins,
                "losses": losses,
                "fees_total": round(fees, 4),
                "events": list(e.events)[-18:],
                "recent_trades": recent_trades,
                "ledger": list(getattr(e, "ledger", []))[-10:],  # balance at each open/close
                "equity_curve": list(self.equity_curve),
                "session": self._session(),
            }
            if hasattr(e, "symbols"):  # PortfolioEngine (multi-coin / leverage)
                coins = []
                for sym in e.symbols:
                    p = e.positions.get(sym)
                    mark = e.marks.get(sym, (p.entry_price if p else 0.0))
                    sig = e.last_signals.get(sym)
                    pend = e.pending.get(sym)
                    pend_disp = (pend[2] if (pend and pend[0] == "open") else ("close" if pend else None))
                    coins.append({
                        "symbol": sym,
                        "state": p.side if p else "FLAT",
                        "entry": round(p.entry_price, 2) if p else None,
                        "qty": p.qty if p else None,
                        "mark": round(mark, 2) if mark else None,
                        "stop": round(p.stop_price, 2) if p else None,
                        "liq": round(p.liq, 2) if (p and e.leverage > 1) else None,
                        "unrealized": round(p.unrealized(mark), 2) if p else 0.0,
                        "roe": round(p.roe(mark) * 100, 1) if p else None,
                        "pending": pend_disp,
                        "signal": (sig.action.value if sig else None),
                        "reason": (sig.reason if sig else None),
                    })
                base.update({
                    "symbol": f"{len(e.symbols)} coins",
                    "multi": True,
                    "leverage": e.leverage,
                    "liquidations": getattr(e, "liquidations", 0),
                    "funding_total": round(getattr(e, "funding_total", 0.0), 4),
                    "open_positions": len(e.positions),
                    "max_positions": e.risk.s.max_open_positions,
                    "coins": coins,
                })
            else:  # SimEngine (single coin)
                pos = e.position
                base.update({
                    "symbol": e.symbol,
                    "multi": False,
                    "leverage": 1,
                    "liquidations": 0,
                    "mark_price": round(e.mark_price, 2),
                    "coins": [{
                        "symbol": e.symbol,
                        "state": pos.state,
                        "entry": pos.entry_price,
                        "qty": pos.quantity,
                        "mark": round(e.mark_price, 2),
                        "stop": pos.stop_price,
                        "liq": None,
                        "unrealized": round(e.unrealized(), 2),
                        "pending": (e.pending[0] if e.pending else None),
                        "signal": (e.last_signal.action.value if e.last_signal else None),
                        "reason": (e.last_signal.reason if e.last_signal else None),
                    }],
                })
            return base


def _make_handler(monitor: Monitor):
    page = PAGE.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                self._send(json.dumps(monitor.snapshot()).encode("utf-8"), "application/json")
            elif self.path == "/" or self.path.startswith("/index"):
                self._send(page, "text/html; charset=utf-8")
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


def serve(monitor: Monitor, run_loop, host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True, logger=None):
    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    httpd = None
    for p in range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer((host, p), _make_handler(monitor))
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError(f"could not bind a port in {port}..{port+9}")

    url = f"http://{host}:{port}"
    print(f"Dashboard live at {url}  (Ctrl+C to stop)")
    if logger:
        logger.info(f"Dashboard live at {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TradeBot · Live Profit</title>
<style>
  :root{--bg:#0b0e14;--panel:#141a23;--panel2:#1b2330;--line:#26303f;--txt:#e6edf3;
    --muted:#8b97a7;--green:#2ecc71;--red:#ff5d5d;--blue:#4aa3ff;--amber:#f5b53d;--purple:#b07cff;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font-family:'Segoe UI',system-ui,-apple-system,Roboto,sans-serif}
  .wrap{max-width:1150px;margin:0 auto;padding:18px}
  header{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
  header h1{font-size:18px;margin:0;font-weight:600;letter-spacing:.3px}
  .pill{background:var(--panel2);color:var(--muted);border:1px solid var(--line);padding:3px 10px;border-radius:20px;font-size:12px}
  .pill.lev{background:rgba(176,124,255,.16);color:var(--purple);border-color:rgba(176,124,255,.4);font-weight:600}
  .pill.liq{background:rgba(255,93,93,.16);color:var(--red);border-color:rgba(255,93,93,.4);font-weight:600}
  .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px;background:#555}
  .dot.ok{background:var(--green);box-shadow:0 0 8px var(--green)} .dot.bad{background:var(--red)}
  .hero{background:linear-gradient(135deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:16px;padding:22px 24px;margin-bottom:14px}
  .hero .label{color:var(--muted);font-size:13px;text-transform:uppercase;letter-spacing:1px}
  .hero .equity{font-size:46px;font-weight:700;margin:6px 0 2px}
  .hero .profit{font-size:20px;font-weight:600}
  .warn{color:var(--amber);font-size:12.5px;margin-top:8px}
  .session{margin-top:14px}
  .session .sesshead{display:flex;justify-content:space-between;font-size:13px;color:var(--muted);margin-bottom:5px}
  .session .sesstime{color:var(--txt);font-weight:600}
  .sessbar{height:8px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .sessfill{height:100%;width:0%;background:linear-gradient(90deg,var(--blue),var(--green));transition:width .5s linear}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .card .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.6px}
  .card .v{font-size:22px;font-weight:600;margin-top:4px}
  .sub{color:var(--muted);font-size:12px;margin-top:2px}
  .green{color:var(--green)} .red{color:var(--red)} .amber{color:var(--amber)} .blue{color:var(--blue)}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:14px}
  .panel h3{margin:0 0 10px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px}
  .row{display:grid;grid-template-columns:2fr 1fr;gap:14px}
  @media(max-width:820px){.row{grid-template-columns:1fr}}
  canvas{width:100%;height:230px;display:block}
  .log{font-family:Consolas,monospace;font-size:12px;line-height:1.55;color:#b7c2d0;max-height:230px;overflow:auto;white-space:pre-wrap}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  td,th{padding:6px 6px;text-align:right;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:500} td:first-child,th:first-child{text-align:left}
  .badge{padding:2px 8px;border-radius:6px;font-size:11.5px;font-weight:600}
  .badge.long{background:rgba(46,204,113,.15);color:var(--green)}
  .badge.short{background:rgba(255,93,93,.15);color:var(--red)}
  .badge.flat{background:rgba(245,181,61,.15);color:var(--amber)}
  footer{color:var(--muted);font-size:11px;text-align:center;margin-top:8px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>TradeBot</h1>
    <span class="pill"><span id="conn" class="dot"></span><span id="conntxt">connecting…</span></span>
    <span class="pill" id="sym">—</span>
    <span class="pill" id="strat">—</span>
    <span class="pill" id="tf">—</span>
    <span class="pill lev" id="lev" style="display:none">—</span>
    <span class="pill liq" id="liq" style="display:none">—</span>
  </header>

  <div class="hero">
    <div class="label">Total equity (fake balance)</div>
    <div class="equity" id="equity">—</div>
    <div class="profit" id="profit">—</div>
    <div class="sub" id="status">—</div>
    <div class="warn" id="levwarn" style="display:none">⚠ Leverage amplifies losses — a position is liquidated (margin lost) if price falls to its liq level.</div>
    <div class="session" id="session" style="display:none">
      <div class="sesshead"><span id="sesslabel">Trading session</span><span class="sesstime" id="sesstime">--:--</span></div>
      <div class="sessbar"><div class="sessfill" id="sessfill"></div></div>
    </div>
  </div>

  <div class="grid">
    <div class="card"><div class="k">Cash (free)</div><div class="v" id="cash">—</div></div>
    <div class="card"><div class="k">Realized PnL</div><div class="v" id="realized">—</div></div>
    <div class="card"><div class="k">Unrealized</div><div class="v" id="unreal">—</div></div>
    <div class="card"><div class="k">Open positions</div><div class="v" id="openpos">—</div></div>
    <div class="card"><div class="k">Trades (W/L)</div><div class="v" id="trades">—</div><div class="sub" id="wl"></div></div>
    <div class="card"><div class="k">Fees paid</div><div class="v" id="fees">—</div></div>
    <div class="card"><div class="k">Funding paid</div><div class="v" id="funding">—</div></div>
  </div>

  <div class="panel">
    <h3>Positions</h3>
    <table id="coins"><thead><tr>
      <th>Coin</th><th>Position</th><th>Entry</th><th>Mark</th><th>Unrealized</th><th>Stop / Liq</th><th>Signal</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class="row">
    <div class="panel"><h3>Equity curve</h3><canvas id="chart"></canvas></div>
    <div class="panel"><h3>Recent trades</h3>
      <table id="trtbl"><thead><tr><th>Entry</th><th>Exit</th><th>PnL</th><th>Why</th></tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class="panel"><h3>Balance history (open/close)</h3>
    <table id="ledtbl"><thead><tr><th>Time (UTC)</th><th>Event</th><th>Symbol</th><th>Side</th><th>PnL</th><th>Balance</th></tr></thead><tbody></tbody></table>
  </div>

  <div class="panel"><h3>Activity log</h3><div class="log" id="logbox">—</div></div>
  <footer>Auto-refresh 1s · paper trading, fake balance · not financial advice</footer>
</div>

<script>
function fmt(x,d){d=(d==null)?2:d; if(x==null||isNaN(x))return '—'; return Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});}
function sign(x){return (x>=0?'+':'')+fmt(x);}
function cls(x){return x>0?'green':(x<0?'red':'');}

async function tick(){
  try{
    const s=await (await fetch('/api/state',{cache:'no-store'})).json();
    document.getElementById('conn').className='dot ok';
    document.getElementById('conntxt').textContent='live';
    render(s);
  }catch(e){
    document.getElementById('conn').className='dot bad';
    document.getElementById('conntxt').textContent='offline';
  }
}

function render(s){
  document.getElementById('sym').textContent=s.symbol;
  document.getElementById('strat').textContent='strategy: '+s.strategy;
  document.getElementById('tf').textContent='tf: '+s.timeframe;
  const lev=document.getElementById('lev'), lw=document.getElementById('levwarn');
  if(s.leverage&&s.leverage>1){lev.style.display='';lev.textContent='leverage '+s.leverage+'x';lw.style.display='';}
  else{lev.style.display='none';lw.style.display='none';}
  const liq=document.getElementById('liq');
  if(s.liquidations&&s.liquidations>0){liq.style.display='';liq.textContent='liquidations: '+s.liquidations;}
  else{liq.style.display='none';}

  document.getElementById('equity').textContent=fmt(s.equity)+' USDT';
  const prof=document.getElementById('profit');
  prof.textContent=sign(s.profit)+' USDT  ('+sign(s.total_return_pct)+'%)';
  prof.className='profit '+cls(s.profit);
  document.getElementById('status').textContent=s.status||'';

  document.getElementById('cash').textContent=fmt(s.cash);
  const rp=document.getElementById('realized'); rp.textContent=sign(s.realized_pnl); rp.className='v '+cls(s.realized_pnl);
  const ur=document.getElementById('unreal'); ur.textContent=sign(s.unrealized); ur.className='v '+cls(s.unrealized);
  document.getElementById('openpos').textContent=(s.open_positions!=null? s.open_positions : (s.coins||[]).filter(c=>c.state==='LONG').length)+(s.max_positions?(' / '+s.max_positions):'');
  document.getElementById('trades').textContent=s.trades;
  document.getElementById('wl').textContent=s.wins+' W / '+s.losses+' L';
  document.getElementById('fees').textContent=fmt(s.fees_total,4);
  document.getElementById('funding').textContent=fmt(s.funding_total!=null?s.funding_total:0,4);

  // positions table
  const cb=document.querySelector('#coins tbody'); cb.innerHTML='';
  (s.coins||[]).forEach(c=>{
    const tr=document.createElement('tr');
    const badge=c.state==='LONG'?'<span class="badge long">LONG</span>':(c.state==='SHORT'?'<span class="badge short">SHORT</span>':'<span class="badge flat">FLAT</span>');
    const pend=c.pending?(' <span style="color:#b07cff">·'+c.pending+'</span>'):'';
    const open=(c.state!=='FLAT');
    const stopliq=open?(fmt(c.stop)+(c.liq?(' / <span class="red">'+fmt(c.liq)+'</span>'):'')):'—';
    const roe=(c.roe!=null)?(' <span style="color:#8b97a7">('+sign(c.roe)+'% ROE)</span>'):'';
    const unr=open?('<span class="'+cls(c.unrealized)+'">'+sign(c.unrealized)+'</span>'+roe):'—';
    const sig=c.signal?(c.signal+(c.reason?(' <span style="color:#8b97a7">· '+c.reason+'</span>'):'')):'—';
    tr.innerHTML='<td><b>'+c.symbol+'</b></td><td>'+badge+pend+'</td><td>'+(c.entry?fmt(c.entry):'—')+'</td><td>'+(c.mark?fmt(c.mark):'—')+'</td><td>'+unr+'</td><td>'+stopliq+'</td><td style="text-align:left">'+sig+'</td>';
    cb.appendChild(tr);
  });

  // recent trades
  const tb=document.querySelector('#trtbl tbody'); tb.innerHTML='';
  (s.recent_trades||[]).slice().reverse().forEach(t=>{
    const tr=document.createElement('tr');
    tr.innerHTML='<td>'+fmt(t.entry)+'</td><td>'+fmt(t.exit)+'</td><td class="'+cls(t.pnl)+'">'+sign(t.pnl)+'</td><td style="color:#8b97a7;text-align:left">'+t.reason+'</td>';
    tb.appendChild(tr);
  });

  // balance history (equity snapshot at each open/close)
  const lb=document.querySelector('#ledtbl tbody'); lb.innerHTML='';
  (s.ledger||[]).slice().reverse().forEach(r=>{
    const tr=document.createElement('tr');
    const t=(r.ts||'').replace('T',' ').slice(0,19);
    const ev=r.event==='OPEN'?'<span class="badge long">OPEN</span>':'<span class="badge short">CLOSE</span>';
    const pnl=(r.pnl!=null)?('<span class="'+cls(r.pnl)+'">'+sign(r.pnl)+'</span>'):'—';
    tr.innerHTML='<td>'+t+'</td><td>'+ev+'</td><td><b>'+r.symbol+'</b></td><td>'+(r.side||'')+'</td><td>'+pnl+'</td><td><b>'+fmt(r.equity)+'</b></td>';
    lb.appendChild(tr);
  });

  document.getElementById('logbox').textContent=(s.events||[]).slice().reverse().join('\n')||'—';
  drawChart(s.equity_curve||[], s.initial_capital);

  if(s.session){sess=s.session;sessSyncMs=Date.now();updateSession();}
  else{document.getElementById('session').style.display='none';}
}

let sess=null, sessSyncMs=0;
function fmtTime(sec){sec=Math.max(0,Math.round(sec));const m=Math.floor(sec/60),x=sec%60;return m+':'+String(x).padStart(2,'0');}
function updateSession(){
  if(!sess)return;
  document.getElementById('session').style.display='block';
  let rem=sess.active?(sess.remaining-(Date.now()-sessSyncMs)/1000):0;
  if(rem<0)rem=0;
  const done=!sess.active||rem<=0;
  document.getElementById('sesstime').textContent=done?'complete':(fmtTime(rem)+' remaining');
  document.getElementById('sesslabel').textContent=done?'Session finished':'Trading session';
  const tot=sess.total||1;
  document.getElementById('sessfill').style.width=(100*Math.min(1,(tot-rem)/tot))+'%';
}
setInterval(updateSession,1000);

function drawChart(curve, base){
  const c=document.getElementById('chart');
  const dpr=window.devicePixelRatio||1, w=c.clientWidth, h=c.clientHeight;
  c.width=w*dpr; c.height=h*dpr; const ctx=c.getContext('2d'); ctx.scale(dpr,dpr); ctx.clearRect(0,0,w,h);
  if(curve.length<2){ctx.fillStyle='#8b97a7';ctx.font='13px Segoe UI';ctx.fillText('waiting for data…',12,24);return;}
  const vals=curve.map(p=>p[1]);
  let mn=Math.min(...vals,base), mx=Math.max(...vals,base); if(mn===mx){mn-=1;mx+=1;}
  const pad=(mx-mn)*0.08; mn-=pad; mx+=pad;
  const X=i=>(i/(curve.length-1))*(w-10)+5, Y=v=>h-8-((v-mn)/(mx-mn))*(h-16);
  ctx.strokeStyle='#3a4658'; ctx.setLineDash([4,4]); ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(0,Y(base)); ctx.lineTo(w,Y(base)); ctx.stroke(); ctx.setLineDash([]);
  const up=vals[vals.length-1]>=base, col=up?'#2ecc71':'#ff5d5d';
  const grad=ctx.createLinearGradient(0,0,0,h);
  grad.addColorStop(0,up?'rgba(46,204,113,.25)':'rgba(255,93,93,.25)'); grad.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath(); ctx.moveTo(X(0),Y(vals[0]));
  for(let i=1;i<vals.length;i++) ctx.lineTo(X(i),Y(vals[i]));
  ctx.strokeStyle=col; ctx.lineWidth=2; ctx.stroke();
  ctx.lineTo(X(vals.length-1),h); ctx.lineTo(X(0),h); ctx.closePath(); ctx.fillStyle=grad; ctx.fill();
}

tick(); setInterval(tick,1000);
</script>
</body>
</html>"""
