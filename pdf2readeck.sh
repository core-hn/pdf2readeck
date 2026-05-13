#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="pdf2readeck"

# Charger conda
if [ -f "$HOME/opt/miniconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/opt/miniconda3/etc/profile.d/conda.sh"
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif [ -f "/opt/homebrew/Caskroom/miniconda/base/etc/profile.d/conda.sh" ]; then
    source "/opt/homebrew/Caskroom/miniconda/base/etc/profile.d/conda.sh"
elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/anaconda3/etc/profile.d/conda.sh"
else
    echo "Erreur : conda introuvable. Vérifie ton installation miniconda/anaconda."
    exit 1
fi

# Créer l'environnement s'il n'existe pas
if ! conda env list | grep -q "^${ENV_NAME} "; then
    echo "Environnement conda '${ENV_NAME}' introuvable — création en cours..."
    conda env create -f "$SCRIPT_DIR/environment.yml"
    if [ $? -ne 0 ]; then
        echo "Erreur : échec de la création de l'environnement."
        exit 1
    fi
fi

conda activate "$ENV_NAME"

python "$SCRIPT_DIR/pdf2readeck.py" "$@"
