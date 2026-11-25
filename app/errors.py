# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

from dataclasses import dataclass


@dataclass(frozen=True)
class Result:
    status_code: int
    body: dict


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def fail(status_code: int, code: str, message: str, **extra: object) -> Result:
    body: dict = {"error": code, "message": message}
    body.update(extra)
    return Result(status_code, body)
