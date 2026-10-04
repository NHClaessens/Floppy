"""Write an episode rating.

A rating belongs to the episode, not to one viewing of it, so every play row
of the episode carries the same score (``Episode.save`` copies it onto a new
replay). The season page and the media-server webhooks all write through here
so they agree on that rule and on the History refresh it needs.
"""

import logging

from django.utils import timezone

from app import history_cache
from app.models import Episode

logger = logging.getLogger(__name__)


def tracked_episode_plays(user, media_id, source, season_number, episode_number):
    """Return the user's play rows of one episode, across library buckets."""
    return Episode.objects.filter(
        related_season__user=user,
        item__media_id=str(media_id),
        item__source=source,
        item__season_number=season_number,
        item__episode_number=episode_number,
    )


def set_episode_score(episodes, score, user_id):
    """Set ``score`` on every play in ``episodes``; return how many changed.

    ``update()`` skips post_save, so the Episode signal that refreshes the
    History cache never fires. Invalidate the affected days here instead.
    """
    # Only plays whose score differs, so a retried request is not a new rating.
    episodes = episodes.exclude(score=score)
    end_dates = list(episodes.values_list("end_date", flat=True))
    updated = episodes.update(score=score, scored_at=timezone.now())

    day_keys = [history_cache.history_day_key(end_date) for end_date in end_dates]
    day_keys = [day_key for day_key in day_keys if day_key]
    if day_keys:
        history_cache.invalidate_history_days(
            user_id,
            day_keys=day_keys,
            logging_styles=("sessions", "repeats"),
            reason="episode_score_change",
        )
    return updated
