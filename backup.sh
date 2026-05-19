#!/bin/bash
set -euo pipefail

BACKUP_DIR="/home/ubuntu/backups"
DB_PATH="/home/ubuntu/asistente-lito/recaudaciones.db"
LOG_FILE="${BACKUP_DIR}/backup.log"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DEST="${BACKUP_DIR}/recaudaciones_${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"

if cp "${DB_PATH}" "${DEST}"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') OK — ${DEST}" >> "${LOG_FILE}"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR — no se pudo copiar DB" >> "${LOG_FILE}"
    exit 1
fi

# Mantener solo los últimos 30 backups
ls -t "${BACKUP_DIR}"/recaudaciones_*.db 2>/dev/null | tail -n +31 | xargs -r rm --
DELETED=$(ls -t "${BACKUP_DIR}"/recaudaciones_*.db 2>/dev/null | wc -l)
echo "$(date '+%Y-%m-%d %H:%M:%S') Total backups: ${DELETED}" >> "${LOG_FILE}"
