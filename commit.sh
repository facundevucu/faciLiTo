#!/bin/bash
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo 'Uso: ./commit.sh "mensaje del commit"'
    exit 1
fi

cd /home/ubuntu/asistente-lito
git add bot.py dashboard.py backup.sh commit.sh .gitignore .env.example VERSION CONTRIBUTING.md
git commit -m "$1"
git push origin main
echo "Commit y push completados."
