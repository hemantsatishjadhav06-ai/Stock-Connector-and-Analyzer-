"""Server-rendered pages for the website.

Plain HTML with inline CSS and no build step, using the same theme tokens as
the report so the site and the analysis look like one product. The only
JavaScript is a few lines that poll a job's status while an analysis runs.
"""

from __future__ import annotations

import html
import json
from typing import Any, Dict, List, Optional

from ..report.charts import esc, fmt

ZONE_LABEL = {
    "buy": "Buy zone", "near": "Near buy line",
    "expensive": "No margin of safety", "unknown": "Undetermined",
}
ZONE_STATUS = {
    "buy": "good", "near": "warning", "expensive": "critical", "unknown": "muted",
}
FLAG_STATUS = {"pass": "good", "watch": "warning", "fail": "critical", "unknown": "muted"}


def shell(title: str, body: str, active: str = "", extra_head: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{SITE_CSS}</style>
{extra_head}
</head>
<body>
<header class="topbar">
  <a class="brand" href="/">Stock&nbsp;Analyzer</a>
  <nav>
    <a href="/" class="{'on' if active == 'home' else ''}">Search</a>
    <a href="/companies" class="{'on' if active == 'companies' else ''}">Companies</a>
    <a href="/status" class="{'on' if active == 'status' else ''}">Status</a>
  </nav>
</header>
<main class="wrap">{body}</main>
<footer class="sitefoot">
  <p><strong>Research and education for individual investors — not personalised
  investment advice.</strong> Figures are scraped from public sources and may be
  incomplete; every report carries a verification score saying how much of it
  could be sourced.</p>
</footer>
</body></html>"""


def searchbox(value: str = "", market: str = "") -> str:
    options = "".join(
        f'<option value="{esc(v)}"{" selected" if market == v else ""}>{esc(lbl)}</option>'
        for v, lbl in [
            ("", "Any market"), ("US", "United States"), ("India", "India"),
            ("UK", "United Kingdom"), ("Europe", "Europe"), ("JP", "Japan"),
        ]
    )
    return f"""
<form class="searchform" action="/search" method="get" autocomplete="off">
  <input type="search" name="q" value="{esc(value)}" placeholder="Type a company name — Apple, Reliance Industries, Infosys…" aria-label="Company name" required>
  <select name="market" aria-label="Market">{options}</select>
  <button type="submit">Analyse</button>
</form>"""


def home(recent: List[Dict[str, Any]], directory_size: int) -> str:
    body = f"""
<section class="hero">
  <h1>Analyse any listed company</h1>
  <p class="lead">Type a company name. The engine finds it, scrapes its filings
  and price history, stores everything in SQL, and returns a full research
  report — valuation on four models, a forensic screen, technicals, trade
  signals and a verification score.</p>
  {searchbox()}
  <p class="hint">{directory_size:,} US registrants indexed for instant name
  search, plus live lookup for India and other markets.</p>
</section>
{_recent_block(recent)}
<section class="how">
  <h2>How it works</h2>
  <ol class="steps">
    <li><strong>Find</strong> — your text is matched against the SEC registrant
      directory, Screener.in and Yahoo. Ambiguous names are disambiguated rather
      than guessed.</li>
    <li><strong>Scrape</strong> — filings from SEC XBRL or Screener.in, prices
      and news from market-data providers, with automatic failover.</li>
    <li><strong>Store</strong> — everything normalises into SQLite, so the second
      visit is instant and the analysis is reproducible.</li>
    <li><strong>Analyse</strong> — fundamentals, forensics, technicals, four
      valuation models, a margin-of-safety rule and a confidence score.</li>
    <li><strong>Refresh</strong> — prices go stale in hours and statements in
      weeks, so each is re-pulled on its own schedule.</li>
  </ol>
</section>"""
    return shell("Stock Analyzer — analyse any listed company", body, "home")


def _recent_block(recent: List[Dict[str, Any]]) -> str:
    if not recent:
        return ""
    cards = "".join(company_card(c) for c in recent[:6])
    return f"""
<section>
  <h2>Recently analysed</h2>
  <div class="cards">{cards}</div>
</section>"""


def company_card(row: Dict[str, Any]) -> str:
    zone = row.get("valuation_zone") or "unknown"
    score = row.get("verification_score")
    flag = row.get("forensic_flag") or "unknown"
    currency = row.get("currency") or ""
    return f"""
<a class="card" href="/company/{esc(row['ticker'])}">
  <p class="cardname">{esc(row.get('name') or row['ticker'])}</p>
  <p class="cardticker">{esc(row['ticker'])} · {esc(row.get('market') or '')}</p>
  <p class="cardzone status-{ZONE_STATUS.get(zone, 'muted')}">{esc(ZONE_LABEL.get(zone, zone))}</p>
  <dl class="cardstats">
    <div><dt>Price</dt><dd>{fmt(row.get('price'), 2)} {esc(currency)}</dd></div>
    <div><dt>Intrinsic</dt><dd>{fmt(row.get('intrinsic_value'), 2)}</dd></div>
    <div><dt>Verification</dt><dd>{fmt(score, 0, '%')}</dd></div>
    <div><dt>Forensic</dt><dd class="status-{FLAG_STATUS.get(flag, 'muted')}">{esc(flag)}</dd></div>
  </dl>
  <p class="cardage">{_age_text(row.get('last_refreshed'))}</p>
</a>"""


def _age_text(stamp: Optional[str]) -> str:
    from ..warehouse import _age_hours

    hours = _age_hours(stamp)
    if hours is None:
        return "never analysed"
    if hours < 1:
        return f"updated {int(hours * 60)} min ago"
    if hours < 48:
        return f"updated {int(hours)} h ago"
    return f"updated {int(hours / 24)} d ago"


def results(query: str, market: str, candidates: List[Any], notes: List[str]) -> str:
    if not candidates:
        rows = f"""<p class="nodata">No company matched “{esc(query)}”.</p>"""
    else:
        rows = "".join(
            f"""
<li class="result">
  <a href="/company/{esc(c.ticker)}?market={esc(c.market)}">
    <span class="rname">{esc(c.name)}</span>
    <span class="rmeta">{esc(c.ticker)} · {esc(c.market or '—')}{(' · ' + esc(c.exchange)) if c.exchange else ''}</span>
  </a>
  <span class="rscore" title="match confidence from {esc(c.source)}">{c.score * 100:.0f}%</span>
</li>"""
            for c in candidates
        )
        rows = f'<ul class="results">{rows}</ul>'
    note_html = (
        '<ul class="notes">' + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul>"
        if notes else ""
    )
    body = f"""
<section>
  <h1>Matches for “{esc(query)}”</h1>
  {searchbox(query, market)}
  <p class="hint">More than one company can share a name. Pick the one you mean —
  the engine will not guess between close matches.</p>
  {rows}
  {note_html}
</section>"""
    return shell(f"Search — {query}", body, "home")


def analysing(ticker: str, name: str, job: Any) -> str:
    """Shown while a background analysis runs; polls until it finishes."""
    # A meta-refresh is the no-JavaScript fallback: the poller below is nicer,
    # but the page must still make progress with scripting disabled.
    head = '<meta http-equiv="refresh" content="8">'
    state = esc(getattr(job, "message", "queued"))
    script = """
<script>
(function(){
  var t = %s;
  function poll(){
    fetch('/api/job/' + encodeURIComponent(t))
      .then(function(r){ return r.json(); })
      .then(function(j){
        var el = document.getElementById('state');
        if (el && j.message) el.textContent = j.message;
        if (j.state === 'done' || j.state === 'error') { location.reload(); }
        else { setTimeout(poll, 2500); }
      })
      .catch(function(){ setTimeout(poll, 4000); });
  }
  setTimeout(poll, 2500);
})();
</script>""" % json.dumps(ticker)

    body = f"""
<section class="working">
  <h1>Analysing {esc(name or ticker)}</h1>
  <p class="lead">Scraping filings and price history, then running the full
  pipeline. This takes up to a couple of minutes the first time; afterwards the
  report is cached and loads instantly.</p>
  <div class="progress" role="status" aria-live="polite"><div class="bar"></div></div>
  <p class="jobstate" id="state">{state}</p>
  <p class="hint">This page refreshes itself. Leaving it will not cancel the run.</p>
</section>{script}"""
    return shell(f"Analysing {name or ticker}", body, extra_head=head)


def failed(ticker: str, message: str, gaps: List[str]) -> str:
    gap_html = (
        '<ul class="notes">' + "".join(f"<li>{esc(g)}</li>" for g in gaps) + "</ul>"
        if gaps else ""
    )
    body = f"""
<section>
  <h1>Could not analyse {esc(ticker)}</h1>
  <div class="callout critical"><p>{esc(message)}</p></div>
  {gap_html}
  <p><a class="btn" href="/company/{esc(ticker)}?force=1">Try again</a>
     <a class="btn ghost" href="/">Search for another company</a></p>
  <p class="hint">Public data sources rate-limit shared IP addresses. If this
  keeps happening, configure a data-provider key (see the project README) —
  the engine will not invent figures to fill the gap.</p>
</section>"""
    return shell(f"Failed — {ticker}", body)


def companies(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        inner = '<p class="nodata">No companies analysed yet.</p>'
    else:
        cells = "".join(
            f"""<tr>
  <td><a href="/company/{esc(r['ticker'])}"><strong>{esc(r.get('name') or r['ticker'])}</strong></a>
      <div class="muted">{esc(r['ticker'])} · {esc(r.get('market') or '')}</div></td>
  <td class="num">{fmt(r.get('price'), 2)}</td>
  <td class="num">{fmt(r.get('intrinsic_value'), 2)}</td>
  <td class="num">{fmt(r.get('buy_below'), 2)}</td>
  <td><span class="status-{ZONE_STATUS.get(r.get('valuation_zone') or 'unknown', 'muted')}">{esc(ZONE_LABEL.get(r.get('valuation_zone') or 'unknown', '—'))}</span></td>
  <td><span class="status-{FLAG_STATUS.get(r.get('forensic_flag') or 'unknown', 'muted')}">{esc(r.get('forensic_flag') or '—')}</span></td>
  <td class="num">{fmt(r.get('verification_score'), 0, '%')}</td>
  <td class="muted">{_age_text(r.get('last_refreshed'))}</td>
</tr>"""
            for r in rows
        )
        inner = f"""<div class="scroll"><table>
<thead><tr><th>Company</th><th class="num">Price</th><th class="num">Intrinsic</th>
<th class="num">Buy below</th><th>Verdict</th><th>Forensic</th><th class="num">Verif.</th><th>Updated</th></tr></thead>
<tbody>{cells}</tbody></table></div>"""
    body = f"""
<section>
  <h1>Companies <span class="muted">({len(rows)})</span></h1>
  {searchbox()}
  {inner}
</section>"""
    return shell("Companies", body, "companies")


def status(info: Dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{esc(k)}</td><td class='num'>{esc(v)}</td></tr>"
        for k, v in info.get("stats", {}).items()
    )
    jobs = "".join(
        f"<tr><td>{esc(j.ticker)}</td><td>{esc(j.state)}</td>"
        f"<td class='muted'>{esc(j.message)}</td><td class='muted'>{esc(j.queued_at)}</td></tr>"
        for j in info.get("jobs", [])
    )
    job_block = (
        f"<h2>Running now</h2><div class='scroll'><table><thead><tr><th>Ticker</th>"
        f"<th>State</th><th>Message</th><th>Queued</th></tr></thead><tbody>{jobs}</tbody></table></div>"
        if jobs else "<p class='hint'>No analyses running.</p>"
    )
    log = "".join(
        f"<tr><td>{esc(r['ticker'])}</td>"
        f"<td><span class='status-{ 'good' if r['status']=='ok' else 'warning' if r['status']=='partial' else 'critical' }'>{esc(r['status'])}</span></td>"
        f"<td class='num'>{fmt(r.get('verification'), 0, '%')}</td>"
        f"<td class='muted'>{esc((r.get('finished_at') or '')[:19])}</td>"
        f"<td class='muted'>{esc(r.get('trigger') or '')}</td></tr>"
        for r in info.get("recent_refreshes", [])
    )
    log_block = (
        f"<h2>Recent refreshes</h2><div class='scroll'><table><thead><tr><th>Ticker</th>"
        f"<th>Status</th><th class='num'>Verification</th><th>Finished</th><th>Trigger</th>"
        f"</tr></thead><tbody>{log}</tbody></table></div>"
        if log else ""
    )
    body = f"""
<section>
  <h1>Status</h1>
  <div class="scroll"><table><tbody>{rows}</tbody></table></div>
  {job_block}
  {log_block}
</section>"""
    return shell("Status", body, "status")


SITE_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  color-scheme:light;
  --surface-1:#fcfcfb; --plane:#f9f9f7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a;
  --status-good:#0ca30c; --status-warning:#fab219; --status-critical:#d03b3b;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --surface-1:#1a1a19; --plane:#0d0d0d;
    --text-primary:#fff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --border:rgba(255,255,255,0.10);
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
  }
}
html,body{margin:0;padding:0}
body{background:var(--plane);color:var(--text-primary);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--series-1);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:26px 20px 60px}
h1{font-size:1.85rem;margin:.2em 0 .35em;letter-spacing:-.01em;font-weight:650}
h2{font-size:1.2rem;margin:1.9em 0 .6em;font-weight:620}
.topbar{display:flex;align-items:center;gap:22px;padding:12px 20px;
  border-bottom:1px solid var(--border);background:var(--surface-1);
  position:sticky;top:0;z-index:5}
.brand{font-weight:680;letter-spacing:-.01em;color:var(--text-primary)}
.topbar nav{display:flex;gap:16px;font-size:.9rem}
.topbar nav a{color:var(--text-secondary)}
.topbar nav a.on{color:var(--text-primary);font-weight:620}
.hero{padding:14px 0 4px}
.lead{font-size:1.05rem;color:var(--text-secondary);max-width:64ch}
.hint{font-size:.82rem;color:var(--muted)}
.searchform{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 8px}
.searchform input[type=search]{flex:1;min-width:260px;padding:12px 14px;font-size:1rem;
  border:1px solid var(--border);border-radius:10px;background:var(--surface-1);
  color:var(--text-primary)}
.searchform select{padding:12px 10px;border:1px solid var(--border);border-radius:10px;
  background:var(--surface-1);color:var(--text-primary)}
.searchform button{padding:12px 22px;font-size:1rem;font-weight:620;border:none;
  border-radius:10px;background:var(--series-1);color:#fff;cursor:pointer}
.searchform button:hover{filter:brightness(1.06)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.card{display:block;background:var(--surface-1);border:1px solid var(--border);
  border-radius:12px;padding:14px 16px;color:inherit}
.card:hover{text-decoration:none;border-color:var(--series-1)}
.cardname{font-weight:640;margin:0}
.cardticker{font-size:.78rem;color:var(--muted);margin:.1em 0 .5em}
.cardzone{font-weight:640;margin:0 0 .5em}
.cardstats{display:grid;grid-template-columns:1fr 1fr;gap:4px 10px;margin:0}
.cardstats div{display:flex;justify-content:space-between;gap:8px;font-size:.8rem}
.cardstats dt{color:var(--muted);margin:0}
.cardstats dd{margin:0;font-variant-numeric:tabular-nums}
.cardage{font-size:.72rem;color:var(--muted);margin:.6em 0 0}
.results{list-style:none;padding:0;margin:14px 0}
.result{display:flex;align-items:center;gap:12px;padding:12px 14px;
  border:1px solid var(--border);border-radius:10px;margin-bottom:8px;
  background:var(--surface-1)}
.result:hover{border-color:var(--series-1)}
.result a{flex:1;color:inherit;display:flex;flex-direction:column}
.result a:hover{text-decoration:none}
.rname{font-weight:620}
.rmeta{font-size:.78rem;color:var(--muted)}
.rscore{font-variant-numeric:tabular-nums;font-size:.82rem;color:var(--text-secondary);
  border:1px solid var(--border);border-radius:999px;padding:2px 9px}
.steps{padding-left:1.2em;color:var(--text-secondary);max-width:70ch}
.steps li{margin:.45em 0}
.steps strong{color:var(--text-primary)}
table{border-collapse:collapse;width:100%;font-size:.87rem}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top}
thead th{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  font-weight:640;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}
.muted{color:var(--muted)}
.nodata{color:var(--muted)}
.status-good{color:var(--status-good)}
.status-warning{color:#8a6100}
.status-critical{color:var(--status-critical)}
.status-muted{color:var(--muted)}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]) .status-warning{color:var(--status-warning)}
}
.notes{font-size:.84rem;color:var(--text-secondary);padding-left:1.15em}
.callout{border:1px solid var(--border);border-radius:10px;padding:12px 15px;margin:12px 0}
.callout.critical{border-color:var(--status-critical);
  background:color-mix(in srgb,var(--status-critical) 9%,transparent)}
.btn{display:inline-block;padding:9px 18px;border-radius:9px;background:var(--series-1);
  color:#fff;font-weight:620;font-size:.9rem;margin-right:8px}
.btn:hover{text-decoration:none;filter:brightness(1.06)}
.btn.ghost{background:transparent;color:var(--text-primary);border:1px solid var(--border)}
.working{text-align:center;padding:40px 0}
.working h1{margin-bottom:.2em}
.working .lead{margin:0 auto 22px;max-width:52ch}
.progress{height:6px;background:var(--grid);border-radius:999px;overflow:hidden;
  max-width:420px;margin:0 auto 14px}
.progress .bar{height:100%;width:38%;background:var(--series-1);border-radius:999px;
  animation:slide 1.6s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(300%)}}
@media (prefers-reduced-motion:reduce){.progress .bar{animation:none;width:100%}}
.jobstate{font-weight:600}
.reportbar{display:flex;align-items:center;justify-content:space-between;gap:12px;
  flex-wrap:wrap;padding:10px 14px;background:var(--surface-1);
  border:1px solid var(--border);border-radius:10px;margin-bottom:14px;font-size:.85rem}
.sitefoot{border-top:1px solid var(--border);padding:16px 20px;max-width:1080px;
  margin:0 auto;color:var(--text-secondary);font-size:.83rem}
"""
