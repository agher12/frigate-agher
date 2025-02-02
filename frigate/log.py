import atexit
import logging
import multiprocessing as mp
import os
import sys
import threading
from collections import deque
from logging.handlers import QueueHandler, QueueListener
from typing import Deque, Optional

from frigate.util.builtin import clean_camera_user_pass
from log_rate_limit import StreamRateLimitFilter, RateLimit

##PAQUETE PRINCIPAL
logger = logging.getLogger("frigate")
logger.setLevel(logging.INFO)

#Rate-limit Filter
rate_limit_filter = StreamRateLimitFilter(period_sec=30)
logger.addFilter(rate_limit_filter)


LOG_HANDLER = logging.StreamHandler()
LOG_HANDLER.setFormatter(
    logging.Formatter(
        "[%(asctime)s] %(name)-30s %(levelname)-8s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
)

LOG_HANDLER.addFilter(
    lambda record: not record.getMessage().startswith(
        "You are using a scalar distance function"
    )
)
logger.addHandler(LOG_HANDLER)

log_listener: Optional[QueueListener] = None


def setup_logging() -> None:
    global log_listener

    log_queue: mp.Queue = mp.Queue()
    log_listener = QueueListener(log_queue, LOG_HANDLER, respect_handler_level=True)

    atexit.register(_stop_logging)
    log_listener.start()

    logging.basicConfig(
        level=logging.INFO,
        handlers=[],
        force=True,
    )

    logging.getLogger().addHandler(QueueHandler(log_listener.queue))


def _stop_logging() -> None:
    global log_listener

    if log_listener is not None:
        log_listener.stop()
        log_listener = None


# When a multiprocessing.Process exits, python tries to flush stdout and stderr. However, if the
# process is created after a thread (for example a logging thread) is created and the process fork
# happens while an internal lock is held, the stdout/err flush can cause a deadlock.
#
# https://github.com/python/cpython/issues/91776
def reopen_std_streams() -> None:
    sys.stdout = os.fdopen(1, "w")
    sys.stderr = os.fdopen(2, "w")


os.register_at_fork(after_in_child=reopen_std_streams)


# based on https://codereview.stackexchange.com/a/17959
class LogPipe(threading.Thread):
    def __init__(self, log_name: str):
        """Setup the object with a logger and start the thread"""
        super().__init__(daemon=False)
        self.logger = logging.getLogger(log_name)
        self.level = logging.ERROR
        self.deque: Deque[str] = deque(maxlen=100)
        self.fdRead, self.fdWrite = os.pipe()
        self.pipeReader = os.fdopen(self.fdRead)
        self.start()

    def cleanup_log(self, log: str) -> str:
        """Cleanup the log line to remove sensitive info and string tokens."""
        log = clean_camera_user_pass(log).strip("\n")
        return log

    def fileno(self) -> int:
        """Return the write file descriptor of the pipe"""
        return self.fdWrite

    def run(self) -> None:
        """Run the thread, logging everything."""
        for line in iter(self.pipeReader.readline, ""):
            self.deque.append(self.cleanup_log(line))

        self.pipeReader.close()

    def dump(self) -> None:
        while len(self.deque) > 0:
            self.logger.log(self.level, self.deque.popleft())

    def close(self) -> None:
        """Close the write end of the pipe."""
        os.close(self.fdWrite)

# Log general messages and messages by type with rate limiting
def log_general_and_types():
    logger.info("This is a general log message, nor rate-limited.")

    # Log messages with rate limiting by type
    for _ in range(3):
        logger.debug("Debug message (debug)", extra=RateLimit(stream_id="debug"))
        logger.info("Informational message (info)", extra=RateLimit(stream_id="info"))
        logger.warning("Warning message (warning)", extra=RateLimit(stream_id="warning"))
        logger.error("Error message (error)", extra=RateLimit(stream_id="error"))