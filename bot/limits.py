class ParseLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._hits: dict[int, list[float]] = {}

    def allow(self, telegram_id: int, now: float) -> bool:
        window = [stamp for stamp in self._hits.get(telegram_id, []) if now - stamp < 60]
        if len(window) >= self.limit:
            self._hits[telegram_id] = window
            return False
        window.append(now)
        self._hits[telegram_id] = window
        return True
