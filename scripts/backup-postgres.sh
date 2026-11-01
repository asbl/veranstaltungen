#!/usr/bin/env bash
set -euo pipefail
# Optionaler Guard-Wrapper (host-spezifisch). Ueber BACKUP_GUARD konfigurierbar;
# ohne Guard laeuft das Skript direkt.
if [[ ${BACKUP_GUARD_ACTIVE:-} != 1 && -n ${BACKUP_GUARD:-} && -x ${BACKUP_GUARD} ]]; then
  exec "$BACKUP_GUARD" backup-veranstaltungen "$0" "$@"
fi

# Datenbank-Dumps enthalten personenbezogene Daten und sind nur fuer den
# ausfuehrenden Benutzer lesbar.
umask 077

MODE="${1:-daily}"
case "$MODE" in
  daily)
    KEEP_COUNT="${KEEP_DAILY_COUNT:-2}"
    ;;
  weekly)
    KEEP_COUNT="${KEEP_WEEKLY_COUNT:-1}"
    ;;
  *)
    printf 'Usage: %s {daily|weekly}\n' "$0" >&2
    exit 2
    ;;
esac

if [[ ! "$KEEP_COUNT" =~ ^[1-9][0-9]*$ ]]; then
  printf 'Retention count must be a positive integer, got: %s\n' "$KEEP_COUNT" >&2
  exit 2
fi

BACKUP_DIR="${BACKUP_DIR:-./backups}"
LOG_FILE="${LOG_FILE:-./logs/db-backup-cron.log}"
MAX_LOG_BYTES="${MAX_LOG_BYTES:-1048576}"
CONTAINER="${POSTGRES_CONTAINER:-veranstaltungen_db}"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
FINAL_FILE="$BACKUP_DIR/veranstaltungen-${MODE}-${TIMESTAMP}.sql.gz"
PARTIAL_FILE="${FINAL_FILE}.partial"
LOCK_FILE="$BACKUP_DIR/.backup.lock"

if [[ ! "$MAX_LOG_BYTES" =~ ^[1-9][0-9]*$ ]]; then
  printf 'MAX_LOG_BYTES must be a positive integer, got: %s\n' "$MAX_LOG_BYTES" >&2
  exit 2
fi

mkdir -p "$BACKUP_DIR" "$(dirname "$LOG_FILE")"
chmod 700 "$BACKUP_DIR"

# Auch das Cron-Log bleibt begrenzt: maximal das aktuelle und ein vorheriges
# Log mit jeweils rund 1 MiB.
if [[ -f "$LOG_FILE" ]] && (( $(stat -c %s "$LOG_FILE") >= MAX_LOG_BYTES )); then
  rm -f "${LOG_FILE}.1"
  mv "$LOG_FILE" "${LOG_FILE}.1"
fi

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"
}

# Ein manueller Lauf soll nicht mit einem Cron-Lauf kollidieren.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Backup skipped: another run is active"
  exit 0
fi

cleanup() {
  rm -f "$PARTIAL_FILE"
}
trap cleanup EXIT

log "Starting $MODE database backup -> $FINAL_FILE"

# Benutzer und Datenbankname kommen aus der bereits im DB-Container gesetzten
# Compose-Umgebung; Zugangsdaten werden dadurch weder dupliziert noch geloggt.
docker exec "$CONTAINER" sh -c \
  'exec pg_dump --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  | gzip -9 > "$PARTIAL_FILE"

# Ein defekter oder leerer Stream wird nicht in die Rotation aufgenommen.
gzip -t "$PARTIAL_FILE"
if [[ ! -s "$PARTIAL_FILE" ]]; then
  log "Backup failed: dump is empty"
  exit 1
fi

mv "$PARTIAL_FILE" "$FINAL_FILE"
trap - EXIT

# Pro Kategorie nur die gewuenschte Anzahl neuester Sicherungen behalten.
# Damit bleibt der Platzverbrauch auch nach jahrelangem Betrieb begrenzt.
mapfile -t backups < <(
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    -name "veranstaltungen-${MODE}-*.sql.gz" -printf '%f\n' | sort -r
)
for ((index = KEEP_COUNT; index < ${#backups[@]}; index++)); do
  expired="$BACKUP_DIR/${backups[$index]}"
  log "Removing expired backup: $expired"
  rm -f -- "$expired"
done

# Reste sehr alter, abgebrochener Prozesse ebenfalls begrenzen.
find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.partial' -mtime +1 -delete

retained_count="${#backups[@]}"
if (( retained_count > KEEP_COUNT )); then
  retained_count="$KEEP_COUNT"
fi
log "Backup finished: $(du -h "$FINAL_FILE" | cut -f1), retained $retained_count $MODE backup(s)"
