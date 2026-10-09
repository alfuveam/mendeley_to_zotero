#!/usr/bin/env python3
"""Mantém no Zotero as mesmas chaves de citação (BibTeX) que o Mendeley usava.

O Mendeley gera chaves como "Giray2023"; o Zotero gera "giray_prompt_2023".
Este script calcula a chave no padrão do Mendeley para cada referência e gera um
JavaScript que grava a linha "Citation Key: ..." no campo Extra dos itens do Zotero.
O exportador BibTeX do Zotero (e o Better BibTeX) usam essa linha como chave, então os
\\cite{...} do seu LaTeX continuam funcionando.

Uso:
  .venv/bin/python chaves_de_citacao.py [--tex PASTA_DO_LATEX]

  Depois, no Zotero: Ferramentas → Desenvolvedor → Executar JavaScript, marque
  "Executar como função assíncrona", cole saida/definir_chaves_no_zotero.js e Execute.

Com --tex, o script também lê os \\cite{...} dos .tex e avisa quais chaves citadas
não batem com nenhuma referência do Mendeley.
"""

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from mendeley_to_zotero import MENDELEY_DIR, build_item, load_library

CITE_RE = re.compile(r"\\[A-Za-z]*cite[A-Za-z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}")


def mendeley_citekey(item):
    """Sobrenome do 1º autor sem espaços nem acentos + ano.
    "Silva Neto" → SilvaNeto2024, "Sandmæl" → Sandml2023, o hífen é mantido."""
    creators, fields = item["creators"], item["fields"]
    base = creators[0]["lastName"] if creators else (fields.get("title", "").split() or ["ref"])[0]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9-]", "", base) or "ref"
    return base + fields.get("date", "")[:4]


def assign_keys(documents):
    """Devolve [(doc, item, chave)] e os grupos de chaves repetidas.

    Repetidas ganham sufixo a, b, ... como no Mendeley. A ordem em que o Mendeley
    distribui os sufixos não é conhecida; aqui segue a data de inclusão."""
    entries = []
    for doc in documents:
        item = build_item(doc, set())
        entries.append([doc, item, doc.get("citation_key") or mendeley_citekey(item)])

    groups = defaultdict(list)
    for entry in entries:
        if not entry[0].get("citation_key"):
            groups[entry[2]].append(entry)
    duplicated = []
    for key, group in groups.items():
        if len(group) > 1:
            group.sort(key=lambda e: e[0].get("created", ""))
            for n, entry in enumerate(group[1:], start=1):
                entry[2] = key + chr(96 + n)
            duplicated.append(group)
    return entries, duplicated


def cited_keys(tex_dir):
    keys = set()
    for path in Path(tex_dir).rglob("*.tex"):
        for m in CITE_RE.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            keys.update(k.strip() for k in m.group(1).split(",") if k.strip())
    return keys


JS_TEMPLATE = r"""// Gerado por chaves_de_citacao.py
// Zotero 7+: Ferramentas → Desenvolvedor → Executar JavaScript,
// marque "Executar como função assíncrona", cole TUDO e clique em Executar.
// Grava "Citation Key: ..." no Extra; pode rodar de novo sem problema.

const KEYS = __DATA__;

const libraryID = Zotero.Libraries.userLibraryID;
let updated = 0, unchanged = 0;
const notFound = [];

async function find(cond, value) {
	let s = new Zotero.Search();
	s.libraryID = libraryID;
	s.addCondition(cond, cond === 'extra' ? 'contains' : 'is', value);
	s.addCondition('deleted', 'false');
	return (await s.search()).map(id => Zotero.Items.get(id)).filter(i => i.isRegularItem());
}

for (let k of KEYS) {
	// Itens importados pelo mendeley_to_zotero.py têm "Mendeley-ID" no Extra;
	// os importados de outro jeito são achados pelo DOI ou pelo título exato.
	let items = await find('extra', 'Mendeley-ID: ' + k.mendeleyId);
	if (!items.length && k.doi) items = await find('DOI', k.doi);
	if (!items.length && k.title) items = await find('title', k.title);
	if (items.length !== 1) {
		notFound.push(`${k.key} (${items.length ? items.length + ' itens com o mesmo título' : 'não encontrado'}): ${k.title}`);
		continue;
	}
	let item = items[0];
	let lines = (item.getField('extra') || '').split('\n').filter(l => !/^\s*Citation Key\s*:/i.test(l) && l.trim());
	let extra = ['Citation Key: ' + k.key, ...lines].join('\n');
	if (extra === item.getField('extra')) { unchanged++; continue; }
	item.setField('extra', extra);
	await item.saveTx();
	updated++;
}

return `Chaves gravadas: ${updated} | já estavam certas: ${unchanged} | sem correspondência: ${notFound.length}`
	+ (notFound.length ? '\n\n' + notFound.join('\n') : '');
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mendeley", type=Path, default=MENDELEY_DIR, help="pasta de dados do Mendeley Reference Manager")
    ap.add_argument("--saida", type=Path, default=Path(__file__).resolve().parent / "saida")
    ap.add_argument("--perfil", help="id do perfil do Mendeley (se houver mais de um)")
    ap.add_argument("--tex", type=Path, help="pasta do projeto LaTeX, para conferir os \\cite{...}")
    ap.add_argument("--incluir-lixeira", action="store_true", help="inclui os itens da lixeira do Mendeley")
    args = ap.parse_args()

    _, documents, _, _, _ = load_library(args.mendeley, args.perfil)
    docs = [d for d in documents.values() if args.incluir_lixeira or not d.get("isTrashed")]
    entries, duplicated = assign_keys(docs)

    data = [{"mendeleyId": doc["id"], "key": key, "title": item["fields"].get("title", ""),
             "doi": item["fields"].get("DOI", "")} for doc, item, key in entries]
    args.saida.mkdir(parents=True, exist_ok=True)
    js = args.saida / "definir_chaves_no_zotero.js"
    js.write_text(JS_TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)), encoding="utf-8")

    print(f"Chaves geradas: {len(entries)}")
    for doc, item, key in sorted(entries, key=lambda e: e[2].lower()):
        print(f"  {key:32} {item['fields'].get('title', '')[:70]}")

    if duplicated:
        print("\nChaves repetidas: o Mendeley põe sufixo a, b... e a ordem pode não ser a mesma daqui.")
        print("Se o seu .tex usa essas chaves, confira se cada uma aponta para a referência certa:")
        for group in duplicated:
            for doc, item, key in group:
                print(f"  - {key}: {item['fields'].get('title', '')[:70]}")

    if args.tex:
        cited = cited_keys(args.tex)
        known = {key for _, _, key in entries}
        missing = sorted(cited - known)
        print(f"\n\\cite no LaTeX: {len(cited)} chaves, {len(cited) - len(missing)} encontradas no Mendeley")
        if missing:
            print("Citadas mas sem referência com essa chave (corrija no .tex ou no Zotero):")
            for key in missing:
                print(f"  - {key}")

    print(f"\nGerado {js}")
    print("Zotero → Ferramentas → Desenvolvedor → Executar JavaScript, marque 'Executar como função\n"
          "assíncrona', cole o arquivo e Execute. Depois exporte o .bib de novo.")


if __name__ == "__main__":
    main()
