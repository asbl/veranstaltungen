# Öffentliche Veranstaltungen vorbereiten

`Event.planning_enabled` aktiviert die Mitglieder-TODO-, Orga- und Werbelisten. Die Veranstaltung
kann bereits ohne Datum gespeichert werden. Sobald erstmals genau ein Tag
hinzukommt, werden die vorbereiteten Dienste diesem Tag zugeordnet.

- Orga und Werbung verwenden `PlanningItem` mit Kategorie, Status, Personen,
  „Wer hat es das letzte Mal gemacht“, Notizen und optionalem Link. Die Orga und
  Administratoren bearbeiten die Einträge. Namen sind Freitext, damit auch
  externe Helfer ohne Konto erfasst werden können. Das Feld „Wer hat es das
  letzte Mal gemacht“ dient als Orientierung für die Zuteilung und ist als
  Vorlage für die nächste Ausgabe der Veranstaltung verfügbar: „Aufgaben aus Vorlage übernehmen“.
- Mitbringen (auch Kuchen) verwendet `PotluckItem` und `PotluckClaim` in einer
  gemeinsamen Mitbringliste. Bei öffentlichen Veranstaltungen zählen Zusagen
  auch ohne Besuchsanmeldung. Die bisherige Zählweise anderer Veranstaltungen
  bleibt bestehen.
- Dienstplan, Aufbau und Abbau verwenden `Duty` und `DutySignup`. Die Orga legt
  zuerst ein Uhrzeitraster fest (Beginn, Ende, Stunden je Zeitraum; die Dauer
  ist frei wählbar, der letzte Zeitraum endet spätestens zur angegebenen
  Endzeit) und danach die Dienste: Jeder Dienst wird einmal je Zeitraum
  angelegt, zusätzlich optional ein Springer-Dienst je Zeitraum. Auf- und
  Abbau sind einzelne Dienste außerhalb des Rasters und ohne Springer; der
  Abbau darf versetzt liegen (z. B. 22–23 Uhr nach einem internen Teil).
  Jeder Dienst kann mehrere Helfer aufnehmen. Springer zählen nicht zur
  regulären Besetzung und sind nur im Dienstplan möglich.
- Alle neuen Endpunkte verwenden den bestehenden Veranstaltungsschutz;
  schreibende Aktionen benötigen Anmeldung und CSRF-Schutz. Die neue Markierung
  für öffentliche Veranstaltungen ändert keine bestehenden Zugangspasswörter.

## Taunus spielt 26

Nach der Schema-Migration bereitet `python manage.py prepare_taunus26` die
Veranstaltung vor. Der Befehl ist wiederholbar und überschreibt bestehende Listen
nicht; er setzt den Zugangsschlüssel aus `--access-code` bzw. der Umgebungsvariable `TAUNUS26_ACCESS_CODE`, falls noch keiner existiert (ohne Angabe bleibt die Veranstaltung ohne Passwort - Schlüssel werden nie im Code abgelegt).
Mit `--date YYYY-MM-DD` kann später ein bestätigtes Datum 2026 ergänzt werden;
alternativ über die Veranstaltungsbearbeitung.

Vorbereitet werden 13 Orga-Aufgaben, 9 TODOs (3 Anfragen, 3 Reminder und 3 Vorbereitungsaufgaben), 6 Werbekanäle und 1 Mitbring-Item.
Orga-Liste und TODO-Liste stehen direkt hintereinander vor „Dienste“. Aufgaben haben Verantwortliche und eine
auswählbare Zeitgruppe: Ein Jahr vorher, 6–8 Wochen vorher, 4–5 Wochen vorher,
2–3 Wochen vorher oder Kurz vor der Veranstaltung. Alle fünf Gruppen sind sichtbar;
Aufgaben ohne Zuordnung stehen unter „Zeitpunkt offen“. Aufgaben können in der Liste
abgehakt werden; das Häkchen speichert sofort den Datenbankstatus (`PlanningItem.status`).
Erledigte Zeilen erscheinen leicht ausgegraut, auch nach erneutem Laden. Ohne JavaScript
steht ein Speichern-Knopf bereit. Bestehende Mitgliederanfragen werden
mit ihrem Bearbeitungsstand aus der Orga-Liste verschoben. Reminder versenden keine
automatischen Nachrichten. Die
Orga- und Werbelisten starten ohne Personen und mit Status „Offen“ (rot
dargestellt); wer die Aufgabe zuletzt übernommen hat, steht als Orientierung im
Feld „Wer hat es das letzte Mal gemacht“ und wird in der Personen-Spalte als
Hinweis angezeigt, solange noch niemand eingetragen ist. Notizen enthalten nur
Informationen, die für die neue Ausgabe wieder relevant sind. Der Dienstplan wird als Uhrzeitraster
10–18 Uhr in 2-Stunden-Zeiträumen (10–12, 12–14, 14–16, 16–18 Uhr) angelegt:
Kasse, Essen/Getränke, Flohmarkt, Erklärbären und Catan-Turnier je Zeitraum,
zusätzlich ein Springer-Dienst je Zeitraum. Hinzu kommen Aufbau 09–10 Uhr und
Abbau 18–19 Uhr, beide ohne Springer. Dienste haben keine Beschreibungstexte;
der Personalbedarf bleibt bis zur Planung offen. Kuchen werden wie alle
Mitbring-Items von ihren Beiträgenden eingetragen; es werden keine Kuchen oder
Benutzerkonten erfunden.
Catan-Turnierzeit und Teilnehmerzahl werden in der Orga-Aufgabe geklärt, bevor
bei Bedarf ein bestehendes Angebot/Workshop mit Anmeldung angelegt wird.

Die verlinkte VHS-Seite nennt den 01.11.2026.

## Prüfen und installieren

```sh
docker compose exec -T web python manage.py test --noinput
docker compose exec -T web python manage.py makemigrations --check --dry-run
# Vor der Migration ein Datenbank-Backup erstellen.
docker compose exec -T web python manage.py migrate --noinput
docker compose exec -T web python manage.py prepare_taunus26
docker compose restart web cron
```

Neue Felder bestehender Tabellen haben Datenbank-Defaults, damit noch laufende
Worker der vorherigen Version während der Migration weiter schreiben können.
Die Migration entfernt oder verändert keine bestehenden Veranstaltungsinhalte.


## Listenfilter, Zuständigkeiten und Werbemaßnahmen

Spiele lassen sich nach fehlenden Mitbringern, fehlenden Erklärbären und eigenen
Mitbring-/Erklärzusagen filtern. Mehrere Schnellfilter werden gemeinsam angewendet.
Suche, Trefferzahl und Zurücksetzen helfen bei langen Listen. Ein direkter Spielelink
hebt Filter auf, wenn sie das Ziel verbergen. Die Sortierung verwendet unformatierte
Zahlen; fehlende Werte stehen in beiden Sortierrichtungen am Ende. Der Vollbildknopf
steht innerhalb der Vollbildansicht und wird nur bei Browserunterstützung angeboten.

Orga, Werbung und TODOs haben Status- und Personenfilter, einen Filter für fehlende
Zuständigkeit sowie eine Fortschrittsanzeige. Orga und Werbung zeigen offene Aufgaben
zuerst, erledigte zuletzt. Werbemaßnahmen können eine Fälligkeit, ein tatsächliches
Veröffentlichungsdatum und einen Link zur Veröffentlichung enthalten. Überfällige,
noch nicht erledigte Maßnahmen werden markiert.

TODO-Häkchen speichern per POST ohne Seitenneuladen. Die Rückmeldung bestätigt die
Speicherung; bei Fehlern bleibt der zuletzt bestätigte Zustand sichtbar. Ohne
JavaScript bleibt der Speichern-Knopf verfügbar und der Rücksprung führt zur Aufgabe.

## Gleichzeitige Änderungen und Verlauf

Bearbeiten, Abhaken und Löschen benötigen die angezeigte Revision des Eintrags.
Eine inzwischen geänderte Aufgabe wird nicht überschrieben oder gelöscht. Beim
Bearbeiten bleiben die eigenen Eingaben stehen; ein Link öffnet den aktuellen Stand
zum Vergleichen in einem neuen Tab. Status, Zuständigkeiten und andere Feldänderungen
werden zusammen mit der Speicherung atomar protokolliert. Den Änderungsverlauf
können ausschließlich die Orga der Veranstaltung und Administratoren einsehen.
Änderungen aus der Zeit vor dieser Erweiterung werden nicht nachträglich erfunden.

## Vorlagen für wiederkehrende Veranstaltungen

„Aufgaben aus Vorlage übernehmen“ zeigt die Planungsaufgaben einer anderen selbst
verwalteten Veranstaltung zunächst als Vorschau. Es werden Orga, Werbung und TODOs
übernommen; vorhandene Einträge bleiben erhalten. Bereits übernommene Aufgaben und
gleichnamige Aufgaben derselben Kategorie werden übersprungen, auch bei erneutem
Absenden. Standardmäßig werden Status auf „Offen“ und Zuständigkeiten auf leer
zurückgesetzt; beide Optionen können bewusst abgewählt werden. Vorherige
Verantwortliche bleiben als „zuletzt gemacht von“ erhalten.

Veröffentlichungsdatum und Veröffentlichungslink werden immer zurückgesetzt.
Fälligkeiten werden um den Abstand zwischen den ersten Tagen beider Veranstaltungen
verschoben. Fehlt bei einer Veranstaltung ein Termin, bleiben die Fälligkeiten offen.
Die ursprüngliche Veranstaltung wird nicht verändert.

## Isolierte Regressionstests

Die Testdatenbank kann über `DJANGO_TEST_DATABASE_NAME` separat benannt werden.
Tests auf demselben Datenbanknamen niemals gleichzeitig starten. Für eine frische
Arbeitskopie zuerst `python manage.py collectstatic --noinput` ausführen.

```sh
DJANGO_TEST_DATABASE_NAME=test_list_audit python manage.py test --noinput
# Optional: synthetische HTML-Fixture innerhalb der Testdatenbank exportieren:
DJANGO_TEST_DATABASE_NAME=test_list_audit AUDIT_HTML_PATH=/tmp/list-fixture.html \
  python manage.py test events.test_planning_audit.PlanningAuditTests.test_browser_fixture --noinput
python scripts/test_list_ui.py /tmp/list-fixture.html
```

Die Browserprüfung benötigt Python Playwright mit installiertem Chromium. Sie
verwendet ausschließlich synthetische Testdaten und simuliert Schreibantworten.
Die zugehörigen echten Endpunkte und ihre Berechtigungen prüft die Django-Testsuite.

## Werbung zum Mitmachen

Werbemaßnahmen können mit „Alle können mitmachen“ angelegt werden, etwa
„Im eigenen Signal-Status“ oder „Einen Flyer ins Auto hängen“. Verantwortlich
sind dann alle. Angemeldete Personen ergänzen mit „Habe ich gemacht“ ihren
Anzeigenamen unter „Schon gemacht“ und können nur ihren eigenen Eintrag wieder
zurücknehmen. Die Maßnahme bleibt für weitere Personen offen. Zugangsschutz
und Schreibschutz der Veranstaltung gelten auch hier. Beim Übernehmen aus einer
Vorlage bleibt die Option erhalten; Teilnahmen werden nicht kopiert.

Der Status von Mitmach-Aktionen zeigt ab der ersten Teilnahme die Anzahl der
Personen in Gelb. Ohne Teilnahmen steht dort „Zum Mitmachen“. Die Statusfilter
behandeln Aktionen mit Teilnahmen als „In Arbeit“, ohne Teilnahmen als „Offen“.
Beim Zurücknehmen wird der Status aus den verbleibenden Teilnahmen neu bestimmt.

Bei Verantwortlichkeiten und gewöhnlichen Werbemaßnahmen ergänzt „Ich übernehme das“ den eigenen Anzeigenamen
unter Verantwortlich. Bestehende Namen bleiben erhalten; erneute Klicks ergänzen
denselben Namen nicht doppelt. Die Änderung wird im Orga-Verlauf protokolliert
und schützt über die Revision vor dem Überschreiben durch ältere Bearbeitungsformulare.


## Werbeorte und erledigte Werbung melden

Angemeldete Teilnehmende können mit „Werbeort / Aktion hinzufügen“ einen Ort
anlegen. Das kurze Formular erfasst bereits erledigte Werbung, Datum, weitere
Mitwirkende und Notiz. Das standardmäßig gesetzte Häkchen „Mich selbst als verantwortlich / mitwirkend eintragen“ ergänzt den eigenen Anzeigenamen. Ohne Häkchen werden nur die angegebenen Personen eingetragen; ohne Namen bleibt die Zuständigkeit offen. Das gilt auch für Rückmeldungen zu bestehenden Orten. Notizen und Änderungsverlauf nennen weiterhin die tatsächlich eintragende Person.
Bei bestehenden Orten öffnet „Hier habe ich Werbung gemacht“ die Rückmeldung.
Bestehende Namen und Notizen bleiben erhalten; Änderungen werden protokolliert
und gegen veraltete Formularstände geschützt. Gleichnamige Orte werden beim
Anlegen abgefangen. Allgemeines Bearbeiten und Löschen bleibt der Orga vorbehalten.
Zugangsschutz, Schreibschutz und deaktivierte Listen gelten auch für diesen Ablauf.
Auf Handys erscheinen Werbeorte als einzelne Karten mit direkter Rückmeldung.

Fristen, Veröffentlichungsdatum und Link im Werbeplanungsformular stehen unter
„Erweiterte Optionen“. Veröffentlichung und Werbekanal verwenden ein gemeinsames
Linkfeld. Frühere Veröffentlichungslinks werden beim Bearbeiten übernommen;
ein abweichender zweiter Bestandslink bleibt sichtbar und erhalten.

Der Status in den Planungslisten öffnet per Klick ein kurzes Statusformular.
Die Orga darf alle gewöhnlichen Planungsstatus bearbeiten; bei Werbung und
Verantwortlichkeiten dürfen außerdem Ersteller und eingetragene Verantwortliche
den Status ändern. Mitmachaktionen behalten ihren berechneten Status.
Revision, Zugangsschutz und Schreibschutz werden geprüft; andere Felder können
über das Statusformular nicht geändert werden. Die Werberückmeldung erscheint
als kleiner Textlink statt als hervorgehobener Button.


Status und Verantwortliche lassen sich direkt aus der Liste in einem Popup
bearbeiten. „Noch offen“ öffnet die Namenseingabe. Bei unbesetzten Werbe- und
Verantwortlichkeitseinträgen können angemeldete Personen Namen ergänzen;
besetzte Einträge bearbeiten die Orga, Ersteller und eingetragene Verantwortliche.
Ohne persönliche Anmeldung zeigt das Popup einen Anmeldelink; das
Veranstaltungspasswort allein gibt keine Schreibrechte. Ohne JavaScript führen
die Links zum entsprechenden Formular. Abbrechen und Escape speichern nichts;
Fehler und Versionskonflikte behalten die Eingaben im Popup.

Browserprüfung mit synthetischen Daten:
`PLANNING_POPUP_FIXTURE=/tmp/planning-popup-fixture.json python manage.py test events.test_planning_status --noinput`
und anschließend `python scripts/test_planning_popups_ui.py /tmp/planning-popup-fixture.json`.

## Werbe-Highscore und Aliase

Der Highscore in der Werbeliste zählt je Person einen Punkt pro erledigtem
Werbeort bzw. pro Teilnahme an einer Mitmach-Aktion. Bei gemeinsamen Aktionen
bekommt jede Person einen Punkt; Namen können durch Komma, Semikolon, Zeilenumbruch,
„und“ oder „&“ getrennt sein. Offene Zuständigkeiten zählen nicht. Gleiche
Punktzahlen erhalten denselben Platz. Rücknahmen und Statusänderungen wirken
sich beim nächsten Laden aus.

Die Orga kann mit „Aliase hinzufügen“ mehrere Namen derselben Person für diese
Veranstaltung zusammenführen, beispielsweise „m.muster“ und „Erika Muster“.
Die Zuordnung und ihre Varianten sind unter dem Highscore sichtbar und lassen
sich bearbeiten oder aufheben. Originaleinträge bleiben erhalten. Eine Person
zählt je Aktion auch bei mehreren Aliasnamen nur einmal. Ein Name kann nur
einer Zuordnung angehören. Bei Konto-Teilnahmen werden Anzeigename und
Benutzername für die Zuordnung berücksichtigt.

## Kompakte Werbelisten

Unter dem unveränderten Highscore stehen Mitmach-Aktionen separat. Die übrigen
Werbeorte erscheinen kompakt nach „Offen“, „In Arbeit“ und „Fertig“ gruppiert;
„Wartet“ steht mit entsprechender Kennzeichnung unter „In Arbeit“. Jeder Eintrag
zeigt Ort, Verantwortliche, für die Orga Bearbeiten-/Löschen-Icons sowie den
Rückmeldelink. Auf breiten Bildschirmen stehen mehrere Einträge nebeneinander.
Die bisherige Tabelle inklusive Notizen, Terminen, Links und Filtern bleibt unter
„Detaillierte Werbeliste“ eingeklappt verfügbar. Rücksprünge zu einzelnen
Werbeaktionen führen zur kompakten Ansicht.
