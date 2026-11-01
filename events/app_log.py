"""Technisches Protokoll der Anwendung (logs/app.jsonl).

Eine JSON-Zeile je Eintrag. Geschrieben wird es von der RequestAuditMiddleware
(jede Formular-Absendung und jede Fehlerantwort) und von ``django.request``
(Tracebacks bei Serverfehlern). ``send_error_report`` liest neue Einträge ab
Stufe WARNING, verschickt sie gesammelt per Mail, rotiert die Datei täglich
und löscht rotierte Dateien nach ``APP_LOG_RETENTION_DAYS``.
"""
import datetime
import json
import logging
import os
from pathlib import Path

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

REPORT_LEVELS = {"WARNING", "ERROR", "CRITICAL"}
MAX_ENTRIES_IN_MAIL = 50


def request_info(request):
    user = getattr(request, "user", None)
    return {
        "method": request.method,
        "path": request.path[:500],  # ohne Query-String (kann Zugangscodes enthalten)
        "user": user.get_username() if user is not None and user.is_authenticated else None,
    }


class JsonLineFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "time": timezone.localtime(datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # django.request übergibt das Request-Objekt; die Middleware liefert
        # ihre Felder schon fertig als "audit".
        request = getattr(record, "request", None)
        if request is not None and hasattr(request, "method"):
            entry.update(request_info(request))
        entry.update(getattr(record, "audit", {}))
        if record.exc_info:
            entry["traceback"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def log_path():
    return Path(settings.APP_LOG_FILE)


def _state_path():
    return log_path().with_name(".error-report-state.json")


def _read_state():
    try:
        return json.loads(_state_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _write_state(state):
    temp = _state_path().with_suffix(".tmp")
    temp.write_text(json.dumps(state))
    os.replace(temp, _state_path())


def _read_entries(path, offset):
    """Liest vollständige Zeilen ab ``offset``; liefert Einträge und neuen Offset."""
    entries = []
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except FileNotFoundError:
        return entries, offset
    complete = data[: data.rfind(b"\n") + 1]
    for line in complete.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            entries.append({"level": "ERROR", "message": line.decode("utf-8", "replace")})
    return entries, offset + len(complete)


def _rotate_if_due(today):
    """Benennt app.jsonl einmal täglich um; liefert den neuen Namen oder None."""
    path = log_path()
    if not path.exists():
        return None
    file_day = datetime.date.fromtimestamp(path.stat().st_mtime)
    first_line = path.open("rb").readline()
    try:
        file_day = datetime.date.fromisoformat(json.loads(first_line)["time"][:10])
    except (ValueError, KeyError, TypeError):
        pass
    if file_day >= today:
        return None
    target = path.with_name(f"app-{file_day.isoformat()}.jsonl")
    counter = 1
    while target.exists():
        target = path.with_name(f"app-{file_day.isoformat()}-{counter}.jsonl")
        counter += 1
    os.replace(path, target)
    return target


def _prune(today):
    cutoff = today - datetime.timedelta(days=settings.APP_LOG_RETENTION_DAYS)
    for old in log_path().parent.glob("app-*.jsonl"):
        try:
            day = datetime.date.fromisoformat(old.name[4:14])
        except ValueError:
            continue
        if day < cutoff:
            old.unlink()


def _format_entry(entry):
    head = f"{entry.get('time', '?')}  {entry.get('level', '?')}  {entry.get('status', '')}".rstrip()
    lines = [head]
    who = entry.get("user") or "nicht angemeldet"
    if entry.get("path"):
        lines.append(f"  {entry.get('method', '')} {entry['path']}  ({who})")
    lines.append(f"  {entry.get('message', '')}")
    for text in entry.get("user_messages", []):
        lines.append(f"  Meldung an Nutzer: {text}")
    if entry.get("traceback"):
        lines.append("  " + entry["traceback"].replace("\n", "\n  "))
    return "\n".join(lines)


def send_error_report(today=None):
    """Verschickt neue Warnungen und Fehler; liefert die Anzahl gemeldeter Einträge."""
    today = today or timezone.localdate()
    path = log_path()
    state = _read_state()
    offset = state.get("offset", 0)
    try:
        if path.stat().st_size < offset:
            offset = 0  # Datei wurde außerhalb dieses Befehls ersetzt.
    except FileNotFoundError:
        offset = 0

    entries, offset = _read_entries(path, offset)
    reported = _mail_problems(entries)
    # Bei Mailfehlern bricht der Lauf oben ab, der Offset bleibt stehen und
    # der nächste Lauf versucht es erneut.
    _write_state({"offset": offset})

    rotated = _rotate_if_due(today)
    if rotated:
        # Zeilen, die zwischen Lesen und Umbenennen geschrieben wurden.
        entries, _ = _read_entries(rotated, offset)
        _write_state({"offset": 0})
        reported += _mail_problems(entries)
    _prune(today)
    return reported


def _mail_problems(entries):
    problems = [entry for entry in entries if entry.get("level") in REPORT_LEVELS]
    recipients = settings.ERROR_REPORT_EMAILS
    if not problems or not recipients:
        return len(problems)
    shown = problems[-MAX_ENTRIES_IN_MAIL:]
    body = "\n\n".join(_format_entry(entry) for entry in shown)
    if len(problems) > len(shown):
        body = f"{len(problems) - len(shown)} ältere Einträge ausgelassen.\n\n" + body
    body += f"\n\nVollständiges Protokoll: {settings.APP_LOG_HOST_PATH}\n"
    send_mail(
        f"[Veranstaltungen] {len(problems)} Fehler/Warnung(en)",
        body,
        settings.DEFAULT_FROM_EMAIL,
        recipients,
    )
    return len(problems)
