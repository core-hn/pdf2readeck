# pdf2readeck

Convertit un PDF académique en HTML lisible et l'envoie à Readeck en associant l'URL de citation (DOI ou autre).

## Installation

### Créer l'environnement conda

```bash
conda env create -f environment.yml
conda activate pdf2readeck
```

> **PyMuPDF est optionnel** : si tu ne l'installes pas, le script fonctionnera mais sans extraire les images du PDF. Il n'est pas disponible sur conda-forge — il s'installe via pip à l'intérieur de l'environnement conda, ce qui est géré automatiquement par `environment.yml`.

## Configuration

Copie `.env.example` en `.env` et remplis les valeurs :

```bash
cp .env.example .env
```

```env
READECK_URL=https://readeck.abbadie.ovh
READECK_TOKEN=ton_token_ici
```

Le token se génère dans Readeck → **Settings → Applications → Create token**.

## Usage

### Mode interactif (recommandé)

Lance simplement le script sans arguments. Il te demande au fur et à mesure :

```
$ python pdf2readeck.py

pdf2readeck v0.2.0
────────────────────────────────────────

Source du PDF
  Chemin local (relatif ou absolu) ou URL directe vers un PDF :
  > ~/Documents/maingueneau2002.pdf

URL de citation (DOI ou URL de l'article) :
  > https://doi.org/10.xxxx/yyyy

Titre détecté dans le PDF :
  « Discourse Analysis »
  Utiliser ce titre ? [O/n] : n
  Titre personnalisé : Maingueneau 2002 — Analyse du discours
```

Le script reconnaît automatiquement si tu donnes un chemin local (relatif `./article.pdf`, absolu `/home/axelle/docs/article.pdf`, avec `~`) ou une URL.

### Mode CLI (pour scripts et automatisation)

```bash
# PDF local
python pdf2readeck.py --source article.pdf --url https://doi.org/10.1016/j.langsci.2023.01.004

# PDF distant
python pdf2readeck.py --source https://example.org/article.pdf --url https://doi.org/10.xxxx/yyyy

# Avec titre et labels forcés (pas de questions posées)
python pdf2readeck.py \
  --source article.pdf \
  --url https://doi.org/10.xxxx/yyyy \
  --title "Maingueneau 2002 — Analyse du discours" \
  --labels lecture these ad
```

## Options

| Option | Court | Description |
|---|---|---|
| `--source` | `-s` | Chemin local (relatif/absolu) ou URL d'un PDF |
| `--url` | `-u` | URL ou DOI de citation |
| `--title` | `-t` | Titre du bookmark (remplace celui du PDF) |
| `--labels` | `-l` | Labels Readeck (séparés par des espaces) |
| `--version` | `-v` | Affiche la version |

## Ce que fait le script

1. Détecte si la source est un chemin local (relatif/absolu) ou une URL
2. Télécharge le PDF si nécessaire, nettoie le fichier temporaire après
3. Extrait le texte page par page via `pdfplumber`
4. Extrait les images via `PyMuPDF` si disponible (sinon continue sans)
5. Affiche le titre détecté dans les métadonnées pour validation
6. Génère un HTML propre et lisible
7. Envoie via `POST /api/bookmarks` en JSON avec le DOI/URL comme `url` et le HTML comme `html`
8. Récupère l'ID du bookmark dans le header `bookmark-id` de la réponse (pas dans le body)
9. Affiche l'URL directe vers le bookmark dans Readeck

## Changelog

### v0.2.1
- Interface terminal colorée (ANSI) : header ASCII, couleurs, icônes
- Spinner animé avec étoiles scintillantes (✦ ✧ ⋆ ✶) pendant les traitements
- Sections claires, prompts stylisés, résultat mis en valeur
- Gestion Ctrl+C propre

### v0.2.0
- Mode interactif : le script pose les questions si aucun argument n'est fourni
- Détection automatique du type de source (URL vs chemin local relatif/absolu)
- Validation du titre détecté avant envoi
- Correction de la récupération de l'ID : lu dans le header `bookmark-id` (la réponse body ne contient que `"Link submited"`)
- URL du bookmark récupérée dans le header `link`
- Passage au payload JSON (plus simple que le multipart)

### v0.1.0
- Version initiale
