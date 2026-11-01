from django.core.management.base import BaseCommand

from events.account_forms import prune_unconfirmed_accounts


class Command(BaseCommand):
    help = (
        "Löscht Konten, die beim Anfordern eines Anmeldecodes für eine "
        "unbekannte Adresse automatisch angelegt wurden (siehe "
        "events.account_forms.RequestLoginCodeOrSignupForm), bei denen aber "
        "nie der zugeschickte Code eingegeben wurde. Gedacht zum "
        "periodischen Aufruf per Cron."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Mindestalter in Tagen, ab dem ein unbestätigtes Konto gelöscht wird (Default: 7).",
        )

    def handle(self, *args, **options):
        deleted_count = prune_unconfirmed_accounts(days=options["days"])
        self.stdout.write(self.style.SUCCESS(f"{deleted_count} unbestätigte Konto(en) gelöscht."))
