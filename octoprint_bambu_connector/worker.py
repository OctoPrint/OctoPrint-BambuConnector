import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("octoprint.plugins.bambu_connector.worker")


class AsyncTaskWorker:
    def __init__(self):
        self._running = threading.Event()

        self._thread = threading.Thread(
            target=self.run,
            name=str(self.__class__),
        )
        self._thread.daemon = True
        logger.info(f"Starting thread {self._thread.name}")
        self._thread.start()
        self._running.wait()

    def _set_running(self):
        self._running.set()

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.set_default_executor(
            ThreadPoolExecutor(thread_name_prefix="bambu_worker")
        )
        self.loop.call_soon(self._set_running)
        self.loop.run_forever()

    def shutdown(self, **kwargs):
        logger.warning("shutdown initiated")
        self.loop.stop()
        self.loop.close()
        self._thread.join()

    def run_coroutine_threadsafe(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)
