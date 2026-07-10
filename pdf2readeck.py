#!/usr/bin/env python3
"""
pdf2readeck v0.2.2
Convertit un PDF en HTML structuré (h1/h2/h3/p) et l'envoie à Readeck.
Détection de structure via heuristique typographique (taille de fonte + bold).
Détection et correction interactive : filigranes rotatifs, mise en page multi-colonnes.

Usage interactif : python pdf2readeck.py
Usage CLI       : python pdf2readeck.py --source <fichier|url> --url <doi>
"""

__version__ = "0.2.2"

import argparse
import base64
import collections
import concurrent.futures
import itertools
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

READECK_URL   = os.getenv("READECK_URL", "").rstrip("/")
READECK_TOKEN = os.getenv("READECK_TOKEN", "")

OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL    = os.getenv(
    "OPENROUTER_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
)
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TIMEOUT  = int(os.getenv("OPENROUTER_TIMEOUT", "420"))

# Seuils de détection
ROTATION_CHAR_THRESHOLD = 50    # nb minimum de chars rotatifs pour déclencher l'alerte
ROTATION_MATRIX_EPSILON = 0.1   # abs(matrix[1]) > seuil => char considéré rotatif
BIMODAL_COLUMN_RATIO    = 0.15  # creux dans l'histogramme X < ratio du max => bimodal

# Lots de traduction (regroupement de blocs par requête LLM)
# Des lots plus gros réduisent le nombre de requêtes — le principal facteur
# de lenteur étant le coût fixe par appel (latence + raisonnement du modèle),
# pas la taille du contenu traduit.
TRANSLATE_BATCH_MAX_ITEMS  = 60
TRANSLATE_BATCH_MAX_CHARS  = 12000
TRANSLATE_MAX_CONCURRENCY = int(os.getenv("TRANSLATE_MAX_CONCURRENCY", "5"))


# ══════════════════════════════════════════════════════════════════
#  TERMINAL UI
# ══════════════════════════════════════════════════════════════════

def fg(n: int, text: str) -> str:
    return f"\033[38;5;{n}m{text}\033[0m"

def p(text: str = "") -> None:
    print(text)

def praw(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


# ── Logo ──────────────────────────────────────────────────────────

LOGO = [
    (54,  "  `7MM\"\"\"Mq.         `7MM\"\"\"Mq.  "),
    (55,  "    MM   `MM.          MM   `MM. "),
    (91,  "    MM   ,M9 pd*\"*b.   MM   ,M9  "),
    (92,  "    MMmmdM9 (O)   j8   MMmmdM9   "),
    (135, "    MM          ,;j9   MM  YM.   "),
    (141, "    MM       ,-='      MM   `Mb. "),
    (147, "  .JMML.    Ammmmmmm .JMML. .JMM."),
]

SEP = "  " + "─" * 54
VER = f"v{__version__}"

def print_header() -> None:
    p()
    p(SEP)
    p()
    for color, row in LOGO:
        p(fg(color, row))
    p()
    p(SEP)
    p(f"    PDF → HTML structuré → Readeck   {VER}")
    p(f"    Par Axelle Abbadie – https://github.com/core-hn/pdf2readeck")
    p(SEP)
    p()


# ── Spinner ───────────────────────────────────────────────────────

class Spinner:
    def __init__(self, label: str):
        self.label   = label
        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self) -> None:
        frames = itertools.cycle(["✦", "✧", "⋆", "✶"])
        colors = itertools.cycle([147, 141, 135, 141])
        while not self._stop.is_set():
            star  = next(frames)
            color = next(colors)
            praw(f"\r  {fg(color, star)}  {self.label}…\033[K")
            time.sleep(0.15)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        praw("\r\033[K")


# ── Messages ──────────────────────────────────────────────────────

def ok(label: str, value: str = "") -> None:
    val = f"  {value}" if value else ""
    print(f"  {fg(82, '✔')}  {label}{val}")

def info(label: str, value: str = "") -> None:
    val = f"  {value}" if value else ""
    print(f"  ·  {label}{val}")

def warn(label: str, value: str = "") -> None:
    val = f"  {value}" if value else ""
    print(f"  {fg(220, '!')}  {fg(220, label)}{val}")

def die(label: str, value: str = "") -> None:
    val = f"  {value}" if value else ""
    print(f"  {fg(196, '✘')}  {fg(196, label)}{val}")
    p()
    sys.exit(1)

def section(title: str) -> None:
    p()
    print(f"  ──  {fg(141, title)}  {'─' * max(0, 46 - len(title))}")
    p()

def divider() -> None:
    p(SEP)

def prompt(label: str, required: bool = True) -> str:
    arrow = fg(141, "›")
    while True:
        praw(f"  {arrow}  {label}  ")
        try:
            value = input("").strip()
        except (KeyboardInterrupt, EOFError):
            p()
            print(f"  {fg(220, 'Annulé.')}")
            p()
            sys.exit(0)
        if value or not required:
            return value
        print(f"  {fg(196, 'Ce champ est obligatoire.')}")

def confirm(label: str, default_yes: bool = True) -> bool:
    hint  = fg(238, "[O/n]" if default_yes else "[o/N]")
    arrow = fg(141, "›")
    praw(f"  {arrow}  {label}  {hint}  ")
    try:
        value = input("").strip().lower()
    except (KeyboardInterrupt, EOFError):
        p()
        sys.exit(0)
    if not value:
        return default_yes
    return value in ("o", "oui", "y", "yes")


# ══════════════════════════════════════════════════════════════════
#  PDF → STRUCTURE
# ══════════════════════════════════════════════════════════════════

def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s.strip()))


def resolve_pdf(source: str) -> str:
    source = source.strip()
    if is_url(source):
        with Spinner("Téléchargement du PDF"):
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            try:
                urllib.request.urlretrieve(source, tmp.name)
            except Exception as e:
                die("Téléchargement échoué", str(e))
        ok("PDF téléchargé", tmp.name)
        return tmp.name

    path = Path(source).expanduser().resolve()
    if not path.exists():
        die("Fichier introuvable", str(path))
    if path.suffix.lower() != ".pdf":
        warn("Extension inattendue", path.suffix)
    ok("Fichier trouvé", str(path))
    return str(path)


# ── Détection d'anomalies ─────────────────────────────────────────

def _dedupe_chars(chars: list) -> list:
    """
    Supprime les caractères redessinés plusieurs fois à la même position.
    Certains PDF (scans/réimpressions) empâtent le texte en repeignant
    chaque glyphe plusieurs fois au pixel près, ce qui multiplie le nombre
    de caractères extraits (et donc la taille du contenu) sans rien ajouter.
    """
    seen = set()
    result = []
    for ch in chars:
        key = (ch.get("text"), round(ch.get("x0", 0), 1), round(ch.get("top", 0), 1))
        if key in seen:
            continue
        seen.add(key)
        result.append(ch)
    return result


def _is_rotated(ch: dict) -> bool:
    """Retourne True si le caractère a une matrice de transformation rotative."""
    matrix = ch.get("matrix")
    if not matrix or len(matrix) < 4:
        return False
    # matrix = [a, b, c, d, e, f] — rotation si abs(b) ou abs(c) > epsilon
    return abs(matrix[1]) > ROTATION_MATRIX_EPSILON or abs(matrix[2]) > ROTATION_MATRIX_EPSILON


def _detect_columns(chars: list, page_width: float) -> bool:
    """
    Détecte une mise en page multi-colonnes via distribution bimodale des X.
    On découpe l'espace horizontal en 20 bandes et on cherche un creux central.
    """
    if not chars or page_width <= 0:
        return False

    # Histogramme des positions X normalisées (0→1)
    buckets = [0] * 20
    for ch in chars:
        x = ch.get("x0", 0)
        bucket = min(int((x / page_width) * 20), 19)
        buckets[bucket] += 1

    if not any(buckets):
        return False

    max_count = max(buckets)
    if max_count == 0:
        return False

    # Cherche un creux dans la moitié centrale (buckets 7-13)
    center_buckets = buckets[7:14]
    center_min = min(center_buckets)

    # Vérifie que les deux moitiés ont du contenu
    left_max  = max(buckets[:7])
    right_max = max(buckets[13:])

    if left_max == 0 or right_max == 0:
        return False

    # Bimodal si le creux central est < RATIO du maximum global
    return center_min < max_count * BIMODAL_COLUMN_RATIO


# ── Heuristique typographique ─────────────────────────────────────

def _is_bold(fontname: str) -> bool:
    return bool(re.search(r"bold|Black|Heavy|Demi", fontname or "", re.I))


def _body_size(chars: list) -> float:
    counts: dict = collections.Counter()
    for ch in chars:
        size = ch.get("size") or 0
        if size > 2:
            counts[round(size * 2) / 2] += 1
    return counts.most_common(1)[0][0] if counts else 10.0


def _tag_for_line(line_size: float, line_bold: bool, body: float) -> str:
    delta = line_size - body
    if delta >= 4.0:
        return "h1"
    if delta >= 1.5:
        return "h2"
    if delta >= 0.5 or (line_bold and delta > -0.5):
        return "h3"
    return "p"


def _lines_to_blocks(page_lines: list, body_size: float) -> list:
    """Convertit une liste de lignes (list of chars) en blocs hiérarchiques."""
    blocks = []
    current_tag  = None
    current_text = []

    for line_chars in page_lines:
        text = "".join(ch.get("text", "") for ch in line_chars).strip()
        if not text:
            if current_text:
                blocks.append({"tag": current_tag or "p",
                                "text": " ".join(current_text)})
                current_tag  = None
                current_text = []
            continue

        sizes   = [ch.get("size") or 0 for ch in line_chars if ch.get("size")]
        bolds   = [_is_bold(ch.get("fontname", "")) for ch in line_chars]
        avg_sz  = sum(sizes) / len(sizes) if sizes else body_size
        is_bold = sum(bolds) / len(bolds) > 0.5 if bolds else False
        tag     = _tag_for_line(avg_sz, is_bold, body_size)

        if tag == current_tag:
            current_text.append(text)
        else:
            if current_text:
                blocks.append({"tag": current_tag or "p",
                                "text": " ".join(current_text)})
            current_tag  = tag
            current_text = [text]

    if current_text:
        blocks.append({"tag": current_tag or "p",
                        "text": " ".join(current_text)})
    return blocks


def _chars_to_lines(chars: list) -> list:
    """Regroupe des chars par position Y arrondie."""
    by_y: dict = collections.defaultdict(list)
    for ch in chars:
        y_key = round((ch.get("top") or 0) / 2) * 2
        by_y[y_key].append(ch)
    return [by_y[y] for y in sorted(by_y)]


def _chars_to_lines_columns(chars: list, page_width: float) -> list:
    """
    Regroupe les chars en deux colonnes (gauche puis droite),
    triées par Y à l'intérieur de chaque colonne.
    """
    mid = page_width / 2
    left_chars  = [ch for ch in chars if (ch.get("x0") or 0) < mid]
    right_chars = [ch for ch in chars if (ch.get("x0") or 0) >= mid]
    return _chars_to_lines(left_chars) + _chars_to_lines(right_chars)


# ── Extraction principale ─────────────────────────────────────────

def extract_structured_content(pdf_path: str) -> dict:
    try:
        import pdfplumber
    except ImportError:
        die("pdfplumber non installé", "conda activate pdf2readeck")

    result = {
        "title": "", "author": "",
        "blocks": [],
        "images": [],
    }

    # ── Première passe : extraction brute + détection
    with Spinner("Analyse du PDF"):
        with pdfplumber.open(pdf_path) as pdf:
            meta = pdf.metadata or {}
            result["title"]  = (meta.get("Title")  or "").strip()
            result["author"] = (meta.get("Author") or "").strip()

            all_chars   = []
            pages_data  = []   # list of (chars, page_width)

            for page in pdf.pages:
                chars      = _dedupe_chars(page.chars or [])
                page_width = float(page.width or 600)
                all_chars.extend(chars)
                pages_data.append((chars, page_width))

    # ── Détection des anomalies
    rotated_chars = [ch for ch in all_chars if _is_rotated(ch)]
    nb_rotated    = len(rotated_chars)

    # Détection colonnes sur la première page non vide
    has_columns = False
    for chars, pw in pages_data:
        if chars:
            has_columns = _detect_columns(chars, pw)
            break

    # ── Propositions de patchs
    apply_rotation_filter = False
    apply_column_mode     = False

    section("Analyse")

    nb_total = len(all_chars)
    ok("Caractères extraits", f"{nb_total:,}")

    if nb_rotated >= ROTATION_CHAR_THRESHOLD:
        ratio = nb_rotated / max(nb_total, 1) * 100
        warn(
            "Filigrane rotatif détecté",
            f"{nb_rotated:,} caractères ({ratio:.1f}% du total)"
        )
        p()
        apply_rotation_filter = confirm(
            "Réextraire en ignorant les caractères rotatifs ?"
        )
        if apply_rotation_filter:
            ok("Patch rotation activé")
        else:
            info("Patch rotation ignoré")
        p()

    if has_columns:
        warn("Mise en page multi-colonnes probable")
        p()
        apply_column_mode = confirm(
            "Réextraire en mode deux colonnes ?"
        )
        if apply_column_mode:
            ok("Patch colonnes activé")
        else:
            info("Patch colonnes ignoré")
        p()

    # ── Deuxième passe : extraction avec patchs
    with Spinner("Extraction structurée"):
        with pdfplumber.open(pdf_path) as pdf:
            all_chars_filtered = []
            page_lines_all     = []

            for page in pdf.pages:
                chars      = _dedupe_chars(page.chars or [])
                page_width = float(page.width or 600)

                # Patch rotation
                if apply_rotation_filter:
                    chars = [ch for ch in chars if not _is_rotated(ch)]

                all_chars_filtered.extend(chars)

                # Patch colonnes
                if apply_column_mode:
                    lines = _chars_to_lines_columns(chars, page_width)
                else:
                    lines = _chars_to_lines(chars)

                page_lines_all.extend(lines)

            body_size = _body_size(all_chars_filtered)
            result["blocks"] = _lines_to_blocks(page_lines_all, body_size)

    nb_h1 = sum(1 for b in result["blocks"] if b["tag"] == "h1")
    nb_h2 = sum(1 for b in result["blocks"] if b["tag"] == "h2")
    nb_h3 = sum(1 for b in result["blocks"] if b["tag"] == "h3")
    nb_p  = sum(1 for b in result["blocks"] if b["tag"] == "p")

    ok("Structure détectée",
       f"{len(result['blocks'])} blocs  ·  h1:{nb_h1}  h2:{nb_h2}  h3:{nb_h3}  p:{nb_p}")

    # ── Images via PyMuPDF (optionnel)
    try:
        import fitz
        with Spinner("Extraction des images"):
            doc = fitz.open(pdf_path)
            for page_num, page in enumerate(doc):
                for idx, img in enumerate(page.get_images(full=True)):
                    try:
                        xref = img[0]
                        pix  = fitz.Pixmap(doc, xref)
                        # Convertir tout ce qui n'est pas RGB ou grayscale
                        if pix.colorspace and pix.colorspace.n > 3:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        elif pix.alpha:
                            pix = fitz.Pixmap(pix, 0)  # supprime le canal alpha
                        b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
                        result["images"].append({
                            "id": f"img_p{page_num}_{idx}", "b64": b64,
                            "mime": "image/png", "page": page_num,
                        })
                    except Exception:
                        continue  # image non convertible, on passe
            doc.close()
        ok("Images extraites", f"{len(result['images'])} image(s)")
    except ImportError:
        info("Images ignorées", "PyMuPDF absent (optionnel)")

    return result


def cache_extracted_content(content: dict) -> str:
    """
    Sauvegarde les blocs extraits (hors images) dans un fichier JSON temporaire.
    Filet de sécurité : si une étape ultérieure (traduction, envoi) plante,
    l'extraction — souvent longue et interactive — n'est pas à refaire.
    """
    cache = {
        "title":  content["title"],
        "author": content["author"],
        "blocks": content["blocks"],
    }
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="pdf2readeck_",
        delete=False, encoding="utf-8"
    )
    with tmp:
        json.dump(cache, tmp, ensure_ascii=False, indent=2)
    return tmp.name


# ══════════════════════════════════════════════════════════════════
#  HTML
# ══════════════════════════════════════════════════════════════════

def build_html(content: dict, citation_url: str, title_override: str = "") -> str:
    title  = title_override or content["title"] or "Document sans titre"
    author = content["author"]

    author_html = f'<p class="author">{author}</p>' if author else ""
    source_html = (f'<p class="source">'
                   f'<a href="{citation_url}">{citation_url}</a></p>')

    body_html = [f"<{b['tag']}>{b['text']}</{b['tag']}>"
                 for b in content["blocks"]]

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="citation_url" content="{citation_url}">
<title>{title}</title>
<style>
  body  {{ font-family: Georgia, serif; max-width: 780px; margin: 2rem auto;
           line-height: 1.75; color: #111; padding: 0 1rem; }}
  h1    {{ font-size: 1.7rem; margin: 2rem 0 .4rem; }}
  h2    {{ font-size: 1.35rem; margin: 1.6rem 0 .3rem; }}
  h3    {{ font-size: 1.1rem; margin: 1.2rem 0 .2rem; font-style: italic; }}
  p     {{ margin: .5rem 0; }}
  .author {{ color: #555; font-style: italic; margin-bottom: .2rem; }}
  .source {{ font-size: .85rem; color: #888; margin-bottom: 2rem; }}
  .source a {{ color: #5a6fa5; }}
  img   {{ max-width: 100%; height: auto; margin: 1rem 0; }}
</style>
</head>
<body>
<h1>{title}</h1>
{author_html}
{source_html}
{"".join(body_html)}
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════
#  TRADUCTION (OpenRouter)
# ══════════════════════════════════════════════════════════════════

def _build_translation_context(title: str, target_lang: str) -> str:
    doc_ref = f" intitulé « {title} »" if title else ""
    return (
        f"Tu traduis des extraits consécutifs d'un document académique{doc_ref} "
        f"vers la langue « {target_lang} ». La traduction sert à saisir le sens en "
        f"lecture, pas à produire un texte de référence : privilégie la fluidité et "
        f"la fidélité du sens plutôt que la littéralité. Les segments sont donnés "
        f"dans leur ordre d'apparition dans le document ; garde la cohérence "
        f"(reprises, temps verbaux, terminologie) avec ce contexte global."
    )


def _chunk_texts(texts: list) -> list:
    """Découpe une liste de textes en lots pour limiter la taille des requêtes LLM."""
    batches = []
    current       = []
    current_chars = 0

    for text in texts:
        text_len = len(text)
        if current and (
            len(current) >= TRANSLATE_BATCH_MAX_ITEMS
            or current_chars + text_len > TRANSLATE_BATCH_MAX_CHARS
        ):
            batches.append(current)
            current       = []
            current_chars = 0
        current.append(text)
        current_chars += text_len

    if current:
        batches.append(current)
    return batches


def _translate_batch(texts: list, target_lang: str, context: str,
                      on_delta=None) -> list:
    """
    Traduit un lot de segments via OpenRouter, en streaming.
    Le streaming sert à la fois à afficher une progression réelle et à
    pouvoir imposer un délai global : OpenRouter envoie des commentaires
    keep-alive périodiques sur les requêtes non-streamées, ce qui empêche
    un timeout de lecture classique (`requests.post(timeout=...)`) de se
    déclencher même après plusieurs dizaines de minutes.
    """
    numbered     = "\n".join(f"{i + 1}: {t}" for i, t in enumerate(texts))
    instructions = (
        "Réponds uniquement avec les traductions, un segment par ligne, dans le "
        "même ordre, au format \"N :: traduction\" (N étant le numéro du segment). "
        "N'ajoute aucun commentaire, aucune explication, aucun texte avant ou "
        "après la liste."
    )

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": f"{context}\n\n{instructions}"},
            {"role": "user",   "content": numbered},
        ],
        "temperature": 0.2,
        # Tâche mécanique de traduction : le raisonnement n'apporte rien et
        # gonfle le nombre de tokens de sortie (donc la latence) inutilement.
        "reasoning": {"enabled": False},
        "stream": True,
    }

    started = time.monotonic()
    parts   = []

    try:
        with requests.post(
            OPENROUTER_ENDPOINT, headers=headers, json=payload,
            timeout=(10, 60), stream=True
        ) as response:
            if response.status_code != 200:
                warn(f"Lot non traduit (erreur OpenRouter {response.status_code})",
                     response.text[:120])
                return texts

            for raw_bytes in response.iter_lines():
                if time.monotonic() - started > OPENROUTER_TIMEOUT:
                    warn("Lot abandonné (délai global dépassé)",
                         f"{OPENROUTER_TIMEOUT}s")
                    return texts

                if not raw_bytes:
                    continue
                # Décodage explicite en UTF-8 : `decode_unicode=True` s'appuie
                # sur response.encoding, que requests devine souvent mal pour
                # les flux SSE (fallback ISO-8859-1), ce qui mojibake les accents.
                raw_line = raw_bytes.decode("utf-8", errors="replace")
                if not raw_line.startswith("data: "):
                    continue
                data = raw_line[len("data: "):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {}).get("content") or ""
                if delta:
                    parts.append(delta)
                    if on_delta:
                        on_delta(len(delta))
    except requests.exceptions.RequestException as e:
        warn("Lot non traduit (erreur réseau OpenRouter)", str(e)[:120])
        return texts
    except Exception as e:
        # Un lot ne doit jamais faire planter toute la traduction : une forme
        # de réponse imprévue côté API doit dégrader ce lot, pas le script.
        warn("Lot non traduit (erreur inattendue)", str(e)[:120])
        return texts

    reply = "".join(parts)

    translated = {}
    for line in reply.splitlines():
        match = re.match(r"\s*(\d+)\s*::\s*(.*)$", line)
        if match:
            idx, text = match.groups()
            translated[int(idx)] = text.strip()

    return [translated.get(i + 1, texts[i]) for i in range(len(texts))]


def _translate_texts(texts: list, target_lang: str, context: str) -> list:
    if not texts:
        return []
    if not OPENROUTER_API_KEY:
        die("OpenRouter non configuré", "OPENROUTER_API_KEY manquant dans .env")

    batches        = _chunk_texts(texts)
    nb_batches     = len(batches)
    results        = [None] * nb_batches
    done           = 0
    chars_received = 0
    lock           = threading.Lock()

    def progress_label() -> str:
        return (f"Traduction ({done}/{nb_batches} lots  ·  "
                f"{TRANSLATE_MAX_CONCURRENCY} en parallèle  ·  "
                f"{chars_received:,} caractères reçus)")

    def on_delta(n: int) -> None:
        nonlocal chars_received
        with lock:
            chars_received += n
            spinner.label = progress_label()

    with Spinner(progress_label()) as spinner:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=TRANSLATE_MAX_CONCURRENCY
        ) as executor:
            futures = {
                executor.submit(_translate_batch, batch, target_lang, context, on_delta): i
                for i, batch in enumerate(batches)
            }
            for future in concurrent.futures.as_completed(futures):
                i = futures[future]
                results[i] = future.result()
                with lock:
                    done += 1
                    spinner.label = progress_label()

    ok(f"{nb_batches} lot(s) traduits")

    translated = []
    for batch_result in results:
        translated.extend(batch_result)
    return translated


def translate_content(content: dict, title: str, target_lang: str) -> tuple:
    """Traduit les blocs de contenu et le titre. Retourne (content, titre_traduit)."""
    block_texts = [b["text"] for b in content["blocks"]]
    all_texts   = block_texts + ([title] if title else [])
    context     = _build_translation_context(title, target_lang)

    info(f"Traduction vers « {target_lang} »", OPENROUTER_MODEL)
    p()
    translated = _translate_texts(all_texts, target_lang, context)

    nb_blocks = len(block_texts)
    for block, text in zip(content["blocks"], translated[:nb_blocks]):
        block["text"] = text
    new_title = translated[nb_blocks] if title else title

    ok("Contenu traduit", f"{nb_blocks} blocs → {target_lang}")
    return content, new_title


# ══════════════════════════════════════════════════════════════════
#  READECK API
# ══════════════════════════════════════════════════════════════════

def send_to_readeck(
    citation_url: str,
    html: str,
    title: str = "",
    labels: list = None,
) -> dict:
    if not READECK_URL:
        die("READECK_URL manquant", "ajoute-le dans .env")
    if not READECK_TOKEN:
        die("READECK_TOKEN manquant", "ajoute-le dans .env")

    endpoint = f"{READECK_URL}/api/bookmarks"
    headers  = {
        "Authorization": f"Bearer {READECK_TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    payload = {"url": citation_url, "html": html}
    if title:
        payload["title"] = title
    if labels:
        payload["labels"] = labels

    with Spinner("Envoi vers Readeck"):
        response = requests.post(
            endpoint, headers=headers, json=payload, timeout=30
        )

    if response.status_code not in (200, 201, 202):
        die(f"Erreur Readeck {response.status_code}", response.text[:120])

    bookmark_id   = response.headers.get("bookmark-id", "")
    bookmark_page = ""
    match = re.search(
        r"<([^>]+)>;\s*rel=\"alternate\"",
        response.headers.get("link", "")
    )
    if match:
        bookmark_page = match.group(1)
    bookmark_api = response.headers.get("location", "")

    return {
        "id":       bookmark_id,
        "page_url": bookmark_page,
        "api_url":  bookmark_api,
        "status":   response.status_code,
    }


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"pdf2readeck v{__version__} — PDF structuré → Readeck"
    )
    parser.add_argument("--source", "-s",
                        help="Chemin local (relatif/absolu) ou URL d'un PDF")
    parser.add_argument("--url", "-u", help="URL ou DOI de citation")
    parser.add_argument("--title", "-t", default="", help="Titre du bookmark")
    parser.add_argument("--labels", "-l", nargs="*", default=[],
                        help="Labels Readeck (ex : --labels lecture these)")
    parser.add_argument("--translate-to", "-tt", default="",
                        help="Code langue cible pour traduire le contenu (ex : fr, en, es) via OpenRouter")
    parser.add_argument("--version", "-v",
                        action="version", version=f"pdf2readeck {__version__}")
    args = parser.parse_args()

    print_header()

    # ── Source PDF
    section("Source")
    source = args.source
    if not source:
        info("Chemin local (relatif ou absolu) ou URL directe vers un PDF")
        p()
        source = prompt("Source PDF")

    # ── URL de citation
    citation_url = args.url
    if not citation_url:
        p()
        info("DOI ou URL de l'article auquel rattacher ce PDF")
        p()
        citation_url = prompt("URL de citation")

    title_override = args.title
    labels         = args.labels or []

    # ── Labels (interactif seulement si pas fournis en CLI)
    if not labels and not args.labels:
        p()
        info("Labels Readeck (séparés par des virgules, laisser vide pour ignorer)")
        p()
        raw = prompt("Labels", required=False)
        labels = [l.strip() for l in raw.split(",") if l.strip()] if raw else []
        if labels:
            ok("Labels retenus", ", ".join(labels))

    # ── Résolution
    section("Résolution")
    pdf_path    = resolve_pdf(source)
    is_tmp_file = is_url(source)

    try:
        # ── Extraction + analyse + patchs interactifs
        content    = extract_structured_content(pdf_path)
        cache_path = cache_extracted_content(content)
        info("Contenu extrait sauvegardé (filet de sécurité)", cache_path)

        # ── Validation du titre
        section("Titre")
        detected = content["title"]
        if not title_override:
            if detected:
                info("Titre détecté dans les métadonnées")
                p(f"    {fg(147, '«')} {detected} {fg(147, '»')}")
                p()
                if confirm("Utiliser ce titre ?"):
                    title_override = detected
                else:
                    p()
                    title_override = prompt("Titre personnalisé")
                ok("Titre retenu", title_override)
            else:
                warn("Aucun titre dans les métadonnées")
                p()
                title_override = prompt("Titre du bookmark")
                ok("Titre retenu", title_override)
        else:
            ok("Titre retenu", title_override)

        # ── Traduction (optionnelle, via OpenRouter)
        section("Traduction")
        target_lang = args.translate_to
        if not target_lang:
            if confirm("Traduire le contenu avant l'envoi ?", default_yes=False):
                p()
                target_lang = prompt("Langue cible (ex : fr, en, es)")
            else:
                info("Traduction ignorée")
        if target_lang:
            p()
            content, title_override = translate_content(
                content, title_override, target_lang
            )

        # ── Génération HTML
        section("HTML")
        with Spinner("Construction du document"):
            html = build_html(content, citation_url, title_override)
        ok("HTML généré", f"{len(html):,} caractères")

        # ── Envoi
        section("Envoi")
        info("Instance", READECK_URL)
        info("Citation", citation_url)
        info("Titre",    title_override)
        if labels:
            info("Labels", ", ".join(labels))
        p()
        result = send_to_readeck(
            citation_url, html, title=title_override, labels=labels
        )
        ok("Réponse reçue", f"HTTP {result['status']}")

        # ── Résultat final
        p()
        divider()
        p()
        print(f"  {fg(82, '✔')}  Bookmark créé avec succès")
        p()
        print(f"    ID      {fg(147, result['id'])}")
        print(f"    Page    {result['page_url']}")
        print(f"    API     {fg(238, result['api_url'])}")
        p()
        divider()
        p()

    finally:
        if is_tmp_file and os.path.exists(pdf_path):
            os.unlink(pdf_path)
            info("Fichier temporaire supprimé")
            p()


if __name__ == "__main__":
    main()