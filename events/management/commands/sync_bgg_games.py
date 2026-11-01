import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from events import bgg
from events.models import GameEntry


class Command(BaseCommand):
    help = "Aktualisiert einen kleinen Batch lokal gecachter BGG-Spieldaten."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=20)
        parser.add_argument("--max-age-days", type=int, default=7)

    def handle(self, *args, **options):
        batch_size = min(max(options["batch_size"], 1), bgg.MAX_IDS_PER_REQUEST)
        stale_before = timezone.now() - datetime.timedelta(days=options["max_age_days"])
        games = list(
            GameEntry.objects.exclude(bgg_id__isnull=True)
            .filter(bgg_synced_at__lt=stale_before)
            .order_by("bgg_synced_at", "pk")[:batch_size]
        )
        if len(games) < batch_size:
            known_ids = {game.pk for game in games}
            games.extend(
                GameEntry.objects.exclude(bgg_id__isnull=True)
                .filter(bgg_synced_at__isnull=True)
                .exclude(pk__in=known_ids)
                .order_by("pk")[: batch_size - len(games)]
            )
        if not games:
            self.stdout.write("Keine BGG-Daten zu aktualisieren.")
            return
        try:
            results = bgg.fetch_bgg_games(game.bgg_id for game in games)
        except bgg.BggLookupError as exc:
            self.stderr.write(str(exc))
            return
        synced_at = timezone.now()
        updated = 0
        for game in games:
            data = results.get(game.bgg_id)
            if not data:
                continue
            game.min_players = data["min_players"]
            game.max_players = data["max_players"]
            game.playtime_minutes = data["playtime_minutes"]
            game.min_age = data["min_age"]
            game.bgg_year = data["year"]
            game.complexity = data["complexity"]
            game.bgg_rating = data["rating"]
            game.bgg_description = data["description"]
            game.bgg_categories = data["categories"]
            game.bgg_mechanics = data["mechanics"]
            game.thumbnail_url = data["thumbnail_url"]
            game.bgg_synced_at = synced_at
            game.save(update_fields=["min_players", "max_players", "playtime_minutes", "min_age", "bgg_year", "complexity", "bgg_rating", "bgg_description", "bgg_categories", "bgg_mechanics", "thumbnail_url", "bgg_synced_at"])
            updated += 1
        self.stdout.write(self.style.SUCCESS(f"{updated} BGG-Spiel(e) aktualisiert."))
