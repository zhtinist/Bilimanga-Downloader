"""共享请求节流器：最小间隔 + 有限并发 + 命中限流后的全体冷却。

站点（哔哩轻小说 / bilimanga）会在短时突发后返回 429，并需要一段冷却才恢复。
命中限流时调用 :meth:`penalize`，让**所有线程**一起停等一段冷却并自适应拉长间隔，
避免每个请求各自疯狂重试把情况拖得更糟。轻小说引擎与漫画网络层共用本类。
"""

from __future__ import annotations

import threading
import time


class RateGate:
    def __init__(self, min_interval: float, concurrency: int,
                 max_interval: float = 6.0):
        self._base = max(0.0, min_interval)
        self._min = self._base
        self._max = max_interval
        self._sem = threading.Semaphore(max(1, concurrency))
        self._lock = threading.Lock()
        self._next = 0.0
        self._cooldown_until = 0.0

    def acquire(self) -> None:
        self._sem.acquire()
        while True:
            with self._lock:
                now = time.monotonic()
                wait = max(self._next - now, self._cooldown_until - now)
                if wait <= 0:
                    self._next = now + self._min
                    return
            time.sleep(min(wait, 3.0))  # 分段睡，便于响应中断

    def release(self) -> None:
        self._sem.release()

    def penalize(self, cooldown: float) -> None:
        """命中限流：全体停等 ``cooldown`` 秒，并把稳态间隔调大一点。"""
        with self._lock:
            self._cooldown_until = max(self._cooldown_until,
                                       time.monotonic() + cooldown)
            self._min = min(self._min * 1.5 + 0.1, self._max)

    def reward(self) -> None:
        """连续成功：把间隔缓慢收回，恢复速度。"""
        with self._lock:
            if self._min > self._base:
                self._min = max(self._base, self._min * 0.9)
