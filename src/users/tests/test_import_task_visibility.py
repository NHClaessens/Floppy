from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django_celery_beat.models import CrontabSchedule, PeriodicTask

from config.celery import app
from integrations import tasks


class StremioPeriodicImportVisibilityTests(TestCase):
    """Regression coverage for Stremio periodic-import visibility."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="stremio-visibility",
            password="12345",
        )
        self.crontab = CrontabSchedule.objects.create(
            minute="0",
            hour="0",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
        )

    def test_disabled_stremio_schedule_is_not_reported_or_mutated(self):
        """Reading import status must ignore and preserve a disabled schedule."""
        kwargs = f'{{"user_id": {self.user.id}}}'
        periodic_task = PeriodicTask.objects.create(
            name="Import from Stremio for test (every 2 hours)",
            task="Import from Stremio (Recurring)",
            kwargs=kwargs,
            crontab=self.crontab,
            enabled=False,
        )

        import_tasks = self.user.get_import_tasks()

        self.assertEqual(import_tasks["schedules"], [])

        periodic_task.refresh_from_db()
        self.assertFalse(periodic_task.enabled)
        self.assertEqual(periodic_task.task, "Import from Stremio (Recurring)")
        self.assertEqual(periodic_task.crontab_id, self.crontab.id)
        self.assertEqual(periodic_task.kwargs, kwargs)


class ImportTaskRegistryConsistencyTests(TestCase):
    """Guard the runtime schedule query against unmapped recurring imports."""

    def test_every_registered_recurring_import_is_queried(self):
        """Every registered recurring import must reach the schedule lookup."""
        # A worker populates its registry by importing the task modules at
        # boot. Nothing in a test process does that, and ``integrations.tasks``
        # only imports its implementations on attribute access, so reading
        # ``app.tasks`` first would compare against an empty registry.
        app.loader.import_default_modules()
        recurring_task_names = {
            name for name in app.tasks if name.endswith("(Recurring)")
        }
        self.assertIn(tasks.import_stremio_recurring.name, recurring_task_names)

        user = get_user_model().objects.create_user(
            username="registry-check",
            password="12345",
        )
        with patch("users.models.PeriodicTask.objects.filter") as periodic_filter:
            periodic_filter.return_value.select_related.return_value = []
            user.get_import_tasks()

        queried_schedule_names = set(periodic_filter.call_args.kwargs["task__in"])
        missing = recurring_task_names - queried_schedule_names
        self.assertEqual(
            missing,
            set(),
            "Recurring import task(s) are registered with Celery but missing from "
            "the Active Periodic Imports schedule lookup: "
            f"{sorted(missing)}.",
        )
