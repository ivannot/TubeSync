#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, os, sqlite3, argparse, time, csv
from pathlib import Path
from configparser import ConfigParser

SCRIPT_DIR = Path(__file__).resolve().parent

# -------------------------
# Utility path/config
# -------------------------
def resolve_relative(base: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = base / p
    return p.resolve()

def load_cfg(cfg_path: Path) -> ConfigParser:
    cfg = ConfigParser(inline_comment_prefixes=('#',';'))
    if not cfg_path.exists():
        sys.exit(f"[ERRORE] Config non trovato: {cfg_path}")
    cfg.read(cfg_path)
    return cfg

def db_from_arg(arg: str) -> Path:
    """
    Se 'arg' è un .db → usa quello.
    Se 'arg' è un .ini → legge [general] db_path e lo risolve rispetto al config.
    Se 'arg' è una directory → cerca 'state.db' dentro.
    """
    p = Path(arg).expanduser().resolve()
    if p.is_dir():
        cand = p / "state.db"
        if cand.exists():
            return cand
        sys.exit(f"[ERRORE] Nessun 'state.db' in {p}")
    if p.suffix.lower() == ".ini":
        cfg = load_cfg(p)
        db_rel = cfg.get("general", "db_path", fallback="state.db")
        return resolve_relative(p.parent, db_rel)
    return p

# -------------------------
# Stampa tabellare semplice
# -------------------------
def fmt_ts(ts: float) -> str:
    if ts is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return str(ts)

def human_size(b: int) -> str:
    try:
        b = int(b)
    except Exception:
        return ""
    units = ["B","KB","MB","GB","TB"]
    i = 0
    v = float(b)
    while v >= 1024 and i < len(units)-1:
        v /= 1024.0
        i += 1
    return f"{v:.1f}{units[i]}"

def print_table(rows, columns, max_widths=None, truncate_middle_cols=("path", "error")):
    if max_widths is None:
        max_widths = {}
    widths = {c: len(c) for c in columns}
    # pre-format rows for width calc
    formatted = []
    for r in rows:
        fr = {}
        for c in columns:
            val = r.get(c, "")
            s = str(val if val is not None else "")
            if c == "size":
                s = human_size(r.get("size"))
            elif c in ("mtime", "created_at", "updated_at"):
                s = fmt_ts(r.get(c))
            mw = max_widths.get(c)
            if mw and len(s) > mw:
                if c in truncate_middle_cols and mw > 6:
                    keep = mw - 3
                    left = keep // 2
                    right = keep - left
                    s = s[:left] + "…" + s[-right:]
                else:
                    s = s[:mw-1] + "…"
            widths[c] = max(widths[c], len(s))
            fr[c] = s
        formatted.append(fr)
    # header + rows
    line = " | ".join(h.ljust(widths[h]) for h in columns)
    sep  = "-+-".join("-" * widths[h] for h in columns)
    print(line)
    print(sep)
    for fr in formatted:
        print(" | ".join(fr.get(c,"").ljust(widths[c]) for c in columns))

# -------------------------
# Query
# -------------------------
def build_query(status, since_days, order_by, limit):
    q = "SELECT id, path, size, mtime, sha1, status, video_id, error, created_at, updated_at FROM uploads"
    clauses = []
    params = []
    if status and status.lower() != "all":
        clauses.append("status = ?")
        params.append(status.lower())
    if since_days is not None:
        try:
            since_days = int(since_days)
            since_ts = time.time() - since_days * 86400
            clauses.append("updated_at >= ?")
            params.append(since_ts)
        except Exception:
            pass
    if clauses:
        q += " WHERE " + " AND ".join(clauses)

    # order by
    if order_by == "updated_desc":
        q += " ORDER BY updated_at DESC"
    elif order_by == "updated_asc":
        q += " ORDER BY updated_at ASC"
    elif order_by == "created_desc":
        q += " ORDER BY created_at DESC"
    elif order_by == "created_asc":
        q += " ORDER BY created_at ASC"
    else:
        q += " ORDER BY id ASC"

    if limit:
        q += " LIMIT ?"
        params.append(int(limit))
    return q, params

# -------------------------
# CSV export
# -------------------------
def to_csv(rows, columns, csv_path: Path, delimiter: str=",", include_header: bool=True):
    csv_path = Path(csv_path).expanduser().resolve()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        if include_header:
            w.writerow(columns)
        for r in rows:
            out_row = []
            for c in columns:
                v = r.get(c, "")
                # non formattiamo (manteniamo i valori raw dal DB),
                # ma i timestamp sono float → convertiamoli a ISO leggibile
                if c in ("mtime","created_at","updated_at"):
                    v = fmt_ts(v) if v not in (None,"") else ""
                out_row.append("" if v is None else v)
            w.writerow(out_row)

# -------------------------
# Main
# -------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="tubesync_list.py",
        description=(
            "Elenca i record di uploads in state.db e opzionalmente esporta in CSV.\n\n"
            "Puoi passare come primo argomento:\n"
            " - il path a state.db, oppure\n"
            " - il path a config.ini (verrà letto [general] db_path), oppure\n"
            " - una cartella che contiene state.db\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("db_or_config",
                        help="Path a state.db, oppure a config.ini, oppure a una cartella con state.db")
    parser.add_argument("status", nargs="?", default="all",
                        choices=["all","done","pending","error"],
                        help="Filtro per status (default: all)")
    parser.add_argument("--columns", default="id,path,status,video_id,updated_at",
                        help=("Colonne da mostrare/esportare, separate da virgola.\n"
                              "Disponibili: id,path,size,mtime,sha1,status,video_id,error,created_at,updated_at\n"
                              "Default: id,path,status,video_id,updated_at"))
    parser.add_argument("--since-days", type=int, default=None,
                        help="Mostra solo record aggiornati negli ultimi N giorni")
    parser.add_argument("--order", default="updated_desc",
                        choices=["id_asc","updated_desc","updated_asc","created_desc","created_asc"],
                        help="Ordinamento (default: updated_desc)")
    parser.add_argument("--limit", type=int, default=None, help="Limita il numero di righe")
    parser.add_argument("--max-widths", default="path:100,error:120",
                        help="Larghezze massime per colonne a schermo (es: path:100,error:120)")
    # CSV options
    parser.add_argument("--csv", dest="csv_path", default=None, help="Percorso file CSV da generare")
    parser.add_argument("--csv-delim", default=",", help="Delimitatore CSV (default: ',')")
    parser.add_argument("--csv-no-header", action="truediv", help=argparse.SUPPRESS)  # legacy guard
    parser.add_argument("--no-header", action="store_true", help="CSV senza header (solo righe dati)")
    args = parser.parse_args()

    db_path = db_from_arg(args.db_or_config)
    if not db_path.exists():
        sys.exit(f"[ERRORE] Database non trovato: {db_path}")

    # colonne richieste
    columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    valid_cols = {"id","path","size","mtime","sha1","status","video_id","error","created_at","updated_at"}
    for c in columns:
        if c not in valid_cols:
            sys.exit(f"[ERRORE] Colonna non valida: {c}\n   Valide: {', '.join(sorted(valid_cols))}")

    # parsing max widths per output tabellare
    max_widths = {}
    if args.max_widths:
        for part in args.max_widths.split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            k, v = part.split(":", 1)
            try:
                max_widths[k.strip()] = int(v.strip())
            except Exception:
                pass

    # connessione
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    # summary per status
    tot = {}
    for st in ("done","pending","error"):
        cur = con.execute("SELECT COUNT(*) as c FROM uploads WHERE status = ?", (st,))
        tot[st] = cur.fetchone()["c"]

    # query principale
    q, params = build_query(args.status, args.since_days, args.order, args.limit)
    cur = con.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    con.close()

    # stampa tabella (solo se non stiamo facendo solo export)
    if args.csv_path is None:
        if not rows:
            print(f"(Nessun record - db: {db_path})")
        else:
            print(f"Database: {db_path}")
            print_table(rows, columns, max_widths=max_widths)
            print()
        print(f"Summary → done: {tot.get('done',0)}, pending: {tot.get('pending',0)}, error: {tot.get('error',0)}")
    else:
        # export CSV
        to_csv(rows, columns, args.csv_path, delimiter=args.csv_delim, include_header=(not args.no_header))
        print(f"[OK] Esportato CSV: {Path(args.csv_path).resolve()} ({len(rows)} righe)")

if __name__ == "__main__":
    main()
