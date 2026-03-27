# pdf2readeck

Convertit un PDF académique en HTML structuré et l'envoie à Readeck en associant l'URL de citation (DOI ou autre).

Par Axelle Abbadie — https://github.com/core-hn/pdf2readeck

---

## Installation

### Créer l'environnement conda

```bash
conda env create -f environment.yml
conda activate pdf2readeck
```

> **PyMuPDF est optionnel** : si tu ne l'installes pas, le script fonctionnera mais sans extraire les images du PDF. Il n'est pas disponible sur conda-forge — il s'installe via pip à l'intérieur de l'environnement conda, ce qui est géré automatiquement par `environment.yml`.

## Configuration

Copie `env.example` en `.env` et remplis les valeurs :

```bash
cp env.example .env
```

```env
READECK_URL=https://readeck.abbadie.ovh
READECK_TOKEN=ton_token_ici
```

Le token se génère dans Readeck → **Settings → Applications → Create token**.

---

## Usage

### Mode interactif (recommandé)

Lance simplement le script sans arguments. Il pose les questions au fur et à mesure :

```
$ python pdf2readeck.py

  ──────────────────────────────────────────────────────
  `7MM"""Mq.         `7MM"""Mq.
    MM   `MM.          MM   `MM.
    ...
  ──────────────────────────────────────────────────────
    PDF → HTML structuré → Readeck   v0.2.2
    Par Axelle Abbadie – https://github.com/core-hn/pdf2readeck
  ──────────────────────────────────────────────────────

  ──  Source  ─────────────────────────────────────────

  ·  Chemin local (relatif ou absolu) ou URL directe vers un PDF

  ›  Source PDF  ~/Documents/gajjala2002.pdf

  ·  DOI ou URL de l'article auquel rattacher ce PDF

  ›  URL de citation  https://doi.org/10.1080/14680770220150854

  ·  Labels Readeck (séparés par des virgules, laisser vide pour ignorer)

  ›  Labels  cyberethnographie, féminisme, lecture

  ──  Analyse  ────────────────────────────────────────

  ✔  Caractères extraits   48 231
  !  Filigrane rotatif détecté   1 247 caractères (2.6% du total)

  ›  Réextraire en ignorant les caractères rotatifs ? [O/n]  o
  ✔  Patch rotation activé

  !  Mise en page multi-colonnes probable

  ›  Réextraire en mode deux colonnes ? [O/n]  o
  ✔  Patch colonnes activé
```

Le script reconnaît automatiquement si la source est un chemin local (relatif `./article.pdf`, absolu `/home/axelle/docs/article.pdf`, avec `~`) ou une URL.

### Mode CLI (pour scripts et automatisation)

```bash
# PDF local
python pdf2readeck.py \
  --source article.pdf \
  --url https://doi.org/10.1080/14680770220150854

# PDF distant
python pdf2readeck.py \
  --source https://example.org/article.pdf \
  --url https://doi.org/10.xxxx/yyyy

# Avec titre et labels forcés
python pdf2readeck.py \
  --source article.pdf \
  --url https://doi.org/10.xxxx/yyyy \
  --title "Gajjala 2002 — Cyberethnographie féministe" \
  --labels cyberethnographie féminisme lecture
```

> En mode CLI, les labels sont séparés par des espaces (comportement argparse standard).

## Options

| Option | Court | Description |
|---|---|---|
| `--source` | `-s` | Chemin local (relatif/absolu) ou URL d'un PDF |
| `--url` | `-u` | URL ou DOI de citation |
| `--title` | `-t` | Titre du bookmark (remplace celui du PDF) |
| `--labels` | `-l` | Labels Readeck (séparés par des espaces en CLI) |
| `--version` | `-v` | Affiche la version |

---

## Ce que fait le script

1. Détecte si la source est un chemin local ou une URL et télécharge si nécessaire
2. Analyse le PDF et détecte les anomalies typiques :
   - **Filigranes rotatifs** (Taylor & Francis, Elsevier, Springer, JSTOR…) : propose de filtrer les caractères dont la matrice de transformation indique une rotation
   - **Mise en page multi-colonnes** : détecte une distribution bimodale des positions X et propose de réextraire en séparant les colonnes gauche/droite
3. Chaque patch est proposé individuellement et peut être refusé
4. Extrait la structure typographique (h1/h2/h3/p) via les tailles de fonte et les attributs bold
5. Extrait les images via PyMuPDF si disponible
6. Affiche le titre détecté dans les métadonnées pour validation
7. Génère un HTML propre et lisible
8. Envoie via `POST /api/bookmarks` en JSON avec le DOI/URL comme `url` et le HTML comme `html`
9. Récupère l'ID du bookmark dans le header `bookmark-id` de la réponse
10. Affiche l'URL directe vers le bookmark dans Readeck

---

## Changelog

### v0.2.2
- Détection interactive des anomalies PDF avec proposition de patchs :
  - Filtrage des filigranes rotatifs (Taylor & Francis, JSTOR, Elsevier…)
  - Détection de mise en page multi-colonnes par histogramme bimodal des positions X
  - Chaque correctif est proposé séparément et peut être refusé
- Saisie interactive des labels (séparés par des virgules)
- Détection de structure typographique (h1/h2/h3/p) via pdfplumber
- Interface terminal : dégradé violet sur fond terminal natif, spinner animé (✦ → ✧ → ⋆ → ✶ sur place), ✔ verts persistants
- Signature dans le header

### v0.2.1
- Interface terminal colorée (ANSI 256 couleurs)
- Spinner animé avec étoiles scintillantes
- Sections claires, prompts stylisés, résultat mis en valeur
- Gestion Ctrl+C propre

### v0.2.0
- Mode interactif : le script pose les questions si aucun argument n'est fourni
- Détection automatique du type de source (URL vs chemin local relatif/absolu)
- Validation du titre détecté avant envoi
- Correction de la récupération de l'ID : lu dans le header `bookmark-id`
- URL du bookmark récupérée dans le header `link`
- Passage au payload JSON

### v0.1.0
- Version initiale : extraction texte (pdfplumber), images (PyMuPDF optionnel), envoi multipart à Readeck
