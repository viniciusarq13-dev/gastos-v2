import os, json, re, copy
try:
    import psycopg2
    import psycopg2.extras
    HAS_PG = True
except ImportError:
    HAS_PG = False
import sqlite3
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import pdfplumber

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gastos-v2-dev-key")

_IS_CLOUD    = os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PORT")
DATABASE_URL = os.environ.get("DATABASE_URL", "")          # PostgreSQL (Supabase)
_USE_PG      = bool(DATABASE_URL and HAS_PG)               # True = nuvem, False = local SQLite
_DATA_DIR    = "/tmp/gastos_data" if _IS_CLOUD else os.path.join(os.path.dirname(__file__), "data")
DB           = os.path.join(_DATA_DIR, "gastos.db")        # usado só no modo SQLite
CFG          = os.path.join(_DATA_DIR, "config.json")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

SIMPLES_PCT  = 0.155
DEFAULT_SAL  = 31000.0

MESES = ["Janeiro","Fevereiro","Março","Abril","Maio","Junho",
         "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]

BUDGET_BASE = {
    "💳 Cartões": [
        {"name":"Cartão Caixa – PJ",  "valor":100.0},
        {"name":"Cartão Caixa – Inter","valor":10.0},
        {"name":"Cartão Caixa – PF",  "valor":4500.0},
    ],
    "🏠 Despesas Fixas": [
        {"name":"Internet Apartamento", "valor":105.0},
        {"name":"Condomínio Tostes",    "valor":393.0},
        {"name":"Vivo",                 "valor":79.0},
        {"name":"Luz",                  "valor":390.0},
        {"name":"Gás Apartamento",      "valor":65.0},
        {"name":"Prestação Apartamento","valor":1400.0},
        {"name":"Beach Tênis",          "valor":285.0},
        {"name":"Plano de Saúde",       "valor":700.0},
        {"name":"Cabelo / Barba",       "valor":100.0},
        {"name":"Mercado",              "valor":950.0},
    ],
    "🎯 Despesas Variáveis": [
        {"name":"Amor",    "valor":700.0},
        {"name":"AGE Rio", "valor":800.0},
    ],
    "💼 Trabalho / Empresa": [
        {"name":"Contabilidade",            "valor":611.0},
        {"name":"Contabilidade Savoretti",  "valor":295.0},
        {"name":"Escritório",               "valor":750.0},
        {"name":"Simples Nacional (15,5%)", "valor":0.0},
        {"name":"Catiele",                  "valor":1080.0},
        {"name":"Ida Rio",                  "valor":2500.0},
        {"name":"RRT_RJ",                   "valor":125.40},
        {"name":"INSS – Savoretti",         "valor":450.0},
        {"name":"INSS",                     "valor":156.0},
        {"name":"ISS Barra Mansa",          "valor":90.0},
    ],
    "🎲 Despesas Diversas": [],
}

KEYWORDS = {
    "✈️ Viagem":["maceio","maceió","sete coquei","mirante","pousada","milagres","patacho",
                 "porto de pedras","localiza","gru to go","bali porto","villa trematerra",
                 "nacasa","tatuah","docebrisamoda","umami","tapiocaria","segredos"],
    "💼 Trabalho / Empresa":["simples nacional","catiele","savoretti","thiago adauto","mariote",
                              "contabilidade","rrt","escritorio","pjbank","inss","iss"],
    "🏠 Despesas Fixas":["light","vivo","condominio","bradesco","caixa econômica","caixa economica",
                          "superlógica","superlogica","banco inter","unimed"],
    "💳 Cartões":["cartao","cartão"],
    "🍔 Alimentação":["padaria","restaurante","lanche","cafe","café","pizza","hamburger",
                      "hamburguer","mercado","supermercado","hortifruti","acougue","açougue",
                      "sorveteria","gelato","ifood","jim.com","buono","bar ","grano",
                      "grazie","parma","royal aterrado","cervejaria"],
    "⛽ Combustível":["posto","combustivel","combustível","gasolina","etanol","abastec",
                      "elovias","ecoriominas","pedagio","pedágio","rodosnack"],
    "🎰 Loteria":["caixa loterias","loteria"],
    "💊 Saúde":["farmacia","farmácia","drogaria","droga","amaral e bruno","raia"],
    "🎯 Despesas Variáveis":["facebook","instagram","noxpay","pushinpay","cpg","fazticket",
                              "mariote tennis","beach","aram clube"],
    "💰 Transferências":["pix enviado","transferência pix enviada","transferencia pix enviada"],
    "📈 Receitas":["pix recebido","transferência pix recebida","rendimentos","liberação",
                   "liberacao","resgate cdb","resgate"],
}

def categorize(desc):
    d = desc.lower()
    for cat, kws in KEYWORDS.items():
        if any(k in d for k in kws):
            return cat
    return "📦 Outros"

# ── CONFIG ────────────────────────────────────────────────────────
def load_config():
    if _USE_PG:
        try:
            con = get_db()
            row = con.execute("SELECT value FROM app_config WHERE key='main'").fetchone()
            con.close()
            if row:
                return json.loads(row["value"])
        except Exception:
            pass
        return {"salario_por_mes": {}, "budget_overrides": {}, "budget_custom_items": {}}
    # SQLite: use JSON file
    if os.path.exists(CFG):
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    return {"salario_por_mes": {}, "budget_overrides": {}, "budget_custom_items": {}}

def save_config(cfg):
    if _USE_PG:
        try:
            con = get_db()
            val = json.dumps(cfg, ensure_ascii=False)
            con.execute(
                "INSERT INTO app_config (key, value) VALUES ('main', ?) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (val,))
            con.commit()
            con.close()
        except Exception as e:
            print(f"Erro ao salvar config: {e}")
        return
    # SQLite: use JSON file
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def get_salario(mes_key):
    cfg = load_config()
    return float(cfg.get("salario_por_mes", {}).get(mes_key, DEFAULT_SAL))

def build_budget(salario, mes_key, db_conn=None):
    b = copy.deepcopy(BUDGET_BASE)
    simples = round(salario * SIMPLES_PCT, 2)
    for item in b["💼 Trabalho / Empresa"]:
        if "Simples Nacional" in item["name"]:
            item["valor"] = simples

    # Despesas Diversas dinâmicas
    try:
        con = db_conn or get_db()
        rows = con.execute(
            "SELECT DISTINCT budget_item FROM expenses "
            "WHERE category='🎲 Despesas Diversas' AND budget_item!='' AND type='saida' AND mes_key=?",
            (mes_key,)
        ).fetchall()
        if not db_conn:
            con.close()
        for r in rows:
            if r["budget_item"]:
                b["🎲 Despesas Diversas"].append({"name": r["budget_item"], "valor": 0.0})
    except Exception:
        pass

    cfg       = load_config()
    overrides = cfg.get("budget_overrides", {}).get(mes_key, {})
    custom    = cfg.get("budget_custom_items", {}).get(mes_key, {})

    for cat, new_items in custom.items():
        if cat not in b:
            b[cat] = []
        existing = {i["name"] for i in b[cat]}
        for ni in new_items:
            if ni["name"] not in existing:
                b[cat].append({"name": ni["name"], "valor": float(ni["valor"]), "custom": True})

    for cat, items in b.items():
        for item in items:
            key = f"{cat}||{item['name']}"
            if key in overrides and cat != "🎲 Despesas Diversas":
                item["valor"] = float(overrides[key])

    return b

# ── DB ────────────────────────────────────────────────────────────
def init_db():
    if not _USE_PG:
        os.makedirs(_DATA_DIR, exist_ok=True)
    con = get_db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS expenses (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mes_key     TEXT    NOT NULL,
        description TEXT    NOT NULL,
        amount      REAL    NOT NULL,
        date        TEXT    NOT NULL,
        category    TEXT    NOT NULL,
        budget_item TEXT,
        type        TEXT    DEFAULT 'saida',
        source      TEXT    DEFAULT 'manual',
        created_at  TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS card_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mes_key     TEXT    NOT NULL,
        card_name   TEXT    NOT NULL,
        description TEXT    NOT NULL,
        amount      REAL    NOT NULL,
        date        TEXT,
        category    TEXT    DEFAULT 'Outros',
        created_at  TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS investments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mes_key     TEXT    NOT NULL,
        name        TEXT    NOT NULL,
        type        TEXT    NOT NULL,
        balance     REAL    DEFAULT 0,
        aporte      REAL    DEFAULT 0,
        rendimento  REAL    DEFAULT 0,
        institution TEXT,
        notes       TEXT,
        created_at  TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS loans (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT    NOT NULL,
        institution     TEXT,
        total_amount    REAL    DEFAULT 0,
        remaining_bal   REAL    DEFAULT 0,
        monthly_payment REAL    DEFAULT 0,
        total_parcelas  INTEGER DEFAULT 0,
        parcelas_pagas  INTEGER DEFAULT 0,
        due_day         INTEGER DEFAULT 1,
        start_date      TEXT,
        end_date        TEXT,
        interest_rate   REAL    DEFAULT 0,
        notes           TEXT,
        active          INTEGER DEFAULT 1,
        created_at      TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS loan_payments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        loan_id     INTEGER NOT NULL,
        mes_key     TEXT    NOT NULL,
        amount      REAL    NOT NULL,
        date        TEXT,
        notes       TEXT,
        created_at  TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS app_config (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    con.commit()
    con.close()

# ── DB CONNECTION ─────────────────────────────────────────────────
class PgConn:
    """Thin wrapper around psycopg2 that mimics sqlite3 interface."""
    def __init__(self, dsn):
        # Parse URL with regex so @ inside the password doesn't break parsing.
        # Greedy match on password ensures we split at the LAST @ before host.
        import re
        from urllib.parse import unquote
        m = re.match(
            r'[^:]+://([^:]+):(.+)@([^:@/]+):(\d+)/([^?]+)', dsn
        )
        if m:
            user, password, host, port, dbname = m.groups()
            self._con = psycopg2.connect(
                host=host, port=int(port),
                dbname=dbname,
                user=unquote(user),
                password=unquote(password),
                sslmode="require",
                connect_timeout=10,
            )
        else:
            self._con = psycopg2.connect(dsn)
        self._con.autocommit = False

    def execute(self, sql, params=()):
        sql = _pg_sql(sql)
        cur = self._con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return _PgCursor(cur)

    def executescript(self, script):
        # split on ; and run each statement
        cur = self._con.cursor()
        for stmt in script.split(";"):
            stmt = _pg_sql(stmt.strip())
            if stmt:
                try: cur.execute(stmt)
                except Exception: pass
        self._con.commit()

    def commit(self):  self._con.commit()
    def close(self):   self._con.close()
    def __enter__(self): return self
    def __exit__(self, *a): self.close()

class _PgCursor:
    def __init__(self, cur): self._cur = cur
    def fetchall(self):
        rows = self._cur.fetchall()
        return [_DictRow(r) for r in (rows or [])]
    def fetchone(self):
        row = self._cur.fetchone()
        return _DictRow(row) if row else None

class _DictRow:
    """Makes psycopg2 RealDictRow behave like sqlite3.Row (subscript + attribute)."""
    def __init__(self, d): self._d = dict(d) if d else {}
    def __getitem__(self, k): return self._d[k]
    def keys(self): return self._d.keys()
    def __iter__(self): return iter(self._d)

def _pg_sql(sql):
    """Convert SQLite SQL to PostgreSQL SQL."""
    import re as _re
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = sql.replace("datetime('now')", "NOW()")
    sql = sql.replace("?", "%s")
    return sql

def get_db():
    if _USE_PG:
        return PgConn(DATABASE_URL)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

# ── AUTH ──────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if APP_PASSWORD and not session.get("logged_in"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

@app.before_request
def check_api_auth():
    if request.path.startswith("/api/") and APP_PASSWORD:
        if not session.get("logged_in"):
            return jsonify({"error": "Não autorizado"}), 401

# ── PAGES ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return redirect("/dashboard")

@app.route("/login", methods=["GET","POST"])
def login():
    if not APP_PASSWORD:
        return redirect("/dashboard")
    error = ""
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["logged_in"] = True
            return redirect("/dashboard")
        error = "Senha incorreta."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("index.html", meses=MESES)

# ── CONFIG API ────────────────────────────────────────────────────
@app.route("/api/config/salario", methods=["POST"])
def api_set_salario():
    d = request.json
    mes_key = d.get("mes_key","")
    sal = float(d.get("salario", DEFAULT_SAL))
    cfg = load_config()
    if "salario_por_mes" not in cfg:
        cfg["salario_por_mes"] = {}
    cfg["salario_por_mes"][mes_key] = sal
    save_config(cfg)
    return jsonify({"ok": True, "salario": sal, "simples": round(sal*SIMPLES_PCT,2)})

@app.route("/api/config/salario/<mes_key>")
def api_get_salario(mes_key):
    sal = get_salario(mes_key)
    return jsonify({"salario": sal, "simples": round(sal*SIMPLES_PCT,2)})

# ── BUDGET API ────────────────────────────────────────────────────
@app.route("/api/budget/<mes_key>")
def api_budget(mes_key):
    sal = get_salario(mes_key)
    return jsonify(build_budget(sal, mes_key))

@app.route("/api/budget/override", methods=["POST"])
def api_budget_override():
    d = request.json
    mes_key = d.get("mes_key","")
    cat = d.get("category",""); name = d.get("name",""); valor = float(d.get("valor",0))
    key = f"{cat}||{name}"
    cfg = load_config()
    if "budget_overrides" not in cfg: cfg["budget_overrides"] = {}
    if mes_key not in cfg["budget_overrides"]: cfg["budget_overrides"][mes_key] = {}
    cfg["budget_overrides"][mes_key][key] = valor
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/budget/override/reset", methods=["POST"])
def api_budget_override_reset():
    d = request.json
    mes_key = d.get("mes_key",""); cat = d.get("category",""); name = d.get("name","")
    cfg = load_config()
    ov = cfg.get("budget_overrides",{}).get(mes_key,{})
    if cat and name:
        ov.pop(f"{cat}||{name}", None)
    else:
        cfg.get("budget_overrides",{})[mes_key] = {}
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/budget/item/add", methods=["POST"])
def api_budget_item_add():
    d = request.json
    mes_key = d.get("mes_key",""); cat = d.get("category","").strip()
    name = d.get("name","").strip(); valor = float(d.get("valor",0))
    if not cat or not name:
        return jsonify({"error":"Categoria e nome obrigatórios"}), 400
    cfg = load_config()
    c = cfg.setdefault("budget_custom_items",{}).setdefault(mes_key,{}).setdefault(cat,[])
    if any(i["name"]==name for i in c):
        return jsonify({"error":"Item já existe nesta categoria"}), 400
    c.append({"name":name,"valor":valor})
    save_config(cfg)
    return jsonify({"ok":True})

@app.route("/api/budget/item/remove", methods=["POST"])
def api_budget_item_remove():
    d = request.json
    mes_key = d.get("mes_key",""); cat = d.get("category","").strip(); name = d.get("name","").strip()
    cfg = load_config()
    cats = cfg.get("budget_custom_items",{}).get(mes_key,{})
    if cat in cats:
        cats[cat] = [i for i in cats[cat] if i["name"]!=name]
    cfg.get("budget_overrides",{}).get(mes_key,{}).pop(f"{cat}||{name}",None)
    save_config(cfg)
    return jsonify({"ok":True})

# ── EXPENSES API ──────────────────────────────────────────────────
@app.route("/api/expenses/<mes_key>")
def api_list_expenses(mes_key):
    con = get_db()
    rows = con.execute("SELECT * FROM expenses WHERE mes_key=? ORDER BY date DESC,id DESC",(mes_key,)).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/expenses", methods=["POST"])
def api_create_expense():
    d = request.json; con = get_db()
    con.execute(
        "INSERT INTO expenses (mes_key,description,amount,date,category,budget_item,type,source) VALUES (?,?,?,?,?,?,?,?)",
        (d["mes_key"],d["description"],float(d["amount"]),d["date"],
         d["category"],d.get("budget_item",""),d.get("type","saida"),"manual"))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/expenses/<int:eid>", methods=["PUT"])
def api_update_expense(eid):
    d = request.json; con = get_db()
    con.execute(
        "UPDATE expenses SET description=?,amount=?,date=?,category=?,budget_item=?,type=? WHERE id=?",
        (d["description"],float(d["amount"]),d["date"],d["category"],d.get("budget_item",""),d.get("type","saida"),eid))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/expenses/<int:eid>", methods=["DELETE"])
def api_delete_expense(eid):
    con = get_db()
    con.execute("DELETE FROM expenses WHERE id=?",(eid,))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/summary/<mes_key>")
def api_summary(mes_key):
    salario = get_salario(mes_key)
    budget  = build_budget(salario, mes_key)
    simples = round(salario * SIMPLES_PCT, 2)
    con  = get_db()
    exps = [dict(r) for r in con.execute("SELECT * FROM expenses WHERE mes_key=?",(mes_key,)).fetchall()]
    con.close()
    by_cat={}; total_saidas=total_entradas=dinheiro_extra=0.0
    for e in exps:
        if e["type"]=="saida":
            by_cat[e["category"]] = by_cat.get(e["category"],0)+e["amount"]
            total_saidas += e["amount"]
        else:
            total_entradas += e["amount"]
            if e["category"]=="💵 Dinheiro Extra":
                dinheiro_extra += e["amount"]
    total_prev = sum(i["valor"] for items in budget.values() for i in items)
    receita    = salario + dinheiro_extra
    return jsonify({
        "mes_key":mes_key,"salario":salario,"simples":simples,
        "total_previsto":total_prev,"saldo_previsto":receita-total_prev,
        "total_saidas":total_saidas,"total_entradas":total_entradas,
        "dinheiro_extra":dinheiro_extra,"receita_total":receita,
        "saldo_real":receita-total_saidas,"by_category":by_cat,
        "budget":budget,"count":len(exps),
    })

# ── CARD ITEMS API ────────────────────────────────────────────────
@app.route("/api/cards/<mes_key>")
def api_list_cards(mes_key):
    con = get_db()
    rows = con.execute("SELECT * FROM card_items WHERE mes_key=? ORDER BY card_name,date,id",(mes_key,)).fetchall()
    con.close()
    result = {}
    for r in rows:
        d = dict(r)
        result.setdefault(d["card_name"],[]).append(d)
    return jsonify(result)

@app.route("/api/cards", methods=["POST"])
def api_add_card_item():
    d = request.json; con = get_db()
    con.execute(
        "INSERT INTO card_items (mes_key,card_name,description,amount,date,category) VALUES (?,?,?,?,?,?)",
        (d["mes_key"],d["card_name"],d["description"],float(d["amount"]),d.get("date",""),d.get("category","Outros")))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/cards/<int:cid>", methods=["PUT"])
def api_update_card_item(cid):
    d = request.json; con = get_db()
    con.execute(
        "UPDATE card_items SET description=?,amount=?,date=?,category=? WHERE id=?",
        (d["description"],float(d["amount"]),d.get("date",""),d.get("category","Outros"),cid))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/cards/<int:cid>", methods=["DELETE"])
def api_delete_card_item(cid):
    con = get_db()
    con.execute("DELETE FROM card_items WHERE id=?",(cid,))
    con.commit(); con.close()
    return jsonify({"ok":True})

# ── INVESTMENTS API ───────────────────────────────────────────────
@app.route("/api/investments/<mes_key>")
def api_list_investments(mes_key):
    con = get_db()
    rows = con.execute("SELECT * FROM investments WHERE mes_key=? ORDER BY name",(mes_key,)).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/investments", methods=["POST"])
def api_add_investment():
    d = request.json; con = get_db()
    con.execute(
        "INSERT INTO investments (mes_key,name,type,balance,aporte,rendimento,institution,notes) VALUES (?,?,?,?,?,?,?,?)",
        (d["mes_key"],d["name"],d["type"],float(d.get("balance",0)),
         float(d.get("aporte",0)),float(d.get("rendimento",0)),
         d.get("institution",""),d.get("notes","")))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/investments/<int:iid>", methods=["PUT"])
def api_update_investment(iid):
    d = request.json; con = get_db()
    con.execute(
        "UPDATE investments SET name=?,type=?,balance=?,aporte=?,rendimento=?,institution=?,notes=? WHERE id=?",
        (d["name"],d["type"],float(d.get("balance",0)),float(d.get("aporte",0)),
         float(d.get("rendimento",0)),d.get("institution",""),d.get("notes",""),iid))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/investments/<int:iid>", methods=["DELETE"])
def api_delete_investment(iid):
    con = get_db()
    con.execute("DELETE FROM investments WHERE id=?",(iid,))
    con.commit(); con.close()
    return jsonify({"ok":True})

# ── LOANS API ─────────────────────────────────────────────────────
@app.route("/api/loans")
def api_list_loans():
    con = get_db()
    rows = con.execute("SELECT * FROM loans WHERE active=1 ORDER BY name").fetchall()
    result = []
    for r in rows:
        loan = dict(r)
        pags = con.execute("SELECT * FROM loan_payments WHERE loan_id=? ORDER BY date",(loan["id"],)).fetchall()
        loan["payments"] = [dict(p) for p in pags]
        result.append(loan)
    con.close()
    return jsonify(result)

@app.route("/api/loans", methods=["POST"])
def api_add_loan():
    d = request.json; con = get_db()
    con.execute(
        "INSERT INTO loans (name,institution,total_amount,remaining_bal,monthly_payment,total_parcelas,parcelas_pagas,due_day,start_date,end_date,interest_rate,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (d["name"],d.get("institution",""),float(d.get("total_amount",0)),
         float(d.get("remaining_bal",0)),float(d.get("monthly_payment",0)),
         int(d.get("total_parcelas",0)),int(d.get("parcelas_pagas",0)),
         int(d.get("due_day",1)),d.get("start_date",""),d.get("end_date",""),
         float(d.get("interest_rate",0)),d.get("notes","")))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/loans/<int:lid>", methods=["PUT"])
def api_update_loan(lid):
    d = request.json; con = get_db()
    con.execute(
        "UPDATE loans SET name=?,institution=?,total_amount=?,remaining_bal=?,monthly_payment=?,total_parcelas=?,parcelas_pagas=?,due_day=?,start_date=?,end_date=?,interest_rate=?,notes=? WHERE id=?",
        (d["name"],d.get("institution",""),float(d.get("total_amount",0)),
         float(d.get("remaining_bal",0)),float(d.get("monthly_payment",0)),
         int(d.get("total_parcelas",0)),int(d.get("parcelas_pagas",0)),
         int(d.get("due_day",1)),d.get("start_date",""),d.get("end_date",""),
         float(d.get("interest_rate",0)),d.get("notes",""),lid))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/loans/<int:lid>", methods=["DELETE"])
def api_delete_loan(lid):
    con = get_db()
    con.execute("UPDATE loans SET active=0 WHERE id=?",(lid,))
    con.commit(); con.close()
    return jsonify({"ok":True})

@app.route("/api/loans/<int:lid>/payment", methods=["POST"])
def api_loan_payment(lid):
    d = request.json; con = get_db()
    con.execute(
        "INSERT INTO loan_payments (loan_id,mes_key,amount,date,notes) VALUES (?,?,?,?,?)",
        (lid,d["mes_key"],float(d["amount"]),d.get("date",""),d.get("notes","")))
    row = con.execute("SELECT COUNT(*) as cnt FROM loan_payments WHERE loan_id=?",(lid,)).fetchone()
    pagas = int(row["cnt"] if hasattr(row,"__getitem__") else row[0])
    con.execute("UPDATE loans SET parcelas_pagas=? WHERE id=?",(pagas,lid))
    con.commit(); con.close()
    return jsonify({"ok":True})

# ── ANNUAL SUMMARY API ────────────────────────────────────────────
@app.route("/api/annual/<int:year>")
def api_annual(year):
    con = get_db()
    months_data = []
    for i, mes_nome in enumerate(MESES):
        mes_key = f"{year}-{i+1:02d}"
        exps = [dict(r) for r in con.execute("SELECT * FROM expenses WHERE mes_key=?",(mes_key,)).fetchall()]
        sal  = get_salario(mes_key)
        total_saidas   = sum(e["amount"] for e in exps if e["type"]=="saida")
        total_entradas = sum(e["amount"] for e in exps if e["type"]=="entrada")
        dinheiro_extra = sum(e["amount"] for e in exps if e["type"]=="entrada" and e["category"]=="💵 Dinheiro Extra")
        receita = sal + dinheiro_extra
        invs = [dict(r) for r in con.execute("SELECT * FROM investments WHERE mes_key=?",(mes_key,)).fetchall()]
        total_invest = sum(i["balance"] for i in invs)
        months_data.append({
            "mes": mes_nome,"mes_key":mes_key,"mes_num":i+1,
            "salario":sal,"receita":receita,"total_saidas":total_saidas,
            "saldo":receita-total_saidas,"total_invest":total_invest,
            "has_data": len(exps) > 0
        })
    loans = [dict(r) for r in con.execute("SELECT * FROM loans WHERE active=1").fetchall()]
    total_debt = sum(l["remaining_bal"] for l in loans)
    con.close()
    return jsonify({"year":year,"months":months_data,"total_debt":total_debt,"loans":loans})

# ── PDF IMPORT ────────────────────────────────────────────────────
def parse_mp_pdf(filepath):
    txs = []; pat = re.compile(r"R\$\s*([-]?\d{1,3}(?:\.\d{3})*,\d{2})")
    with pdfplumber.open(filepath) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    lines = text.split("\n"); i = 0
    while i < len(lines):
        line = lines[i].strip()
        dm   = re.match(r"(\d{2}-\d{2}-\d{4})", line)
        if dm:
            date_fmt = dm.group(1).replace("-","/"); desc_parts=[line[len(dm.group(1)):].strip()]
            j,found = i+1,False
            while j < min(i+5,len(lines)):
                nl = lines[j].strip(); vm = pat.search(nl)
                if vm:
                    amt = float(vm.group(1).replace(".","").replace(",","."))
                    desc = " ".join(desc_parts).strip() or nl.split("R$")[0].strip()
                    if amt != 0:
                        txs.append({"description":desc,"amount":abs(amt),"date":date_fmt,
                                    "category":categorize(desc),"budget_item":"",
                                    "type":"entrada" if amt>0 else "saida","source":"pdf"})
                    found=True; i=j; break
                elif nl and not re.match(r"\d{2}-\d{2}-\d{4}",nl):
                    desc_parts.append(nl)
                j+=1
            if not found: i+=1
        else: i+=1
    return txs

@app.route("/api/import-pdf/<mes_key>", methods=["POST"])
def api_import_pdf(mes_key):
    if "file" not in request.files: return jsonify({"error":"Nenhum arquivo"}),400
    f = request.files["file"]
    if not f.filename.endswith(".pdf"): return jsonify({"error":"Apenas PDF"}),400
    tmp = os.path.join(_DATA_DIR,"tmp.pdf"); f.save(tmp)
    try:    txs = parse_mp_pdf(tmp)
    except Exception as e: return jsonify({"error":str(e)}),500
    finally: os.path.exists(tmp) and os.remove(tmp)
    if not txs: return jsonify({"error":"Nenhuma transação encontrada"}),400
    if request.args.get("preview")=="1":
        return jsonify({"transactions":txs,"count":len(txs)})
    con = get_db()
    for t in txs:
        con.execute(
            "INSERT INTO expenses (mes_key,description,amount,date,category,budget_item,type,source) VALUES (?,?,?,?,?,?,?,?)",
            (mes_key,t["description"],t["amount"],t["date"],t["category"],t["budget_item"],t["type"],"pdf"))
    con.commit(); con.close()
    return jsonify({"ok":True,"inserted":len(txs)})

@app.route("/api/import-confirm/<mes_key>", methods=["POST"])
def api_import_confirm(mes_key):
    data = request.json.get("transactions",[]); con = get_db()
    for t in data:
        con.execute(
            "INSERT INTO expenses (mes_key,description,amount,date,category,budget_item,type,source) VALUES (?,?,?,?,?,?,?,?)",
            (mes_key,t["description"],t["amount"],t["date"],t["category"],t.get("budget_item",""),t["type"],"pdf"))
    con.commit(); con.close()
    return jsonify({"ok":True,"inserted":len(data)})

@app.route("/api/import-pdf-card/<mes_key>", methods=["POST"])
def api_import_pdf_card(mes_key):
    card_name = request.args.get("card","")
    if not card_name: return jsonify({"error":"Informe o cartão"}),400
    if "file" not in request.files: return jsonify({"error":"Nenhum arquivo"}),400
    f = request.files["file"]
    if not f.filename.endswith(".pdf"): return jsonify({"error":"Apenas PDF"}),400
    tmp = os.path.join(_DATA_DIR,"tmp_card.pdf"); f.save(tmp)
    try:    txs = parse_mp_pdf(tmp)
    except Exception as e: return jsonify({"error":str(e)}),500
    finally: os.path.exists(tmp) and os.remove(tmp)
    # For card imports keep only saidas (expenses)
    txs = [t for t in txs if t["type"]=="saida"]
    if not txs: return jsonify({"error":"Nenhuma transação encontrada"}),400
    return jsonify({"transactions":txs,"count":len(txs),"card":card_name})

@app.route("/api/import-confirm-card/<mes_key>", methods=["POST"])
def api_import_confirm_card(mes_key):
    data = request.json; card_name = data.get("card",""); txs = data.get("transactions",[])
    if not card_name: return jsonify({"error":"Informe o cartão"}),400
    con = get_db()
    for t in txs:
        con.execute(
            "INSERT INTO card_items (mes_key,card_name,description,amount,date,category) VALUES (?,?,?,?,?,?)",
            (mes_key,card_name,t["description"],float(t["amount"]),t.get("date",""),t.get("category","Outros")))
    con.commit(); con.close()
    return jsonify({"ok":True,"inserted":len(txs)})


# ── BACKUP / RESTORE ─────────────────────────────────────────────
@app.route("/api/backup")
@login_required
def api_backup():
    """Export everything as JSON for download/backup."""
    con = get_db()
    data = {
        "version": 2,
        "config":    json.loads(open(CFG, encoding="utf-8").read()) if os.path.exists(CFG) else {},
        "expenses":  [dict(r) for r in con.execute("SELECT * FROM expenses").fetchall()],
        "card_items":[dict(r) for r in con.execute("SELECT * FROM card_items").fetchall()],
        "investments":[dict(r) for r in con.execute("SELECT * FROM investments").fetchall()],
        "loans":     [dict(r) for r in con.execute("SELECT * FROM loans").fetchall()],
        "loan_payments":[dict(r) for r in con.execute("SELECT * FROM loan_payments").fetchall()],
    }
    con.close()
    from flask import Response
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=gastos_backup.json"}
    )

@app.route("/api/restore", methods=["POST"])
@login_required
def api_restore():
    """Import a full backup JSON."""
    data = request.json
    if not data or data.get("version") != 2:
        return jsonify({"error": "Arquivo de backup inválido"}), 400
    con = get_db()
    # Clear existing data
    for table in ["expenses","card_items","investments","loan_payments","loans"]:
        con.execute(f"DELETE FROM {table}")
    # Restore
    for e in data.get("expenses", []):
        con.execute("INSERT INTO expenses (id,mes_key,description,amount,date,category,budget_item,type,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (e["id"],e["mes_key"],e["description"],e["amount"],e["date"],e["category"],e.get("budget_item",""),e["type"],e.get("source","manual"),e.get("created_at","")))
    for c in data.get("card_items", []):
        con.execute("INSERT INTO card_items (id,mes_key,card_name,description,amount,date,category,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (c["id"],c["mes_key"],c["card_name"],c["description"],c["amount"],c.get("date",""),c.get("category","Outros"),c.get("created_at","")))
    for i in data.get("investments", []):
        con.execute("INSERT INTO investments (id,mes_key,name,type,balance,aporte,rendimento,institution,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i["id"],i["mes_key"],i["name"],i["type"],i["balance"],i["aporte"],i["rendimento"],i.get("institution",""),i.get("notes",""),i.get("created_at","")))
    for l in data.get("loans", []):
        con.execute("INSERT INTO loans (id,name,institution,total_amount,remaining_bal,monthly_payment,total_parcelas,parcelas_pagas,due_day,start_date,end_date,interest_rate,notes,active,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (l["id"],l["name"],l.get("institution",""),l["total_amount"],l["remaining_bal"],l["monthly_payment"],l["total_parcelas"],l["parcelas_pagas"],l["due_day"],l.get("start_date",""),l.get("end_date",""),l.get("interest_rate",0),l.get("notes",""),l.get("active",1),l.get("created_at","")))
    for p in data.get("loan_payments", []):
        con.execute("INSERT INTO loan_payments (id,loan_id,mes_key,amount,date,notes,created_at) VALUES (?,?,?,?,?,?,?)",
            (p["id"],p["loan_id"],p["mes_key"],p["amount"],p.get("date",""),p.get("notes",""),p.get("created_at","")))
    con.commit(); con.close()
    # Restore config
    if data.get("config"):
        save_config(data["config"])
    return jsonify({"ok": True, "msg": "Backup restaurado com sucesso!"})

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    is_cloud = bool(_IS_CLOUD)
    if not is_cloud:
        import webbrowser,threading
        threading.Timer(1.2,lambda: webbrowser.open(f"http://localhost:{port}")).start()
        print(f"\nGastos App v2 iniciado! -> http://localhost:{port}\n")
    app.run(debug=False,host="0.0.0.0",port=port)
