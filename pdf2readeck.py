#!/usr/bin/env python3
"""
pdf2readeck — Convertit un PDF en HTML structuré et l'envoie à Readeck.

Usage:
    python pdf2readeck.py --file article.pdf --url https://doi.org/10.xxxx/yyyy
    python pdf2readeck.py --pdf-url https://example.org/article.pdf --url https://doi.org/10.xxxx/yyyy
    python pdf2readeck.py --file article.pdf --url https://doi.org/10.xxxx --title "Mon titre"
"""

import argparse
import base64
import io
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

READECK_URL = os.getenv("READECK_URL", "").rstrip("/")
READECK_TOKEN = os.getenv("READECK_TOKEN", "")


# ─── PDF parsing ──────────────────────────────────────────────────────────────

def extract_pdf_content(pdf_path: str) -> dict:
    """
    Extrait le texte, les métadonnées et les images d'un PDF.
    Retourne un dict avec : title, author, pages (list of str), images (list of dict).
    """
    try:
        import pdfplumber
    except ImportError:
        sys.exit("Erreur : pdfplumber non installé. Lance : pip install pdfplumber")

    result = {"title": "", "author": "", "pages": [], "images": []}

    with pdfplumber.open(pdf_path) as pdf:
        meta = pdf.metadata or {}
        result["title"] = meta.get("Title", "") or ""
        result["author"] = meta.get("Author", "") or ""

        for page in pdf.pages:
            text = page.extract_text() or ""
            result["pages"].append(text)

    # Images via PyMuPDF (optionnel — graceful degradation si absent)
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        for page_num, page in enumerate(doc):
            for img_index, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                img_bytes = pix.tobytes("png")
                result["images"].append({
                    "id": f"img_p{page_num}_{img_index}",
                    "data": img_bytes,
                    "mime": "image/png",
                    "page": page_num,
                })
        doc.close()
    except ImportError:
        print("Info : PyMuPDF non installé — les images du PDF ne seront pas extraites.", file=sys.stderr)

    return result


# ─── HTML generation ──────────────────────────────────────────────────────────

def build_html(content: dict, citation_url: str, title_override: str = "") -> str:
    """
    Construit un HTML propre à partir du contenu extrait.
    Les images sont encodées en base64 inline pour simplifier.
    """
    title = title_override or content["title"] or "Document sans titre"
    author = content["author"]

    # Encode images en base64 pour injection inline
    img_map = {}
    for img in content["images"]:
        b64 = base64.b64encode(img["data"]).decode("ascii")
        img_map[img["id"]] = f"data:{img['mime']};base64,{b64}"

    pages_html = []
    for i, page_text in enumerate(content["pages"]):
        if not page_text.strip():
            continue
        # Paragraphes : on coupe sur double saut de ligne
        paragraphs = [p.strip() for p in page_text.split("\n\n") if p.strip()]
        paras_html = "\n".join(f"<p>{p}</p>" for p in paragraphs)
        pages_html.append(f'<section class="page" data-page="{i+1}">\n{paras_html}\n</section>')

    author_html = f'<p class="author">{author}</p>' if author else ""
    source_html = f'<p class="source"><a href="{citation_url}">{citation_url}</a></p>'

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="citation_url" content="{citation_url}">
<title>{title}</title>
<style>
  body {{ font-family: Georgia, serif; max-width: 800px; margin: 2rem auto; line-height: 1.7; color: #111; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 0.5rem; }}
  .author {{ color: #555; font-style: italic; }}
  .source {{ font-size: 0.85rem; color: #777; margin-bottom: 2rem; }}
  .source a {{ color: #4a6fa5; }}
  .page {{ margin-bottom: 2rem; padding-bottom: 2rem; border-bottom: 1px solid #eee; }}
  p {{ margin: 0.6rem 0; }}
  img {{ max-width: 100%; height: auto; margin: 1rem 0; }}
</style>
</head>
<body>
<h1>{title}</h1>
{author_html}
{source_html}
{''.join(pages_html)}
</body>
</html>"""

    return html


# ─── Readeck API ──────────────────────────────────────────────────────────────

def send_to_readeck(
    citation_url: str,
    html: str,
    title: str = "",
    labels: list = None,
) -> dict:
    """
    Envoie le bookmark à Readeck via multipart/form-data.
    """
    if not READECK_URL:
        sys.exit("Erreur : READECK_URL manquant dans .env")
    if not READECK_TOKEN:
        sys.exit("Erreur : READECK_TOKEN manquant dans .env")

    endpoint = f"{READECK_URL}/api/bookmarks"
    headers = {"Authorization": f"Bearer {READECK_TOKEN}"}

    # Multipart : url + html comme resource avec Location
    fields = [
        ("url", (None, citation_url)),
    ]
    if title:
        fields.append(("title", (None, title)))
    if labels:
        for label in labels:
            fields.append(("labels", (None, label)))

    # Le HTML est envoyé comme resource avec Location = citation_url
    # afin que Readeck l'associe correctement à l'URL
    files = {
        "resource": ("_", html.encode("utf-8"), "text/html"),
    }
    # On utilise requests avec des champs mixtes
    # Construction manuelle car requests ne gère pas Location header sur parts
    import uuid
    boundary = uuid.uuid4().hex

    body_parts = []

    # url field
    body_parts.append(
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="url"\r\n\r\n'
        f'{citation_url}\r\n'
    )
    if title:
        body_parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="title"\r\n\r\n'
            f'{title}\r\n'
        )
    if labels:
        for label in labels:
            body_parts.append(
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="labels"\r\n\r\n'
                f'{label}\r\n'
            )

    # HTML resource avec Location header
    html_bytes = html.encode("utf-8")
    body_parts.append(
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="resource"; filename="_"\r\n'
        f'Location: {citation_url}\r\n'
        f'Content-Type: text/html\r\n\r\n'
    )

    # Assemblage du body bytes
    body = b""
    for part in body_parts:
        body += part.encode("utf-8")
    body += html_bytes
    body += f'\r\n--{boundary}--\r\n'.encode("utf-8")

    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    response = requests.post(endpoint, headers=headers, data=body, timeout=30)

    if response.status_code in (200, 201, 202):
        return response.json()
    else:
        print(f"Erreur Readeck {response.status_code}: {response.text}", file=sys.stderr)
        sys.exit(1)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convertit un PDF en HTML et l'envoie à Readeck avec une URL de citation."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", "-f", help="Chemin vers un PDF local")
    source.add_argument("--pdf-url", "-p", help="URL d'un PDF distant à télécharger")

    parser.add_argument("--url", "-u", required=True,
                        help="URL ou DOI de citation (ex: https://doi.org/10.xxxx/yyyy)")
    parser.add_argument("--title", "-t", default="",
                        help="Titre du bookmark (optionnel, remplace celui du PDF)")
    parser.add_argument("--labels", "-l", nargs="*", default=[],
                        help="Labels Readeck à appliquer (ex: --labels recherche article)")

    args = parser.parse_args()

    # Résolution du PDF
    if args.file:
        pdf_path = args.file
        if not Path(pdf_path).exists():
            sys.exit(f"Erreur : fichier introuvable : {pdf_path}")
        print(f"Lecture du PDF local : {pdf_path}")
    else:
        print(f"Téléchargement du PDF : {args.pdf_url}")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            urllib.request.urlretrieve(args.pdf_url, tmp.name)
            pdf_path = tmp.name

    # Extraction
    print("Extraction du contenu PDF...")
    content = extract_pdf_content(pdf_path)
    nb_pages = len(content["pages"])
    nb_images = len(content["images"])
    print(f"  {nb_pages} page(s), {nb_images} image(s) extraite(s)")

    if content["title"]:
        print(f"  Titre détecté : {content['title']}")

    # Génération HTML
    print("Génération du HTML...")
    html = build_html(content, args.url, title_override=args.title)

    # Envoi à Readeck
    effective_title = args.title or content["title"] or ""
    print(f"Envoi à Readeck ({READECK_URL})...")
    result = send_to_readeck(args.url, html, title=effective_title, labels=args.labels)

    bookmark_id = result.get("id", "?")
    bookmark_url = result.get("url", "")
    print(f"\nBookmark créé avec succès !")
    print(f"  ID      : {bookmark_id}")
    print(f"  URL     : {READECK_URL}/bookmarks/{bookmark_id}")
    print(f"  Source  : {bookmark_url}")

    # Nettoyage fichier temp
    if args.pdf_url and pdf_path:
        os.unlink(pdf_path)


if __name__ == "__main__":
    main()
