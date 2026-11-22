"""Main-thread termination intent, deferred across initialization and teardown."""

import signal


class TerminationSignals:
    """Records TERM/INT without asynchronous exceptions inside cleanup or constructor code."""

    def __init__(self):
        """Creates main-thread-owned intent and original-handler storage."""
        self.requested = False
        self._previous = {}

    def _request(self, _number, _frame):
        """Only records intent; the HTTP safe-point or startup return drives closing."""
        self.requested = True

    def __enter__(self):
        """Installs deferred handlers before any runtime lock or listener is acquired."""
        try:
            for number in (signal.SIGTERM, signal.SIGINT):
                self._previous[number] = signal.signal(number, self._request)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, _kind, _value, _traceback):
        """Restores caller handlers only after startup or shutdown has finished unwinding."""
        for number, handler in self._previous.items():
            signal.signal(number, handler)
