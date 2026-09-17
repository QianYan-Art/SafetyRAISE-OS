class HarnessError(Exception):
    def __init__(self, code: str, status_code: int = 409, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.details = details
