"""Provider episode identities and auditable personal order changes."""

from django.conf import settings
from django.db import models


class EpisodeOrder(models.Model):
    """An immutable provider catalogue used by order-local items."""

    show = models.ForeignKey("app.Item", on_delete=models.PROTECT, related_name="episode_orders")
    provider = models.CharField(max_length=20)
    series_id = models.CharField(max_length=500)
    key = models.CharField(max_length=255)
    label = models.CharField(max_length=255)
    catalogue = models.JSONField(default=dict)
    revision = models.CharField(max_length=64)

    class Meta:
        """Keep each immutable provider catalogue revision unique."""

        constraints = [models.UniqueConstraint(
            fields=["show", "provider", "series_id", "key", "revision"],
            name="app_episode_order_revision_unique",
        )]

    def __str__(self):
        """Return the user-facing provider order label."""
        return f"{self.label} ({self.provider})"

    @property
    def media_id(self):
        """Return the internal identity consumed by metadata dispatch."""
        return f"order_{self.pk}"


class EpisodeOrderChange(models.Model):
    """Keep original rows and explicit user resolutions for reconciliation."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    tv = models.ForeignKey("app.TV", on_delete=models.CASCADE)
    order = models.ForeignKey(EpisodeOrder, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    mappings = models.JSONField(default=list)
    before_state = models.JSONField(default=dict)

    def __str__(self):
        """Return the audited order change label."""
        return f"{self.tv} -> {self.order}"
