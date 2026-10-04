"""Remove duplicate watch entries left by the Jellyfin webhook and import.

The webhook timestamps a play at playback-stop; the history import
timestamps the same play at playback-start. Before issue #1162 was fixed,
the history import never checked whether the webhook had already recorded a
play, so running both against the same Jellyfin server left every rewatched
item with an extra `Episode` row (or `MoviePlay` row) a few minutes to a few
hours apart. This command finds and removes those already-created
duplicates; the underlying bug is fixed separately in
`integrations.imports.jellyfin_playback_reporting`.

Two watches of the same item are duplicates when they fall within the same
runtime-scaled window `app.fork_services_play_dedupe` uses everywhere else
duplicate plays are detected, so this command agrees with that logic rather
than inventing its own notion of "duplicate".

Read-only by default. Pass --apply to write.
"""

from collections import defaultdict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from app.fork_services_play_dedupe import duplicate_play_window
from app.models import Episode, Movie, MoviePlay

MIN_CLUSTER_SIZE = 2


class Command(BaseCommand):
    """Collapse duplicate Jellyfin watch entries."""

    help = (
        "Remove duplicate movie/episode watch entries left behind when both "
        "the Jellyfin webhook and the history import recorded the same play"
    )

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes (default is a read-only report)",
        )
        parser.add_argument(
            "--username",
            type=str,
            default=None,
            help="Only process entries belonging to this user",
        )

    def handle(self, *_args, **options):
        """Execute the command."""
        self.apply = options["apply"]

        users = get_user_model().objects.all()
        if options["username"]:
            users = users.filter(username=options["username"])
        users = list(users.order_by("username"))
        if not users:
            self.stdout.write("No matching users.")
            return

        mode = "APPLYING" if self.apply else "DRY RUN (no changes written)"
        self.stdout.write(f"{mode}\n")

        removed = 0
        for user in users:
            removed += self._process_episodes(user)
            removed += self._process_movies(user)

        if not removed:
            self.stdout.write("No duplicate Jellyfin watch entries found.")
            return

        verb = "removed" if self.apply else "to remove"
        self.stdout.write(f"\nDuplicate rows {verb}: {removed}")
        if not self.apply:
            self.stdout.write(
                self.style.WARNING("\nRe-run with --apply to write these changes."),
            )

    def _process_episodes(self, user):
        """Collapse duplicate Episode rows for one user."""
        groups = defaultdict(list)
        episodes = Episode.objects.filter(
            related_season__user=user,
            end_date__isnull=False,
        ).select_related("item")
        for episode in episodes:
            item = episode.item
            key = (item.source, item.media_id, item.season_number, item.episode_number)
            groups[key].append(episode)

        removed = 0
        for episodes in groups.values():
            runtime_minutes = episodes[0].item.runtime_minutes
            for cluster in self._cluster(episodes, runtime_minutes):
                removed += self._collapse_episode_cluster(user, cluster)
        return removed

    def _process_movies(self, user):
        """Collapse duplicate MoviePlay rows for one user."""
        removed = 0
        movies = Movie.objects.filter(user=user).select_related("item")
        for movie in movies:
            plays = list(
                MoviePlay.objects.filter(movie=movie, end_date__isnull=False),
            )
            clusters = self._cluster(plays, movie.item.runtime_minutes)
            movie_removed = sum(len(cluster) - 1 for cluster in clusters)
            for cluster in clusters:
                self._describe_movie_cluster(movie, cluster)
            if movie_removed and self.apply:
                with transaction.atomic():
                    for cluster in clusters:
                        self._collapse_movie_cluster(movie, cluster)
                    self._sync_movie_end_date(movie)
            removed += movie_removed
        return removed

    def _sync_movie_end_date(self, movie):
        """Point Movie.end_date back at a play that still exists.

        Deleting the play `Movie.end_date` was copied from would otherwise
        leave it naming a watch that no longer has a row behind it.
        """
        latest = (
            MoviePlay.objects.filter(movie=movie, end_date__isnull=False)
            .order_by("-end_date")
            .first()
        )
        latest_end_date = latest.end_date if latest else None
        if movie.end_date != latest_end_date:
            movie.end_date = latest_end_date
            movie.save(update_fields=["end_date"])

    def _cluster(self, rows, runtime_minutes):
        """Group rows into duplicate clusters against retained keepers.

        Each row joins the first existing cluster whose keeper (the
        earliest row in it) falls within the runtime-scaled window, rather
        than chaining adjacent rows transitively - a 00:00/00:50/01:40 chain
        with a 60-minute window must not collapse into one cluster, since
        00:00 and 01:40 are not duplicates of each other under
        `app.fork_services_play_dedupe.PlayTimes.is_duplicate`.
        """
        rows = sorted(rows, key=lambda row: row.end_date)
        window = duplicate_play_window(runtime_minutes)

        clusters = []
        for row in rows:
            match = next(
                (
                    cluster
                    for cluster in clusters
                    if (row.end_date - cluster[0].end_date) < window
                ),
                None,
            )
            if match is not None:
                match.append(row)
            else:
                clusters.append([row])
        return [cluster for cluster in clusters if len(cluster) >= MIN_CLUSTER_SIZE]

    def _pick_keeper(self, cluster):
        """Keep the row with the most provenance, breaking ties by age."""

        def sort_key(row):
            has_operation_id = getattr(row, "watch_operation_id", None) or getattr(
                row,
                "external_id",
                None,
            )
            return (has_operation_id is None, row.created_at)

        ordered = sorted(cluster, key=sort_key)
        return ordered[0], ordered[1:]

    def _collapse_episode_cluster(self, user, cluster):
        keeper, losers = self._pick_keeper(cluster)
        item = keeper.item
        self.stdout.write(
            f"{user.username}: {item.title} S{item.season_number:02d}"
            f"E{item.episode_number:02d} - {len(losers)} duplicate watch(es), "
            f"keeping #{keeper.pk} ({keeper.end_date})",
        )
        if self.apply:
            with transaction.atomic():
                Episode.objects.filter(
                    pk__in=[loser.pk for loser in losers],
                ).delete()
        return len(losers)

    def _describe_movie_cluster(self, movie, cluster):
        keeper, losers = self._pick_keeper(cluster)
        self.stdout.write(
            f"{movie.user.username}: {movie.item.title} - "
            f"{len(losers)} duplicate watch(es), keeping play #{keeper.pk} "
            f"({keeper.end_date})",
        )

    def _collapse_movie_cluster(self, movie, cluster):
        _keeper, losers = self._pick_keeper(cluster)
        MoviePlay.objects.filter(pk__in=[loser.pk for loser in losers]).delete()
