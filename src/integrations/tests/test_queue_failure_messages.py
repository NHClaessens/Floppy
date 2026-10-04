"""Import views name an unreachable Redis instead of blaming the worker (#1263)."""

from unittest import mock

from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, SimpleTestCase, override_settings
from kombu.exceptions import OperationalError

from integrations import views

DNS_TEXT = "Error -2 connecting to redis:6379. Name does not resolve."


@override_settings(CELERY_BROKER_URL="redis://redis:6379/0")
class QueueTaskOrMessageTests(SimpleTestCase):
    def _request(self):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda _request: None).process_request(request)
        request._messages = FallbackStorage(request)
        return request

    def _messages(self, request):
        return [str(message) for message in get_messages(request)]

    def test_unresolvable_broker_names_redis_and_the_network(self):
        request = self._request()
        task = mock.Mock()
        task.delay.side_effect = OperationalError(DNS_TEXT)

        with self.assertLogs("integrations.views", level="ERROR"):
            queued = views._queue_task_or_message(request, task, user_id=1)

        self.assertIs(queued, False)
        [message] = self._messages(request)
        self.assertIn("The import could not be queued.", message)
        self.assertIn("cannot reach Redis at redis:6379", message)
        self.assertIn("does not resolve", message)
        self.assertNotIn("worker", message)

    def test_staged_task_failure_uses_the_same_message(self):
        request = self._request()
        with (
            mock.patch.object(
                views,
                "enqueue_staged_task",
                side_effect=OperationalError(DNS_TEXT),
            ),
            self.assertLogs("integrations.views", level="ERROR"),
        ):
            queued = views._queue_staged_task_or_message(request, mock.Mock())

        self.assertIs(queued, False)
        [message] = self._messages(request)
        self.assertIn("cannot reach Redis at redis:6379", message)

    def test_other_queue_failures_keep_the_worker_hint(self):
        request = self._request()
        task = mock.Mock()
        task.delay.side_effect = RuntimeError("serializer exploded")

        with self.assertLogs("integrations.views", level="ERROR"):
            views._queue_task_or_message(request, task)

        self.assertEqual(
            self._messages(request),
            ["The import could not be queued. Check the worker and try again."],
        )
