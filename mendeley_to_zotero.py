#!/usr/bin/env python3
"""Exporta a biblioteca do Mendeley Reference Manager (desktop) para o Zotero.

Lê direto do cache local do Mendeley (IndexedDB do Electron em
~/.config/Mendeley Reference Manager), sem precisar de API nem login, e gera em
--saida:

  pdfs/                   cópia dos PDFs originais, com o nome de arquivo do Mendeley
  pdfs_anotados/          os mesmos PDFs com os highlights/notas gravados dentro
                          (abrem anotados em qualquer leitor de PDF)
  importar_no_zotero.js   script para o Zotero: cria itens, coleções, anexa os PDFs
                          e recria os highlights como anotações NATIVAS do Zotero
                          (editáveis, com cor e texto)
  biblioteca.bib          alternativa simples: BibTeX apontando para pdfs_anotados/
  dados.json              tudo o que foi extraído, para conferência

Uso:
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python mendeley_to_zotero.py

  Depois, no Zotero 7: Ferramentas → Desenvolvedor → Executar JavaScript, marque
  "Executar como função assíncrona", cole o conteúdo de importar_no_zotero.js e
  clique em Executar. Rodar de novo não duplica (os itens levam "Mendeley-ID" no Extra).
"""

import argparse
import colorsys
import json
import re
import shutil
import sys
import tempfile
import unicodedata
from collections import defaultdict
from pathlib import Path

import pymupdf
from ccl_chromium_reader import ccl_chromium_indexeddb as idb

MENDELEY_DIR = Path.home() / ".config" / "Mendeley Reference Manager"

ITEM_TYPES = {
    "journal": "journalArticle",
    "book": "book",
    "book_section": "bookSection",
    "conference_proceedings": "conferencePaper",
    "thesis": "thesis",
    "report": "report",
    "web_page": "webpage",
    "generic": "document",
    "patent": "patent",
    "magazine_article": "magazineArticle",
    "newspaper_article": "newspaperArticle",
    "working_paper": "preprint",
    "computer_program": "computerProgram",
    "film": "film",
    "television_broadcast": "tvBroadcast",
    "hearing": "hearing",
    "bill": "bill",
    "case": "case",
    "statute": "statute",
    "encyclopedia_article": "encyclopediaArticle",
    "dataset": "dataset",
}

BIBTEX_TYPES = {
    "journalArticle": "article",
    "book": "book",
    "bookSection": "incollection",
    "conferencePaper": "inproceedings",
    "thesis": "phdthesis",
    "report": "techreport",
    "webpage": "online",
}

# Paleta de anotação do Zotero: (hex, matiz em graus)
ZOTERO_COLORS = [
    ("#ff6666", 0), ("#f19837", 31), ("#ffd400", 50), ("#5fb236", 98),
    ("#2ea8e5", 199), ("#a28ae5", 255), ("#e56eee", 296),
]
ZOTERO_GRAY = "#aaaaaa"

LATEX_ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308",
                 "~": "\u0303", "c": "\u0327", "=": "\u0304", ".": "\u0307"}


# --------------------------------------------------------------------------
# Leitura do IndexedDB do Mendeley
# --------------------------------------------------------------------------

def decode_blob(path):
    """Valor serializado pelo Blink/V8 guardado fora do LevelDB (registros grandes).

    O localforage do Mendeley grava strings JSON, então só tratamos strings."""
    b = path.read_bytes()
    i = 0
    while i < len(b) and b[i] == 0xFF:  # envelope do Blink (0xFF ver) e header do V8 (0xFF ver)
        i += 1
        while b[i] & 0x80:
            i += 1
        i += 1
    while b[i] == 0x00:  # padding
        i += 1
    tag = b[i]
    i += 1
    n = shift = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if c < 0x80:
            break
    raw = b[i:i + n]
    if tag == 0x63:  # 'c' string two-byte
        text = raw.decode("utf-16-le")
    elif tag == 0x22:  # '"' string one-byte
        text = raw.decode("latin-1")
    elif tag == 0x53:  # 'S' string utf-8
        text = raw.decode("utf-8")
    else:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def classify_blob(value):
    if isinstance(value, list) and value and isinstance(value[0], dict) and "_custom" in value[0]:
        return "annotationsV2"
    if isinstance(value, dict) and value:
        first = next(iter(value.values()))
        if isinstance(first, dict) and "title" in first and "profile_id" in first:
            return "documents"
        if isinstance(first, list) and first and isinstance(first[0], dict) and "positions" in first[0]:
            return "highlights"
    return None


def read_localforage(mendeley_dir):
    """Devolve ({chave: valor} dos registros inline, {tipo: [valores]} dos blobs)."""
    src = mendeley_dir / "IndexedDB"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # O Mendeley pode estar aberto: trabalha numa cópia, sem o LOCK.
        for name in ("file__0.indexeddb.leveldb", "file__0.indexeddb.blob"):
            if (src / name).exists():
                shutil.copytree(src / name, tmp / name, ignore=shutil.ignore_patterns("LOCK"))
        leveldb, blobdir = tmp / "file__0.indexeddb.leveldb", tmp / "file__0.indexeddb.blob"
        if not leveldb.exists():
            sys.exit(f"Não achei o IndexedDB do Mendeley em {src}")

        inline = {}
        wrapped = idb.WrappedIndexDB(leveldb, blobdir if blobdir.exists() else None)
        db = wrapped["localforage", "file__0@1"]
        latest = {}
        # Registros grandes apontam para blobs; o índice blob→arquivo do LevelDB
        # costuma estar defasado, então eles são lidos abaixo, direto dos arquivos.
        for rec in db["keyvaluepairs"].iterate_records(bad_deserializer_data_handler=lambda k, b: None):
            key = rec.key.value
            if key not in latest or rec.ldb_seq_no > latest[key].ldb_seq_no:
                latest[key] = rec
        for key, rec in latest.items():
            if rec.is_live and isinstance(rec.value, str):
                try:
                    inline[key] = json.loads(rec.value)
                except ValueError:
                    pass

        blobs = defaultdict(list)
        if blobdir.exists():
            # O Chromium apaga blobs antigos; os que existem são os atuais.
            for path in blobdir.rglob("*"):
                if path.is_file():
                    try:
                        value = decode_blob(path)
                    except (IndexError, UnicodeDecodeError):
                        continue
                    kind = classify_blob(value)
                    if kind:
                        blobs[kind].append(value)
    return inline, blobs


def load_library(mendeley_dir, profile_id=None):
    inline, blobs = read_localforage(mendeley_dir)

    profiles = {k.split(":")[0] for k in inline if k.endswith(":profile")}
    if profile_id is None:
        if not profiles:
            sys.exit("Nenhum perfil do Mendeley encontrado (abra o Mendeley e faça login/sincronize uma vez).")
        if len(profiles) > 1:
            def last_sync(p):
                return json.dumps(inline.get(f"{p}:lastSync", ""))
            profile_id = max(profiles, key=last_sync)
            print(f"Vários perfis encontrados {sorted(profiles)}; usando {profile_id} (use --perfil para escolher)")
        else:
            profile_id = profiles.pop()

    def get(key, kind=None):
        if f"{profile_id}:{key}" in inline:
            return inline[f"{profile_id}:{key}"]
        return blobs.get(kind or key, [])

    documents = {}
    for value in ([get("documents")] if isinstance(get("documents"), dict) else get("documents")):
        documents.update({k: d for k, d in value.items() if d.get("profile_id", profile_id) == profile_id})
    if not documents:
        sys.exit("Não encontrei os documentos no cache local. Abra o Mendeley, deixe sincronizar e rode de novo.")

    files = get("files") or {}

    annotations = {}
    v2 = get("annotationsV2")
    for value in ([v2] if v2 and isinstance(v2[0], dict) else v2):
        for a in value:
            c = a["_custom"]
            if c.get("profileId", profile_id) != profile_id:
                continue
            annotations[c["id"]] = {
                "id": c["id"],
                "type": c.get("type", "highlight"),
                "file_id": c.get("fileId"),
                "filehash": c.get("filehash"),
                "document_id": c.get("documentId"),
                "positions": c.get("positions") or [],
                "rgb": _rgb_from_css(json.dumps(a.get("stylesheet", ""))) or _rgb_from_name(c.get("color", "")),
                "text": " ".join(b.get("value", "") for b in a.get("body", []) if isinstance(b, dict)).strip()
                        or c.get("text", ""),
                "created": a.get("created"),
                "modified": a.get("modified"),
            }
    hl = get("highlights")
    for value in ([hl] if isinstance(hl, dict) else hl):
        for items in value.values():
            for h in items:
                if h.get("profile_id", profile_id) != profile_id or h["id"] in annotations:
                    continue
                col = h.get("color") or {}
                annotations[h["id"]] = {
                    "id": h["id"],
                    "type": h.get("type", "highlight"),
                    "file_id": h.get("file_id"),
                    "filehash": h.get("filehash"),
                    "document_id": h.get("document_id"),
                    "positions": h.get("positions") or [],
                    "rgb": (col.get("r"), col.get("g"), col.get("b")) if col else None,
                    "text": h.get("text", ""),
                    "created": h.get("created"),
                    "modified": h.get("last_modified"),
                }

    collections = (inline.get(f"{profile_id}:collections") or {}).get("user", [])
    return profile_id, documents, files, list(annotations.values()), collections


def _rgb_from_css(css):
    m = re.search(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", css)
    return tuple(int(x) for x in m.groups()) if m else None


def _rgb_from_name(name):
    for key, rgb in (("yellow", (255, 245, 173)), ("green", (220, 255, 176)), ("blue", (186, 226, 255)),
                     ("red", (255, 181, 182)), ("purple", (211, 194, 255)), ("orange", (255, 200, 150)),
                     ("grey", (200, 200, 200)), ("gray", (200, 200, 200))):
        if key in name:
            return rgb
    return (255, 245, 173)


def zotero_color(rgb):
    """Cores do Mendeley são pastel; mapeia pela matiz para a paleta do Zotero."""
    if not rgb or None in rgb:
        return "#ffd400"
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    if s < 0.12:
        return ZOTERO_GRAY
    hue = h * 360
    return min(ZOTERO_COLORS, key=lambda c: min(abs(hue - c[1]), 360 - abs(hue - c[1])))[0]


# --------------------------------------------------------------------------
# Metadados
# --------------------------------------------------------------------------

def unlatex(s):
    if not isinstance(s, str):
        return s
    s = s.replace("{\\i}", "i").replace("\\i ", "i").replace("\\i", "i")
    s = re.sub(r"\\([`'^\"~c=.])\s*\{?\\?([A-Za-z])\}?",
               lambda m: unicodedata.normalize("NFC", m.group(2) + LATEX_ACCENTS[m.group(1)]), s)
    s = re.sub(r"\\([&%$#_])", r"\1", s)
    return s


def clean(s, keep_paragraphs=False):
    s = unlatex(s)
    if not isinstance(s, str):
        return s
    if keep_paragraphs:
        paragraphs = re.split(r"\n\s*\n", s)
        return "\n\n".join(re.sub(r"\s+", " ", p).strip() for p in paragraphs).strip()
    return re.sub(r"\s+", " ", s).strip()


def build_item(doc, collection_ids):
    item_type = ITEM_TYPES.get(doc.get("type"), "document")
    ids = doc.get("identifiers") or {}

    date = ""
    if doc.get("year"):
        date = str(doc["year"])
        if doc.get("month"):
            date += f"-{int(doc['month']):02d}"
            if doc.get("day"):
                date += f"-{int(doc['day']):02d}"

    fields = {
        "title": clean(doc.get("title")),
        "publicationTitle": clean(doc.get("source")),
        "publisher": clean(doc.get("publisher") or doc.get("institution")),
        "place": clean(doc.get("city")),
        "volume": doc.get("volume"),
        "issue": doc.get("issue"),
        "pages": doc.get("pages"),
        "edition": doc.get("edition"),
        "series": clean(doc.get("series")),
        "type": clean(doc.get("genre")),
        "date": date,
        "abstractNote": clean(doc.get("abstract"), keep_paragraphs=True),
        "language": doc.get("language"),
        "url": (doc.get("websites") or [None])[0],
        "accessDate": doc.get("accessed"),
        "DOI": ids.get("doi"),
        "ISBN": ids.get("isbn"),
        "ISSN": ids.get("issn"),
    }
    fields = {k: str(v) for k, v in fields.items() if v not in (None, "")}

    extra = [f"Mendeley-ID: {doc['id']}"]
    if ids.get("pmid"):
        extra.append(f"PMID: {ids['pmid']}")
    if ids.get("arxiv"):
        extra.append(f"arXiv: {ids['arxiv']}")

    creators = []
    for role, people in (("author", doc.get("authors")), ("editor", doc.get("editors"))):
        for p in people or []:
            first, last = clean(p.get("first_name", "")), clean(p.get("last_name", ""))
            if last or first:
                creators.append({"firstName": first, "lastName": last, "creatorType": role})

    tags = [{"tag": clean(t), "type": 0} for t in doc.get("tags") or [] if t]
    tags += [{"tag": clean(t).rstrip("."), "type": 1} for t in doc.get("keywords") or [] if t]
    if doc.get("starred"):
        tags.append({"tag": "Mendeley: favorito", "type": 0})

    return {
        "mendeleyId": doc["id"],
        "itemType": item_type,
        "fields": fields,
        "extra": "\n".join(extra),
        "creators": creators,
        "tags": tags,
        "notes": [doc["notes"]] if (doc.get("notes") or "").strip() else [],
        "collections": [c for c in doc.get("folder_uuids") or [] if c in collection_ids],
        "dateAdded": doc.get("created"),
    }


# --------------------------------------------------------------------------
# PDFs e anotações
# --------------------------------------------------------------------------

def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip() or "arquivo.pdf"
    stem, dot, ext = name.rpartition(".")
    if not dot or ext.lower() != "pdf":
        stem, ext = name, "pdf"
    return f"{stem[:120]}.{ext}"


def unique_path(folder, name):
    path = folder / name
    n = 2
    while path.exists():
        path = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return path


def process_pdf(src, dst_original, dst_annotated, annotations):
    """Copia o PDF, grava as anotações numa segunda cópia e devolve as anotações
    no formato do Zotero (coordenadas PDF, origem embaixo-esquerda, igual ao Mendeley)."""
    shutil.copy2(src, dst_original)
    zotero_annots, warnings = [], []
    try:
        pdf = pymupdf.open(src)
    except Exception as e:  # noqa: BLE001 - PDF corrompido não deve parar a exportação
        shutil.copy2(src, dst_annotated)
        return [], [f"não abriu o PDF ({e}); anotações não gravadas"]

    for a in sorted(annotations, key=lambda a: (min((p["page"] for p in a["positions"]), default=0), a["created"] or "")):
        by_page = defaultdict(list)
        for p in a["positions"]:
            by_page[p["page"]].append(p)
        if not by_page:
            continue
        rgb01 = tuple(c / 255 for c in (a["rgb"] or (255, 245, 173)))
        color = zotero_color(a["rgb"])
        for page_no, positions in sorted(by_page.items()):
            if not 1 <= page_no <= pdf.page_count:
                warnings.append(f"anotação {a['id']} aponta para a página {page_no}, que não existe")
                continue
            page = pdf[page_no - 1]
            m = page.transformation_matrix
            label = page.get_label() or str(page_no)
            is_note = a["type"] != "highlight"
            pdf_rects, mu_rects = [], []
            for p in positions:
                tl, br = p["top_left"], p.get("bottom_right") or p["top_left"]
                x0, x1 = sorted((tl["x"], br["x"]))
                y0, y1 = sorted((tl["y"], br["y"]))
                if is_note:
                    x1, y0 = x0 + 22, y1 - 22
                pdf_rects.append([round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)])
                mu_rects.append(pymupdf.Rect(x0, y0, x1, y1) * m)

            text = ""
            if is_note:
                annot = page.add_text_annot(mu_rects[0].tl, a["text"] or "", icon="Note")
            else:
                text = clean(" ".join(page.get_textbox(r) for r in mu_rects))
                text = re.sub(r"(\w)- (\w)", r"\1\2", text)  # hifenização de fim de linha
                annot = page.add_highlight_annot(quads=[r.quad for r in mu_rects])
                if a["text"]:
                    annot.set_info(content=a["text"])
            annot.set_colors(stroke=rgb01)
            annot.set_info(title="Mendeley")
            annot.update()

            top = int(max(0, min(99999, min(r.y0 for r in mu_rects))))
            zotero_annots.append({
                "type": "note" if is_note else "highlight",
                "color": color,
                "pageIndex": page_no - 1,
                "pageLabel": label,
                "rects": pdf_rects[:1] if is_note else pdf_rects,
                "sortIndex": f"{page_no - 1:05d}|{0:06d}|{top:05d}",
                "text": text,
                "comment": a["text"] or "",
                "dateAdded": a["created"],
            })
    try:
        pdf.save(dst_annotated, garbage=1, deflate=True)
    except Exception as e:  # noqa: BLE001
        shutil.copy2(src, dst_annotated)
        warnings.append(f"não consegui salvar o PDF anotado ({e}); copiado sem anotações")
    pdf.close()
    return zotero_annots, warnings


# --------------------------------------------------------------------------
# Saídas
# --------------------------------------------------------------------------

def bibtex(items):
    def esc(v):
        return re.sub(r"([&%$#_])", r"\\\1", str(v)).replace("\n", " ")

    fieldmap = {"title": "title", "date": "date", "volume": "volume", "issue": "number", "pages": "pages",
                "publisher": "publisher", "place": "address", "DOI": "doi", "ISBN": "isbn", "ISSN": "issn",
                "url": "url", "abstractNote": "abstract", "language": "language", "edition": "edition",
                "series": "series", "accessDate": "urldate"}
    containers = {"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle"}
    out, used = [], set()
    for it in items:
        f = it["fields"]
        btype = BIBTEX_TYPES.get(it["itemType"], "misc")
        first = (it["creators"][0]["lastName"] if it["creators"] else "anon")
        base = re.sub(r"\W", "", unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode()) or "ref"
        key = f"{base}{f.get('date', '')[:4]}"
        n = 0
        while key in used:
            n += 1
            key = f"{base}{f.get('date', '')[:4]}{chr(96 + n)}"
        used.add(key)
        lines = [f"@{btype}{{{key},"]
        for role, bibrole in (("author", "author"), ("editor", "editor")):
            names = [f"{c['lastName']}, {c['firstName']}".strip(", ") for c in it["creators"] if c["creatorType"] == role]
            if names:
                lines.append(f"  {bibrole} = {{{esc(' and '.join(names))}}},")
        for zf, bf in fieldmap.items():
            if f.get(zf):
                lines.append(f"  {bf} = {{{esc(f[zf])}}},")
        if f.get("publicationTitle"):
            lines.append(f"  {containers.get(btype, 'howpublished')} = {{{esc(f['publicationTitle'])}}},")
        if it["tags"]:
            lines.append(f"  keywords = {{{esc(', '.join(t['tag'] for t in it['tags']))}}},")
        lines.append(f"  note = {{{esc(it['extra'])}}},")
        files = [a["annotatedPath"] for a in it["attachments"]]
        if files:
            lines.append("  file = {" + ";".join(f"PDF:{p}:application/pdf" for p in files) + "},")
        lines.append("}")
        out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


JS_TEMPLATE = r"""// Gerado por mendeley_to_zotero.py
// Zotero 7: Ferramentas → Desenvolvedor → Executar JavaScript,
// marque "Executar como função assíncrona", cole TUDO e clique em Executar.
// Pode rodar de novo: itens que já têm o mesmo "Mendeley-ID" no Extra são pulados.

const DATA = __DATA__;

const libraryID = Zotero.Libraries.userLibraryID;
const log = [];
let created = 0, skipped = 0, pdfs = 0, annots = 0;

function setF(item, field, value) {
	if (value === undefined || value === null || value === '') return false;
	let fieldID = Zotero.ItemFields.getID(field);
	if (!fieldID) return false;
	let mapped = Zotero.ItemFields.getFieldIDFromTypeAndBase(item.itemTypeID, fieldID);
	if (mapped) fieldID = mapped;
	if (!Zotero.ItemFields.isValidForType(fieldID, item.itemTypeID)) return false;
	item.setField(fieldID, String(value));
	return true;
}

function sqlDate(iso) {
	try { return iso ? Zotero.Date.dateToSQL(new Date(iso), true) : null; } catch (e) { return null; }
}

async function getCollection(name, parentID) {
	let found = Zotero.Collections.getByLibrary(libraryID, true)
		.find(c => c.name === name && (c.parentID || null) === (parentID || null));
	if (found) return found;
	let c = new Zotero.Collection();
	c.libraryID = libraryID;
	c.name = name;
	if (parentID) c.parentID = parentID;
	await c.saveTx();
	return c;
}

// Coleções (respeitando a hierarquia do Mendeley)
const colIDs = {};
const rootCol = DATA.rootCollection ? await getCollection(DATA.rootCollection, null) : null;
async function ensureCol(id) {
	if (colIDs[id]) return colIDs[id];
	let mc = DATA.collections.find(c => c.id === id);
	if (!mc) return null;
	let parent = mc.parentId ? await ensureCol(mc.parentId) : (rootCol ? rootCol.id : null);
	let c = await getCollection(mc.name, parent);
	return (colIDs[id] = c.id);
}
for (let mc of DATA.collections) await ensureCol(mc.id);

for (let doc of DATA.items) {
	try {
		let s = new Zotero.Search();
		s.libraryID = libraryID;
		s.addCondition('extra', 'contains', 'Mendeley-ID: ' + doc.mendeleyId);
		s.addCondition('deleted', 'false');
		if ((await s.search()).length) { skipped++; continue; }

		let type = Zotero.ItemTypes.getID(doc.itemType) ? doc.itemType : 'document';
		let item = new Zotero.Item(type);
		item.libraryID = libraryID;
		let extra = [doc.extra];
		for (let [field, value] of Object.entries(doc.fields)) {
			if (!setF(item, field, value)) {
				if (['DOI', 'ISBN', 'ISSN'].includes(field)) extra.push(field + ': ' + value);
				else if (field === 'publicationTitle') extra.push('Fonte: ' + value);
				else if (field !== 'type') log.push(`"${doc.fields.title}": campo ${field} não existe em ${type}, ignorado`);
			}
		}
		setF(item, 'extra', extra.join('\n'));
		let primary = Zotero.CreatorTypes.getName(Zotero.CreatorTypes.getPrimaryIDForType(item.itemTypeID));
		item.setCreators(doc.creators.map(c => ({
			firstName: c.firstName, lastName: c.lastName,
			creatorType: Zotero.CreatorTypes.isValidForItemType(Zotero.CreatorTypes.getID(c.creatorType), item.itemTypeID)
				? c.creatorType : primary,
		})));
		for (let t of doc.tags) item.addTag(t.tag, t.type);
		let cols = doc.collections.map(id => colIDs[id]).filter(Boolean);
		if (rootCol) cols.push(rootCol.id);
		item.setCollections(cols);
		let added = sqlDate(doc.dateAdded);
		if (added) { try { item.setField('dateAdded', added); } catch (e) {} }
		await item.saveTx();
		created++;

		for (let html of doc.notes) {
			let note = new Zotero.Item('note');
			note.libraryID = libraryID;
			note.parentID = item.id;
			note.setNote(html);
			await note.saveTx();
		}

		for (let f of doc.attachments) {
			let att = await Zotero.Attachments.importFromFile({
				file: DATA.attachAnnotatedPdfs ? f.annotatedPath : f.path,
				parentItemID: item.id,
				title: f.title,
			});
			pdfs++;
			if (DATA.attachAnnotatedPdfs) continue;
			for (let a of f.annotations) {
				let an = new Zotero.Item('annotation');
				an.libraryID = libraryID;
				an.parentID = att.id;
				an.annotationType = a.type;
				if (a.type === 'highlight' && a.text) an.annotationText = a.text;
				if (a.comment) an.annotationComment = a.comment;
				an.annotationColor = a.color;
				an.annotationPageLabel = a.pageLabel;
				an.annotationSortIndex = a.sortIndex;
				an.annotationPosition = JSON.stringify({ pageIndex: a.pageIndex, rects: a.rects });
				let d = sqlDate(a.dateAdded);
				if (d) { try { an.setField('dateAdded', d); } catch (e) {} }
				await an.saveTx();
				annots++;
			}
		}
	}
	catch (e) {
		log.push(`ERRO em "${doc.fields.title}": ${e}`);
	}
}

return `Itens criados: ${created} | já existiam: ${skipped} | PDFs: ${pdfs} | anotações: ${annots}`
	+ (log.length ? '\n\n' + log.join('\n') : '');
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mendeley", type=Path, default=MENDELEY_DIR, help="pasta de dados do Mendeley Reference Manager")
    ap.add_argument("--saida", type=Path, default=Path(__file__).resolve().parent / "saida")
    ap.add_argument("--perfil", help="id do perfil do Mendeley (se houver mais de um)")
    ap.add_argument("--colecao-raiz", default="Mendeley",
                    help='coleção do Zotero que recebe tudo, com as pastas do Mendeley dentro ("" para não criar)')
    ap.add_argument("--incluir-lixeira", action="store_true", help="exporta também os itens da lixeira do Mendeley")
    ap.add_argument("--anexar-pdfs-anotados", action="store_true",
                    help="no Zotero, anexa os PDFs com anotações gravadas em vez de criar anotações nativas")
    args = ap.parse_args()

    profile, documents, files, annotations, collections = load_library(args.mendeley, args.perfil)
    userfiles = args.mendeley / "userfiles"

    out = args.saida.resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / "pdfs").mkdir(parents=True)
    (out / "pdfs_anotados").mkdir()

    annots_by_file = defaultdict(list)
    for a in annotations:
        annots_by_file[a["file_id"]].append(a)

    collection_ids = {c["id"] for c in collections}
    items, missing, warnings = [], [], []
    trashed = 0
    for doc in sorted(documents.values(), key=lambda d: d.get("created", "")):
        if doc.get("isTrashed") and not args.incluir_lixeira:
            trashed += 1
            continue
        item = build_item(doc, collection_ids)
        item["attachments"] = []
        for f in files.get(doc["id"], []):
            src = userfiles / f"{f['id']}.pdf"
            if f.get("mime_type", "application/pdf") != "application/pdf":
                continue
            if not src.exists():
                missing.append(f"{item['fields'].get('title', doc['id'])} — {f.get('file_name')}")
                continue
            name = safe_name(f.get("file_name") or f"{f['id']}.pdf")
            dst = unique_path(out / "pdfs", name)
            dst_annot = out / "pdfs_anotados" / dst.name
            zannots, warns = process_pdf(src, dst, dst_annot, annots_by_file.pop(f["id"], []))
            warnings += [f"{dst.name}: {w}" for w in warns]
            item["attachments"].append({
                "path": str(dst),
                "annotatedPath": str(dst_annot),
                "title": "PDF",
                "annotations": zannots,
            })
        items.append(item)

    orphan = sum(len(v) for v in annots_by_file.values())

    data = {
        "rootCollection": args.colecao_raiz or None,
        "attachAnnotatedPdfs": args.anexar_pdfs_anotados,
        "collections": [{"id": c["id"], "name": c["name"], "parentId": c.get("parent_id") or c.get("parentId")}
                        for c in collections],
        "items": items,
    }
    (out / "dados.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "importar_no_zotero.js").write_text(
        JS_TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)), encoding="utf-8")
    (out / "biblioteca.bib").write_text(bibtex(items), encoding="utf-8")

    n_pdfs = sum(len(i["attachments"]) for i in items)
    n_ann = sum(len(a["annotations"]) for i in items for a in i["attachments"])
    print(f"Perfil Mendeley: {profile}")
    print(f"Referências exportadas: {len(items)}  (lixeira ignorada: {trashed})")
    print(f"Coleções: {', '.join(c['name'] for c in collections) or '-'}")
    print(f"PDFs: {n_pdfs}   anotações: {n_ann}")
    if orphan:
        print(f"Anotações não exportadas (item na lixeira ou PDF não baixado): {orphan}")
    if missing:
        print(f"\nPDFs que não estão baixados no Mendeley ({len(missing)}) — abra-os no Mendeley e rode de novo:")
        print("\n".join(f"  - {m}" for m in missing))
    if warnings:
        print("\nAvisos:")
        print("\n".join(f"  - {w}" for w in warnings))
    print(f"\nSaída em {out}")
    print("Próximo passo: Zotero → Ferramentas → Desenvolvedor → Executar JavaScript,\n"
          "marque 'Executar como função assíncrona', cole importar_no_zotero.js e Execute.\n"
          "Não apague a pasta de saída antes disso (o Zotero copia os PDFs de lá).")


if __name__ == "__main__":
    main()
