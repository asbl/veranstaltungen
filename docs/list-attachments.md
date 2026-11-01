# Anhänge an Listenbeschreibungen

Unter Veranstaltung bearbeiten → Listen → Einstellungen können mehrere Bilder
(PNG, JPEG, GIF, WebP) und PDFs ausgewählt werden. Pro Datei gelten maximal 20 MB,
pro Liste und Speichervorgang maximal zehn neue Dateien. Bestehende Anhänge
bleiben erhalten, bis sie ausdrücklich zum Entfernen ausgewählt werden.

Bilder erscheinen als Vorschau; alle Anhänge bieten einen Download. PDF-Dateien
werden als Download ausgeliefert. Dateiendung und Dateisignatur werden geprüft.
Dateien werden unter zufälligen Namen gespeichert, die Anzeigenamen bleiben erhalten.

`ListAttachment` hält Veranstaltung, Listenschlüssel und Dateiverweis. Dateien
liegen in `MEDIA_ROOT` (`/app/media`, per bestehendem Bind-Mount persistent im
Projektverzeichnis). Der Zugriff erfolgt ausschließlich über
`/<event-id>/anhaenge/<attachment-id>/`, mit derselben Zugangsprüfung wie die
Veranstaltung. Anhänge ausgeblendeter Listen können nur Veranstaltungsverwalter
abrufen. Antworten werden nicht zwischengespeichert.

Beim Entfernen eines Anhangs oder Löschen der Veranstaltung wird nach erfolgreicher
Datenbanktransaktion auch die Datei gelöscht. Für die Wiederherstellung müssen
sowohl die Datenbank als auch das Verzeichnis `media/` gesichert werden.
