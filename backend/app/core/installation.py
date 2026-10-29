"""Installation contracts shared by startup, API, and worker."""

BOOTSTRAP_LOCK_ID = 728130401
PROVIDER_LOCK_ID = 728130402
MIN_OWNER_PASSWORD_LENGTH = 12
MAX_OWNER_PASSWORD_BYTES = 72
REQUIRED_SCHEMA_REVISION = "feedback_mirror_20261029"
WORKER_HEARTBEAT_MAX_AGE_SECONDS = 120


class InstallationError(Exception):
    def __init__(self, code, message, status_code=409, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def body(self):
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
