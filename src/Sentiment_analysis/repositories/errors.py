"""Safe repository error codes, shared without importing job orchestration."""


class JobError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
