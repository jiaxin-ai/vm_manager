"""各模块共用的小工具：错误类型、按键加锁、时间格式。"""
import threading
from datetime import datetime, timedelta, timezone

GIB = 1024 ** 3
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ApiError(Exception):
    """由全局异常处理器转换为 {"error": {"code", "message", "details"}}。"""

    def __init__(self, http_status, code, message, details=None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.details = details


class KeyedLocks:
    """每个 key 一把 threading.Lock，按需创建（8.2）。"""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks = {}

    def get(self, key):
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock


def utc_now():
    return datetime.now(timezone.utc)


def to_iso(dt):
    return dt.astimezone(timezone.utc).strftime(TIME_FORMAT) if dt else None


def from_iso(text):
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc) if text else None


def add_seconds(dt, seconds):
    return dt + timedelta(seconds=seconds)
