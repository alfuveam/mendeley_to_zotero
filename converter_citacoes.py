#!/usr/bin/env python3
"""Troca, nos arquivos .tex, as chaves de citação do Mendeley pelas do Zotero.

  \\cite{SilvaNeto2024}  →  \\cite{silva_neto_inteligencia_2024}

Vale para \\cite, \\citeonline, \\citeauthor, \\citeyear, \\citet, \\parencite, \\autocite,
\\nocite etc. As chaves do Zotero são lidas do .bib que o próprio Zotero exporta, então
batem exatamente com o que você vai usar no \\bibliography.

Uso:
  1. No Zotero, exporte a biblioteca (ou a coleção "Mendeley") em BibTeX:
     botão direito → Exportar coleção… → BibTeX. Não marque "Manter atualizado".
  2. Veja o que vai mudar (não altera nada):
       .venv/bin/python converter_citacoes.py --tex pasta/do/latex --bib zotero.bib
  3. Aplique (cria uma cópia .bak de cada .tex alterado):
       .venv/bin/python converter_citacoes.py --tex pasta/do/latex --bib zotero.bib --aplicar
  4. No .tex, aponte \\bibliography{...} para o .bib exportado pelo Zotero.

Sem --bib, o script tenta buscar o BibTeX direto do Zotero aberto. Para isso é preciso
ligar Configurações → Avançado → "Permitir que outros aplicativos neste computador se
comuniquem com o Zotero".

Como cada chave é casada:
  - pela linha "Mendeley-ID" que o mendeley_to_zotero.py grava no Extra (vai para o
    campo note do .bib), ou pelo DOI, ou pelo título;
  - a chave antiga é recalculada no padrão do Mendeley (sobrenome + ano). Se você tiver
    o .bib que o Mendeley exportava, passe com --bib-mendeley: aí as chaves antigas
    saem dele e casam mesmo se o ano/autor tiver mudado depois.
"""

import argparse
import json
import re
import shutil
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

from chaves_de_citacao import CITE_RE, assign_keys
from mendeley_to_zotero import MENDELEY_DIR, load_library

ZOTERO_LOCAL_API = "http://127.0.0.1:23119/api/users/0/items/top"


# --------------------------------------------------------------------------
# BibTeX
# --------------------------------------------------------------------------

def _balanced(text, start):
    """text[start] é '{' ou '"'; devolve (conteúdo, posição depois do fechamento)."""
    if text[start] == '"':
        end = start + 1
        while end < len(text) and not (text[end] == '"' and text[end - 1] != "\\"):
            end += 1
        return text[start + 1:end], end + 1
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{" and text[i - 1] != "\\":
            depth += 1
        elif text[i] == "}" and text[i - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    return text[start + 1:], len(text)


def parse_bib(text):
    """Devolve [{"key", "type", campos...}] com os campos em minúsculas."""
    entries = []
    for m in re.finditer(r"@(\w+)\s*\{", text):
        if m.group(1).lower() in ("comment", "string", "preamble"):
            continue
        body, _ = _balanced(text, m.end() - 1)
        key, _, rest = body.partition(",")
        entry = {"key": key.strip(), "type": m.group(1).lower()}
        pos = 0
        for f in re.finditer(r"(\w[\w-]*)\s*=\s*", rest):
            if f.start() < pos:
                continue
            start = f.end()
            if start < len(rest) and rest[start] in '{"':
                value, pos = _balanced(rest, start)
            else:
                value = re.match(r"[^,]*", rest[start:]).group(0)
                pos = start + len(value)
            entry[f.group(1).lower()] = value.strip()
        if entry["key"]:
            entries.append(entry)
    return entries


def fetch_zotero_bib():
    out, start = [], 0
    while True:
        url = f"{ZOTERO_LOCAL_API}?format=bibtex&limit=100&start={start}"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                total = int(r.headers.get("Total-Results", 0))
                out.append(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            sys.exit(f"A API local do Zotero respondeu {e.code}: {e.read().decode(errors='ignore').strip()}\n"
                     "Ligue em Configurações → Avançado → 'Permitir que outros aplicativos neste computador "
                     "se comuniquem com o Zotero', ou exporte o .bib e use --bib.")
        except urllib.error.URLError:
            sys.exit("Não consegui falar com o Zotero. Abra o Zotero ou exporte o .bib e use --bib.")
        start += 100
        if start >= total:
            return "\n".join(out)


def norm_title(s):
    s = (s or "").replace("{\\i}", "i").replace("\\i", "i")
    s = re.sub(r"\\[a-zA-Z]+\s*", "", s)  # comandos como \textit
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def norm_doi(s):
    s = (s or "").strip().lower()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s)
    return s.replace("\\_", "_")


# --------------------------------------------------------------------------
# Mapeamento chave antiga → chave do Zotero
# --------------------------------------------------------------------------

def mendeley_long_citekey(item):
    """Formato longo do Mendeley: sobrenome + ano + 1ª e última palavra do título,
    com inicial maiúscula, sem acentos, mantendo '-' e ':'.
    Ex.: Song2025AirGPT:Science, Aguiar2023INTELIGENCIADESAFIOS, LoDuca2026Semi-AutomatedChatbot."""
    def ascii_only(s):
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

    creators, fields = item["creators"], item["fields"]
    name = re.sub(r"[^A-Za-z0-9-]", "", ascii_only(creators[0]["lastName"])) if creators else ""
    words = [re.sub(r"[^A-Za-z0-9:-]", "", ascii_only(w)) for w in fields.get("title", "").split()]
    words = [w[:1].upper() + w[1:] for w in words if w]
    return name + fields.get("date", "")[:4] + (words[0] + words[-1] if words else "")


def build_mapping(zotero_entries, mendeley_entries, old_bib_entries):
    by_mid, by_doi, by_title = {}, {}, {}
    for e in zotero_entries:
        m = re.search(r"Mendeley-ID:\s*([0-9a-f-]{36})", e.get("note", ""))
        if m:
            by_mid[m.group(1)] = e["key"]
        if e.get("doi"):
            by_doi.setdefault(norm_doi(e["doi"]), e["key"])
        if e.get("title"):
            by_title.setdefault(norm_title(e["title"]), e["key"])

    def zotero_key(mendeley_id=None, doi=None, title=None):
        if mendeley_id and mendeley_id in by_mid:
            return by_mid[mendeley_id]
        if doi and norm_doi(doi) in by_doi:
            return by_doi[norm_doi(doi)]
        if title and norm_title(title) in by_title:
            return by_title[norm_title(title)]
        return None

    mapping, not_in_zotero = {}, {}
    for doc, item, old_key in mendeley_entries:
        new = zotero_key(doc["id"], item["fields"].get("DOI"), item["fields"].get("title"))
        if new:
            mapping[old_key] = new
            mapping.setdefault(mendeley_long_citekey(item), new)
        else:
            title = item["fields"].get("title", "")
            not_in_zotero[old_key] = not_in_zotero[mendeley_long_citekey(item)] = title
    # As chaves do .bib antigo do Mendeley são as que o .tex realmente usa: têm prioridade.
    for e in old_bib_entries:
        new = zotero_key(None, e.get("doi"), e.get("title"))
        if new:
            mapping[e["key"]] = new
            not_in_zotero.pop(e["key"], None)
        else:
            not_in_zotero[e["key"]] = e.get("title", "")
    return mapping, not_in_zotero


# --------------------------------------------------------------------------
# Reescrita dos .tex
# --------------------------------------------------------------------------

def read_text(path):
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def rewrite(text, mapping):
    changes, unknown = [], set()

    def repl_keys(m):
        keys_text = m.group(1)
        parts = re.split(r"(,)", keys_text)
        for i, part in enumerate(parts):
            key = part.strip()
            if not key or key == ",":
                continue
            if key in mapping and mapping[key] != key:
                parts[i] = part.replace(key, mapping[key])
                changes.append((key, mapping[key]))
            elif key not in mapping.values():
                unknown.add(key)
        whole = m.group(0)
        brace = whole.rindex("{" + keys_text + "}")
        return whole[:brace] + "{" + "".join(parts) + "}"

    # Linhas comentadas (%) também são convertidas: assim um \cite comentado continua
    # válido se for descomentado depois.
    return CITE_RE.sub(repl_keys, text), changes, unknown


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tex", type=Path, required=True, help="pasta do projeto LaTeX (ou um arquivo .tex)")
    ap.add_argument("--bib", type=Path, help=".bib exportado pelo Zotero (sem isso, usa a API local do Zotero)")
    ap.add_argument("--bib-mendeley", type=Path, action="append", default=[],
                    help=".bib antigo exportado pelo Mendeley (pode repetir)")
    ap.add_argument("--mendeley", type=Path, default=MENDELEY_DIR, help="pasta de dados do Mendeley Reference Manager")
    ap.add_argument("--perfil", help="id do perfil do Mendeley (se houver mais de um)")
    ap.add_argument("--sem-mendeley", action="store_true",
                    help="não lê o Mendeley local; usa só o --bib-mendeley para saber as chaves antigas")
    ap.add_argument("--aplicar", action="store_true", help="grava as mudanças (sem isso, só mostra)")
    args = ap.parse_args()

    zotero_text = args.bib.read_text(encoding="utf-8") if args.bib else fetch_zotero_bib()
    zotero_entries = parse_bib(zotero_text)
    if not zotero_entries:
        sys.exit("O .bib do Zotero está vazio.")

    mendeley_entries = []
    if not args.sem_mendeley:
        _, documents, _, _, _ = load_library(args.mendeley, args.perfil)
        mendeley_entries, _ = assign_keys(list(documents.values()))
    old_bib = [e for p in args.bib_mendeley for e in parse_bib(read_text(p)[0])]
    if not mendeley_entries and not old_bib:
        sys.exit("Sem fonte para as chaves antigas: use o Mendeley local ou --bib-mendeley.")

    mapping, not_in_zotero = build_mapping(zotero_entries, mendeley_entries, old_bib)

    tex_files = [args.tex] if args.tex.is_file() else sorted(args.tex.rglob("*.tex"))
    total, all_unknown = 0, set()
    for path in tex_files:
        text, encoding = read_text(path)
        new_text, changes, unknown = rewrite(text, mapping)
        all_unknown |= unknown
        if not changes:
            continue
        total += len(changes)
        print(f"{path}: {len(changes)} citações")
        for old, new in sorted(set(changes)):
            print(f"    {old:32} → {new}")
        if args.aplicar:
            backup = path.with_name(path.name + ".bak")
            if not backup.exists():  # numa 2ª rodada, preserva o original da 1ª
                shutil.copy2(path, backup)
            path.write_text(new_text, encoding=encoding)

    zotero_keys = {e["key"] for e in zotero_entries}
    # "#1", "#2"... são parâmetros de \newcommand, não chaves.
    unresolved = {k for k in all_unknown if k not in zotero_keys and not k.startswith("#")}
    absent = sorted(k for k in unresolved if k in not_in_zotero)
    missing = sorted(k for k in unresolved if k not in not_in_zotero)
    print(f"\nTotal: {total} chaves trocadas em {len(tex_files)} arquivo(s) .tex")
    if absent:
        print("\nReferências do Mendeley que faltam no .bib do Zotero. Coloque-as na coleção exportada,")
        print("exporte de novo e rode outra vez:")
        for key in absent:
            print(f"  - {key}: {not_in_zotero[key][:70]}")
    if missing:
        print("\nChaves citadas que não vêm do Mendeley ou não consegui converter (confira à mão):")
        for key in missing:
            base = re.sub(r"[a-z]?$", "", re.sub(r"\d{4}[a-z]?$", "", key))
            similar = sorted(k for k in mapping if base and k.startswith(base) and k != key)
            hint = f"   (parecida: {', '.join(f'{k} → {mapping[k]}' for k in similar)})" if similar else ""
            print(f"  - {key}{hint}")
    if not args.aplicar:
        print("\nNada foi alterado. Rode de novo com --aplicar para gravar (cria .bak de cada arquivo).")
    else:
        print("\nPronto. Os originais ficaram em *.tex.bak. Lembre de apontar o \\bibliography para o .bib do Zotero.")


if __name__ == "__main__":
    main()
