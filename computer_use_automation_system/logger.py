from __future__ import annotations

import json


class Logger:
    def _log(self, level: str, message: str, data: dict | None = None) -> None:
        payload = f" {json.dumps(data, ensure_ascii=False)}" if data else ""
        print(f"[{level.upper()}] {message}{payload}")

    def info(self, message: str, data: dict | None = None) -> None:
        self._log("info", message, data)

    def warn(self, message: str, data: dict | None = None) -> None:
        self._log("warn", message, data)

    def error(self, message: str, data: dict | None = None) -> None:
        self._log("error", message, data)
