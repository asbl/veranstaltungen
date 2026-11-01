import logging

from django.contrib import messages

from events.app_log import request_info

audit_logger = logging.getLogger("events.audit")


class RequestAuditMiddleware:
    """Protokolliert jede Formular-Absendung und jede Fehlerantwort.

    So lässt sich später nachvollziehen, ob z. B. eine Dienst-Eintragung
    beim Server angekommen ist und was die Person als Antwort bekam.
    404 wird nur bei angemeldeten Personen protokolliert (sonst Bot-Rauschen).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        status = response.status_code
        # Noch nicht vom MessageMiddleware weggeschrieben, weil es weiter außen sitzt.
        queued = getattr(getattr(request, "_messages", None), "_queued_messages", [])
        user_errors = [str(message) for message in queued if message.level >= messages.ERROR]
        info = request_info(request)

        if status == 404 and not info["user"]:
            return response
        if status >= 500:
            level, text = logging.ERROR, "Serverfehler"
        elif status >= 400:
            level, text = logging.WARNING, "Anfrage abgelehnt"
        elif user_errors:
            level, text = logging.WARNING, "Fehlermeldung angezeigt"
        elif request.method == "POST":
            level, text = logging.INFO, "Absendung verarbeitet"
        else:
            return response

        info["status"] = status
        if response.get("Location"):
            info["location"] = response["Location"][:500]
        if user_errors:
            info["user_messages"] = user_errors
        reason = getattr(response, "reason_phrase", "")
        audit_logger.log(level, "%s: %s %s", text, status, reason, extra={"audit": info})
        return response
