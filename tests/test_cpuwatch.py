"""관리 프로그램이 순간적으로 CPU 를 많이 쓰면 범인 스레드·함수가 로그에 남는다."""

import logging
import threading
import time

from livemax import cpuwatch


def _busy_work_for_test(until: float) -> None:
    x = 0
    while time.monotonic() < until:
        x += 1


def test_cpuwatch_names_the_busy_thread(caplog):
    stop = threading.Event()
    w = threading.Thread(target=cpuwatch.run, args=(stop, 0.3, 0.3, 0.0), daemon=True)
    with caplog.at_level(logging.WARNING, logger="livemax.cpuwatch"):
        w.start()
        busy = threading.Thread(target=_busy_work_for_test, args=(time.monotonic() + 1.5,), name="범인스레드")
        busy.start()
        busy.join()
        time.sleep(0.4)
        stop.set()
        w.join(2)
    text = caplog.text
    assert "관리 프로그램 순간 CPU" in text
    assert "범인스레드" in text and "_busy_work_for_test" in text
