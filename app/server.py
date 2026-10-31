#!/usr/bin/env python3
"""Chat local para resumir pdf/txt/md com modelos do Ollama.

Resultados ficam em SQLite; histórico e relatórios são páginas renderizadas
a partir do banco. Só biblioteca padrão. Um job por vez (a CPU é o gargalo:
rodar modelos em paralelo não reduz o tempo total e distorce as medições).
"""
import html
import json
import os
import re
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OLLAMA = os.environ.get("OLLAMA_HOST", "http://ollama:11434").rstrip("/")
OUT_DIR = Path(os.environ.get("OUT_DIR", "/outputs"))
DB_PATH = Path(os.environ.get("DB_PATH", str(OUT_DIR / "resumos.db")))
PORT = int(os.environ.get("PORT", "8000"))
STATIC = Path(__file__).parent / "static"
README_PATH = Path(os.environ.get("README_PATH", Path(__file__).parent / "README.md"))

PROMPT = ("Resuma o texto recebido, sempre em português. Responda somente com o "
          "resumo, sem saudações, perguntas ou comentários finais.")
MAX_UPLOAD = 20 * 1024 * 1024
NUM_PREDICT = 1024            # teto de tokens gerados (também reservado no contexto)
CTX_MIN, CTX_MAX = 2048, 16384
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}$")

job_lock = threading.Lock()


class UserError(Exception):
    """Erro de validação mostrado ao usuário (HTTP 400)."""


# ---------- SQLite ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT    NOT NULL,              -- UTC, ISO 8601 com Z
  filename   TEXT    NOT NULL,
  ftype      TEXT    NOT NULL,
  size_bytes INTEGER NOT NULL,
  chars      INTEGER NOT NULL,
  ctx        INTEGER NOT NULL,
  warnings   TEXT    NOT NULL DEFAULT '[]'  -- JSON: lista de strings
);
CREATE TABLE IF NOT EXISTS results (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  model     TEXT    NOT NULL,
  ok        INTEGER NOT NULL,
  summary   TEXT,
  error     TEXT,
  load_s    REAL, read_s REAL, gen_s REAL, tokens INTEGER, tok_s REAL,
  truncated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(db()) as c, c:
        c.executescript(SCHEMA)


def save_run(info, results):
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with closing(db()) as c, c:   # `with c` = uma transação só
        cur = c.execute(
            "INSERT INTO runs (created_at, filename, ftype, size_bytes, chars, ctx, warnings)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (created, info["filename"], info["ftype"], info["size_bytes"], info["chars"],
             info["ctx"], json.dumps(info["warnings"], ensure_ascii=False)))
        run_id = cur.lastrowid
        for r in results:
            c.execute(
                "INSERT INTO results (run_id, model, ok, summary, error, load_s, read_s, gen_s,"
                " tokens, tok_s, truncated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, r["model"], int(r["ok"]), r.get("summary"), r.get("error"),
                 r.get("load_s"), r.get("read_s"), r.get("gen_s"), r.get("tokens"),
                 r.get("tok_s"), int(r.get("truncated", False))))
    return run_id


def get_run(run_id):
    with closing(db()) as c:
        run = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            return None, []
        return run, c.execute("SELECT * FROM results WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()


def list_runs(limit=200):
    with closing(db()) as c:
        runs = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        by_run = {}
        if runs:
            marks = ",".join("?" * len(runs))
            for r in c.execute(f"SELECT run_id, model, ok FROM results WHERE run_id IN ({marks}) ORDER BY id",
                               [x["id"] for x in runs]):
                by_run.setdefault(r["run_id"], []).append(r)
        return runs, by_run


# ---------- entrada: tipo, texto, contexto ----------

def detect_type(data: bytes, filename: str):
    """Retorna (tipo, aviso). PDF pelo conteúdo; txt/md pela extensão."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if data[:4] == b"%PDF":
        warn = None if ext == "pdf" else (
            f"A extensão é .{ext or '?'} mas o conteúdo é PDF; tratado como pdf.")
        return "pdf", warn
    if ext == "pdf":
        raise UserError("O arquivo tem extensão .pdf mas o conteúdo não é um PDF válido.")
    if ext not in ("txt", "md", "markdown"):
        raise UserError(f"Tipo não compatível (.{ext or 'sem extensão'}). Use pdf, txt ou md.")
    if b"\0" in data[:8192]:
        raise UserError("O arquivo parece binário, não texto; não é compatível como txt/md.")
    return ("md" if ext in ("md", "markdown") else "txt"), None


def extract_text(data: bytes, ftype: str) -> str:
    if ftype == "pdf":
        try:
            p = subprocess.run(["pdftotext", "-", "-"], input=data,
                               capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise UserError("Tempo esgotado ao extrair o texto do PDF.")
        if p.returncode != 0:
            raise UserError("Não foi possível ler o PDF (corrompido ou protegido por senha).")
        text = p.stdout.decode("utf-8", errors="replace")
        empty = "PDF sem texto extraível (provavelmente escaneado; seria preciso OCR)."
    else:
        text = data.decode("utf-8", errors="replace")
        empty = "O arquivo está vazio."
    if not text.strip():
        raise UserError(empty)
    return text


def plan_context(text: str):
    """num_ctx ajustado ao texto; avisa se o texto não cabe (o padrão do Ollama,
    4096, truncaria sem avisar)."""
    need = len(text) // 3 + len(PROMPT) // 3 + 100 + NUM_PREDICT
    ctx = min(max(-(-need // 1024) * 1024, CTX_MIN), CTX_MAX)
    warn = None
    if need > CTX_MAX:
        warn = (f"O texto (~{need - NUM_PREDICT} tokens) excede o contexto máximo de "
                f"{CTX_MAX}; o modelo verá só parte dele.")
    return ctx, warn


# ---------- Ollama ----------

def ollama_stream(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", e.reason)
        except Exception:
            msg = e.reason
        raise RuntimeError(str(msg))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama indisponível: {e.reason}")
    with resp:
        for line in resp:
            if line.strip():
                obj = json.loads(line)
                if "error" in obj:
                    raise RuntimeError(obj["error"])
                yield obj


def installed_models():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
            return sorted(m["name"] for m in json.load(r)["models"])
    except Exception:
        return None


def ollama_json(path, body=None, timeout=5):
    """Chamada simples (sem streaming) à API do Ollama; levanta exceção se falhar."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OLLAMA + path, data,
                                 {"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def models_info():
    """Versão, modelos instalados (com detalhes) e o que está na memória agora.
    Retorna None se o Ollama estiver offline."""
    try:
        version = ollama_json("/api/version")["version"]
        tags = ollama_json("/api/tags")["models"]
    except Exception:
        return None
    try:
        loaded = {m["name"]: m for m in ollama_json("/api/ps").get("models", [])}
    except Exception:
        loaded = {}
    models = []
    for m in sorted(tags, key=lambda x: x["name"]):
        ctx, caps = None, []
        try:   # contexto máximo e capacidades só vêm de /api/show
            show = ollama_json("/api/show", {"model": m["name"]})
            caps = show.get("capabilities", [])
            ctx = next((v for k, v in show.get("model_info", {}).items()
                        if k.endswith(".context_length")), None)
        except Exception:
            pass
        d = m.get("details", {})
        live = loaded.get(m["name"])
        models.append({
            "name": m["name"], "params": d.get("parameter_size"), "quant": d.get("quantization_level"),
            "family": d.get("family"), "size": m.get("size", 0), "modified": m.get("modified_at"),
            "ctx": ctx, "caps": caps,
            "loaded": bool(live), "mem": live.get("size") if live else None,
            "expires": live.get("expires_at") if live else None,
        })
    return {"version": version, "models": models}


def ollama_status():
    """Estado do Ollama para a bolinha verde/vermelha do menu."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=5) as r:
            version = json.load(r).get("version", "?")
    except Exception:
        return {"ok": False}
    return {"ok": True, "version": version, "models": len(installed_models() or [])}


def render_status_fragment():
    """Trecho HTML para o htmx: conteúdo do item de status do menu."""
    s = ollama_status()
    if s["ok"]:
        state, text = "on", f'Ollama online · v{html.escape(str(s["version"]))} · {s["models"]} modelos'
    else:
        state, text = "off", "Ollama offline"
    return f'<span class="dot dot-{state}" aria-hidden="true"></span><span>{text}</span>'


def ensure_model(model, emit):
    have = installed_models()
    if have is None:
        raise RuntimeError("Ollama indisponível")
    if model in have or f"{model}:latest" in have:
        return
    for ev in ollama_stream("/api/pull", {"model": model, "stream": True}, timeout=3600):
        pct = None
        if ev.get("total"):
            pct = round(100 * ev.get("completed", 0) / ev["total"])
        emit({"type": "pull", "model": model, "status": ev.get("status", ""), "percent": pct})


def run_model(model, text, ctx, emit):
    ensure_model(model, emit)
    body = {"model": model, "prompt": f"{PROMPT}\n\n---\n{text}\n---", "stream": True,
            "think": False, "keep_alive": "10m",
            "options": {"temperature": 0, "num_ctx": ctx, "num_predict": NUM_PREDICT}}
    parts, final = [], {}
    for ev in ollama_stream("/api/generate", body):
        if ev.get("response"):
            parts.append(ev["response"])
            emit({"type": "token", "model": model, "text": ev["response"]})
        if ev.get("done"):
            final = ev
    summary = "".join(parts).strip()
    if not summary:
        raise RuntimeError("O modelo devolveu um resumo vazio.")
    gen_s = final.get("eval_duration", 0) / 1e9
    return {
        "model": model, "ok": True, "summary": summary,
        "load_s": final.get("load_duration", 0) / 1e9,
        "read_s": final.get("prompt_eval_duration", 0) / 1e9,
        "gen_s": gen_s, "tokens": final.get("eval_count", 0),
        "tok_s": (final.get("eval_count", 0) / gen_s) if gen_s else 0,
        "truncated": final.get("done_reason") == "length",
    }


# ---------- páginas HTML (renderizadas a partir do banco) ----------

HEAD = (STATIC / "head.html").read_text(encoding="utf-8")
e = html.escape

# O container roda em UTC: o horário é guardado em UTC e convertido no navegador.
DATE_SCRIPT = """<script>
  document.querySelectorAll('time[datetime]').forEach(t => {
    const d = new Date(t.dateTime);
    if (isNaN(d)) return;
    t.textContent = t.dataset.fmt === 'time' ? d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })
      : t.dataset.fmt === 'date' ? d.toLocaleDateString('pt-BR')
      : d.toLocaleString('pt-BR');
  });
</script>"""


def time_tag(iso):
    return f'<time datetime="{e(iso)}">{e(iso.replace("T", " ").rstrip("Z"))} UTC</time>'


def nav(current, embed=False):
    if embed:   # dentro do modal do chat não há navegação própria
        return ""

    def link(href, label, active):
        cls = "font-medium text-foreground" if active else "text-muted-foreground hover:text-foreground"
        return f'<a href="{href}" class="{cls}">{label}</a>'
    return ('<nav class="flex gap-4 text-sm">'
            + link("/", "← Chat", False) + link("/history", "Histórico", current == "history")
            + link("/help", "Ajuda", current == "help") + "</nav>")


def _fmt(x):
    return f"{(x or 0):.1f}s"


def render_report(run, results, embed=False):
    rows = []
    for r in results:
        if r["ok"]:
            note = ('<span class="badge badge-destructive">cortado em %d tokens</span> ' % NUM_PREDICT
                    if r["truncated"] else "")
            rows.append(
                f'<tr class="border-b border-border align-top">'
                f'<td class="p-3 font-medium whitespace-nowrap">{e(r["model"])}</td>'
                f'<td class="p-3"><span class="badge">{e(run["ftype"])}</span></td>'
                f'<td class="p-3 whitespace-nowrap text-muted-foreground">{_fmt(r["load_s"])}</td>'
                f'<td class="p-3 whitespace-nowrap text-muted-foreground">{_fmt(r["read_s"])}</td>'
                f'<td class="p-3 whitespace-nowrap">{_fmt(r["gen_s"])}'
                f'<div class="text-xs text-muted-foreground">{(r["tok_s"] or 0):.1f} tok/s</div></td>'
                f'<td class="p-3">{note}<div class="md whitespace-pre-wrap" data-md="{e(r["summary"])}">{e(r["summary"])}</div></td>'
                f'</tr>')
        else:
            rows.append(
                f'<tr class="border-b border-border align-top">'
                f'<td class="p-3 font-medium whitespace-nowrap">{e(r["model"])}</td>'
                f'<td class="p-3"><span class="badge">{e(run["ftype"])}</span></td>'
                f'<td class="p-3 text-muted-foreground" colspan="3">—</td>'
                f'<td class="p-3"><span class="badge badge-destructive">erro</span> {e(r["error"] or "")}</td>'
                f'</tr>')
    warns = "".join(f'<div class="alert">{e(w)}</div>' for w in json.loads(run["warnings"]))
    return f"""<!doctype html>
<html lang="pt-BR"><head><title>Relatório #{run["id"]} · {e(run["filename"])}</title>
{HEAD}</head>
<body class="min-h-screen">
<main class="mx-auto max-w-6xl px-4 py-8 space-y-6">
  {nav("report", embed)}
  <header>
    <p class="text-sm text-muted-foreground">Relatório #{run["id"]} · {time_tag(run["created_at"])}</p>
    <h1 class="text-2xl font-semibold tracking-tight">{e(run["filename"])}</h1>
  </header>
  {warns}
  <section class="card">
    <div class="p-6 grid gap-4 sm:grid-cols-4">
      <div><div class="stat-label">Tipo</div><div class="stat-value">{e(run["ftype"])}</div></div>
      <div><div class="stat-label">Tamanho</div><div class="stat-value">{run["size_bytes"] / 1024:.1f} KB</div></div>
      <div><div class="stat-label">Texto extraído</div><div class="stat-value">{run["chars"]:,} caracteres</div></div>
      <div><div class="stat-label">Contexto usado</div><div class="stat-value">{run["ctx"]:,} tokens</div></div>
    </div>
  </section>
  <section class="card overflow-hidden">
    <div class="p-6 pb-3"><h2 class="font-semibold leading-none tracking-tight">Resumos</h2>
      <p class="text-sm text-muted-foreground mt-1.5">Modelos executados em sequência. Carga = colocar o modelo na RAM; leitura = processar o texto de entrada; geração = escrever o resumo.</p></div>
    <div class="overflow-x-auto"><table class="w-full text-sm">
      <thead><tr class="border-y border-border text-left text-muted-foreground">
        <th class="p-3 font-medium">Modelo</th><th class="p-3 font-medium">Tipo</th>
        <th class="p-3 font-medium">Carga</th><th class="p-3 font-medium">Leitura</th>
        <th class="p-3 font-medium">Geração</th><th class="p-3 font-medium">Resumo</th></tr></thead>
      <tbody>{"".join(rows)}</tbody></table></div>
  </section>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>
<script>
  // Renderiza o markdown do modelo; se o CDN falhar, o texto puro continua visível.
  if (window.marked && window.DOMPurify) document.querySelectorAll('.md').forEach(el => {{
    el.innerHTML = DOMPurify.sanitize(marked.parse(el.dataset.md));
    el.classList.remove('whitespace-pre-wrap');
  }});
</script>
{DATE_SCRIPT}
</body></html>"""


def render_history(embed=False):
    q = "?embed=1" if embed else ""
    runs, by_run = list_runs()
    rows = []
    for run in runs:
        res = by_run.get(run["id"], [])
        chips = "".join(
            f'<span class="badge{"" if r["ok"] else " badge-destructive"} mr-1">{e(r["model"])}</span>'
            for r in res)
        kind = "comparação" if len(res) > 1 else "resumo"
        rows.append(
            f'<tr class="cursor-pointer border-b border-border hover:bg-muted/50" '
            f'onclick="location.href=\'/reports/{run["id"]}{q}\'">'
            f'<td class="p-3 text-muted-foreground">#{run["id"]}</td>'
            f'<td class="p-3 whitespace-nowrap">{time_tag(run["created_at"])}</td>'
            f'<td class="p-3 font-medium"><a href="/reports/{run["id"]}{q}" class="hover:underline">{e(run["filename"])}</a></td>'
            f'<td class="p-3"><span class="badge">{e(run["ftype"])}</span></td>'
            f'<td class="p-3 text-muted-foreground">{kind}</td>'
            f'<td class="p-3">{chips}</td></tr>')
    body = ("".join(rows) if rows else
            '<tr><td colspan="6" class="p-6 text-center text-muted-foreground">'
            'Nenhum resultado ainda. Gere um relatório no <a href="/" class="underline">chat</a>.</td></tr>')
    return f"""<!doctype html>
<html lang="pt-BR"><head><title>Histórico de resultados</title>
{HEAD}</head>
<body class="min-h-screen">
<main class="mx-auto max-w-6xl px-4 py-8 space-y-6">
  {nav("history", embed)}
  <header>
    <h1 class="text-2xl font-semibold tracking-tight">Histórico de resultados</h1>
    <p class="text-sm text-muted-foreground">Clique em um registro para abrir o relatório com a matriz de comparação.</p>
  </header>
  <section class="card overflow-hidden">
    <div class="overflow-x-auto"><table class="w-full text-sm">
      <thead><tr class="border-b border-border text-left text-muted-foreground">
        <th class="p-3 font-medium">#</th><th class="p-3 font-medium">Data</th>
        <th class="p-3 font-medium">Arquivo</th><th class="p-3 font-medium">Tipo</th>
        <th class="p-3 font-medium">Execução</th><th class="p-3 font-medium">Modelos</th></tr></thead>
      <tbody>{body}</tbody></table></div>
  </section>
</main>
{DATE_SCRIPT}
</body></html>"""


def fmt_bytes(n):
    return f"{n / (1 << 30):.1f} GB" if n >= (1 << 30) else f"{n / (1 << 20):.0f} MB"


def local_time(iso, fmt_name=None):
    """<time> que o navegador converte para o fuso local (o texto é só o plano B).
    fmt_name: None = data e hora, "time" = só hora, "date" = só data."""
    if not iso:
        return "—"
    fmt = f' data-fmt="{fmt_name}"' if fmt_name else ""
    return f'<time datetime="{e(iso)}"{fmt}>{e(iso[:16].replace("T", " "))}</time>'


def render_models(embed=False):
    info = models_info()
    if info is None:
        body = ('<section class="card p-6"><span class="badge badge-destructive">offline</span> '
                '<span class="ml-2">O Ollama não está respondendo. Verifique '
                '<code>docker compose ps</code>.</span></section>')
    else:
        ms = info["models"]
        total = sum(m["size"] for m in ms)
        rows = []
        for m in ms:
            caps = "".join(f'<span class="badge mr-1">{e(c)}</span>' for c in m["caps"]) or "—"
            mem = (f'<span class="badge whitespace-nowrap">na memória</span>'
                   f'<div class="text-xs text-muted-foreground mt-1 whitespace-nowrap">'
                   f'{fmt_bytes(m["mem"] or 0)} · sai às {local_time(m["expires"], "time")}</div>'
                   if m["loaded"] else '<span class="text-muted-foreground">—</span>')
            ctx_txt = f"{m['ctx']:,} tokens" if m["ctx"] else "—"
            rows.append(
                f'<tr class="border-b border-border align-top">'
                f'<td class="p-3 font-medium whitespace-nowrap">{e(m["name"])}</td>'
                f'<td class="p-3 whitespace-nowrap">{e(m["params"] or "—")}</td>'
                f'<td class="p-3 whitespace-nowrap">{e(m["quant"] or "—")}</td>'
                f'<td class="p-3 whitespace-nowrap">{e(m["family"] or "—")}</td>'
                f'<td class="p-3 whitespace-nowrap">{ctx_txt}</td>'
                f'<td class="p-3 whitespace-nowrap">{fmt_bytes(m["size"])}</td>'
                f'<td class="p-3">{caps}</td>'
                f'<td class="p-3 whitespace-nowrap text-muted-foreground">{local_time(m["modified"], "date")}</td>'
                f'<td class="p-3">{mem}</td></tr>')
        in_mem = sum(1 for m in ms if m["loaded"])
        body = f"""<section class="card"><div class="p-6 grid gap-4 sm:grid-cols-4">
      <div><div class="stat-label">Ollama</div><div class="stat-value"><span class="dot dot-on" style="display:inline-block"></span>online · v{e(str(info["version"]))}</div></div>
      <div><div class="stat-label">Modelos instalados</div><div class="stat-value">{len(ms)}</div></div>
      <div><div class="stat-label">Espaço em disco</div><div class="stat-value">{fmt_bytes(total)}</div></div>
      <div><div class="stat-label">Na memória agora</div><div class="stat-value">{in_mem}</div></div>
    </div></section>
  <section class="card overflow-hidden"><div class="overflow-x-auto"><table class="w-full text-sm">
      <thead><tr class="border-b border-border text-left text-muted-foreground">
        <th class="p-3 font-medium">Modelo</th><th class="p-3 font-medium">Parâmetros</th>
        <th class="p-3 font-medium">Quantização</th><th class="p-3 font-medium">Família</th>
        <th class="p-3 font-medium">Contexto máx.</th><th class="p-3 font-medium">Disco</th>
        <th class="p-3 font-medium">Capacidades</th><th class="p-3 font-medium">Atualizado</th>
        <th class="p-3 font-medium">Memória</th></tr></thead>
      <tbody>{"".join(rows) or '<tr><td colspan="9" class="p-6 text-center text-muted-foreground">Nenhum modelo instalado.</td></tr>'}</tbody>
  </table></div></section>"""
    return f"""<!doctype html>
<html lang="pt-BR"><head><title>Modelos instalados</title>
{HEAD}</head>
<body class="min-h-screen">
<main class="mx-auto max-w-6xl px-4 py-8 space-y-6">
  {nav("models", embed)}
  <header>
    <h1 class="text-2xl font-semibold tracking-tight">Modelos instalados</h1>
    <p class="text-sm text-muted-foreground">Dados lidos do Ollama agora. Modelos ficam na memória por 10 minutos depois do último uso.</p>
  </header>
  {body}
</main>
{DATE_SCRIPT}
</body></html>"""


def render_help(embed=False):
    """Página de ajuda: renderiza o README.md (única fonte do texto) no navegador."""
    return f"""<!doctype html>
<html lang="pt-BR"><head><title>Ajuda · Resumos locais</title>
{HEAD}</head>
<body class="min-h-screen">
<main class="mx-auto max-w-3xl px-4 py-8 space-y-6">
  {nav("help", embed)}
  <article id="doc" class="card md p-6 whitespace-pre-wrap">Carregando…</article>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>
<script>
  const doc = document.getElementById('doc');
  fetch('/help.md').then(r => r.ok ? r.text() : Promise.reject()).then(t => {{
    if (window.marked && window.DOMPurify) {{
      doc.classList.remove('whitespace-pre-wrap');
      doc.innerHTML = DOMPurify.sanitize(marked.parse(t));
    }} else {{ doc.textContent = t; }}   // sem CDN: mostra o texto puro
  }}).catch(() => {{ doc.textContent = 'Não foi possível carregar a ajuda (README.md).'; }});
</script>
</body></html>"""


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "resumos"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _page(self, code, page):
        self._send(code, page, "text/html; charset=utf-8")

    def _host_ok(self):
        # Contra DNS rebinding: só aceita localhost / 127.0.0.1.
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1", "[::1]")

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"message": "host não permitido"})
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        embed = urllib.parse.parse_qs(parsed.query).get("embed") == ["1"]
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/":
            return self._page(200, (STATIC / "index.html").read_text(encoding="utf-8")
                              .replace("<!--HEAD-->", HEAD))
        if path == "/history":
            return self._page(200, render_history(embed))
        if path == "/models":
            return self._page(200, render_models(embed))
        if path == "/help":
            return self._page(200, render_help(embed))
        if path == "/help.md" and README_PATH.is_file():
            return self._send(200, README_PATH.read_text(encoding="utf-8"),
                              "text/markdown; charset=utf-8")
        m = re.fullmatch(r"/reports/([0-9]{1,9})", path)
        if m:
            run, results = get_run(int(m.group(1)))
            if run:
                return self._page(200, render_report(run, results, embed))
        if path == "/fragments/ollama-status":
            return self._send(200, render_status_fragment(), "text/html; charset=utf-8")
        if path == "/api/models":
            return self._json(200, {"models": installed_models() or []})
        self._json(404, {"message": "não encontrado"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"message": "host não permitido"})
        url = urllib.parse.urlparse(self.path)
        # Header customizado: navegadores só o enviam de outra origem após preflight,
        # que não respondemos -> bloqueia CSRF de outros sites.
        if url.path != "/api/summarize" or self.headers.get("X-Upload") != "1":
            return self._json(404, {"message": "não encontrado"})
        q = urllib.parse.parse_qs(url.query)
        try:
            models, filename, data = self._parse_request(q)
            ftype, type_warn = detect_type(data, filename)
            text = extract_text(data, ftype)
            ctx, ctx_warn = plan_context(text)
        except UserError as err:
            return self._json(400, {"message": str(err)})
        info = {"filename": filename, "ftype": ftype, "size_bytes": len(data),
                "chars": len(text), "ctx": ctx,
                "warnings": [w for w in (type_warn, ctx_warn) if w]}
        self._stream_job(models, text, ctx, info)

    def _parse_request(self, q):
        models = [m.strip() for m in (q.get("models", [""])[0]).split(",") if m.strip()]
        if not models:
            raise UserError("Selecione ao menos um modelo.")
        if len(models) > 6 or any(not MODEL_RE.match(m) for m in models):
            raise UserError("Lista de modelos inválida.")
        filename = os.path.basename(q.get("filename", ["arquivo"])[0])[:200] or "arquivo"
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise UserError("Envie o arquivo no corpo da requisição.")
        if length <= 0:
            raise UserError("O arquivo está vazio.")
        if length > MAX_UPLOAD:
            raise UserError(f"Arquivo maior que o limite de {MAX_UPLOAD // 1024 // 1024} MB.")
        return list(dict.fromkeys(models)), filename, self.rfile.read(length)

    def _stream_job(self, models, text, ctx, info):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        try:
            emit({"type": "info", **info, "est_tokens": len(text) // 3})
            if not job_lock.acquire(blocking=False):
                emit({"type": "queue"})
                job_lock.acquire()
            try:
                results = []
                for i, model in enumerate(models, 1):
                    emit({"type": "model_start", "model": model, "index": i, "total": len(models)})
                    try:
                        r = run_model(model, text, ctx, emit)
                        emit({"type": "model_done", **{k: v for k, v in r.items() if k != "summary"}})
                    except RuntimeError as err:
                        r = {"model": model, "ok": False, "error": str(err)}
                        emit({"type": "model_error", "model": model, "message": str(err)})
                    results.append(r)
                run_id = save_run(info, results)
                emit({"type": "done", "report": f"/reports/{run_id}", "run_id": run_id})
            finally:
                job_lock.release()
        except (BrokenPipeError, ConnectionResetError):
            pass  # cliente saiu; o job para aqui e nada é salvo


if __name__ == "__main__":
    init_db()
    print(f"Chat em http://0.0.0.0:{PORT} (Ollama: {OLLAMA}, banco: {DB_PATH})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
