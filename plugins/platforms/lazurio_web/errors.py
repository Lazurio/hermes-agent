"""Request errors of the ``lazurio_web`` runtime API (kept free of aiohttp so the adapter imports
it without the HTTP stack)."""


class ApiError(Exception):
    """An HTTP error answer: ``{"error": {"code", "message"}}`` with ``status``."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code
