"""Shared project circuit; quota rolls over, credentials require operator repair."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from src.Sentiment_analysis.repositories.dynamo import Change
from src.Sentiment_analysis.repositories.errors import JobError

KEY = "CIRCUIT#YOUTUBE"
PACIFIC = ZoneInfo("America/Los_Angeles")


def next_quota_reset(now):
    tomorrow = now.astimezone(PACIFIC).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, PACIFIC).astimezone(timezone.utc)


class ProviderCircuit:
    def __init__(self, store, *, clock, telemetry):
        self.store, self.clock, self.telemetry = store, clock, telemetry

    def assert_available(self):
        item = self.store.get(KEY)
        if not item or not item.data["open"]:
            return item
        if item.data["reason"] == "quota" and item.data["blocked_until"] <= int(self.clock().timestamp()):
            return item  # Expiry is checked strongly, never delegated to TTL.
        self.telemetry.emit("provider_circuit", {"ProviderCircuitBlockedCount": 1}, outcome="retry")
        raise JobError("YOUTUBE_QUOTA_EXHAUSTED" if item.data["reason"] == "quota" else "DEPENDENCY_UNAVAILABLE")

    @staticmethod
    def condition(item):
        # Pin the state read by admission/spend to the same transaction. If it
        # changes, the retry must check availability again before spending.
        return Change(KEY, item.version if item else None, None, check_only=True)

    def open(self, reason):
        if reason not in {"quota", "credentials"}:
            raise ValueError("Only verified project quota/credential faults trip this circuit")
        for _ in range(8):
            item = self.store.get(KEY)
            now = self.clock()
            if item and item.data["open"] and item.data["reason"] == "credentials":
                return
            until = int(next_quota_reset(now).timestamp()) if reason == "quota" else None
            if item and item.data["open"] and item.data["reason"] == reason and item.data["blocked_until"] == until:
                return
            value = {"open": True, "reason": reason, "opened_at": int(now.timestamp()), "blocked_until": until}
            if self.store.commit([Change(KEY, item.version if item else None, value)]):
                self.telemetry.emit("provider_circuit", {"ProviderCircuitOpenedCount": 1},
                    error_code="YOUTUBE_QUOTA_EXHAUSTED" if reason == "quota" else "PROVIDER_CREDENTIALS_INVALID")
                return
        raise JobError("SYSTEM_BUSY")

    def close_after_credentials_repaired(self, *, expected_version):
        """Internal operator action after repairing the restricted key and restarting workers."""
        item = self.store.get(KEY)
        if not item or item.version != expected_version or item.data["reason"] != "credentials":
            raise ValueError("Re-read and repair the credential circuit before closing it")
        if not self.store.commit([Change(KEY, item.version, {**item.data, "open": False})]):
            raise JobError("SYSTEM_BUSY")
