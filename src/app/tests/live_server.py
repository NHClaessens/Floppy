"""A browser-test live server that handles one request at a time.

The test database is SQLite in memory, so Django hands the live server the
test thread's single connection. Its default threaded server then runs
overlapping browser requests (a page plus its htmx, manifest or service
worker fetches) on that one connection at once, and SQLite answers with
"bad parameter or other API misuse": a random 500 that failed a different
Playwright test on each CI run. Serving requests one at a time removes the
overlap; Django closes each connection after the response when the server
is not threaded, so the browser cannot hold it open.
"""

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.servers.basehttp import WSGIServer
from django.test.testcases import LiveServerThread, QuietWSGIRequestHandler


class SerialLiveServerThread(LiveServerThread):
    """Live server thread whose server is not threaded."""

    def _create_server(self, connections_override=None):
        # run() has already pointed this thread at the shared connections, and
        # every request is handled on this thread.
        return WSGIServer(
            (self.host, self.port),
            QuietWSGIRequestHandler,
            allow_reuse_address=False,
        )


class SerialStaticLiveServerTestCase(StaticLiveServerTestCase):
    """StaticLiveServerTestCase that serves one request at a time."""

    server_thread_class = SerialLiveServerThread
