"""Local-only telemetry shim.

The research fork never sends usage, identity, feature-flag, or memory metadata
to a remote telemetry service.  The public no-op functions are retained so the
memory algorithm can stay unchanged.
"""

MEM0_TELEMETRY = False
MEM0_TELEMETRY_SAMPLE_RATE = 0.0


class AnonymousTelemetry:
    """Compatibility object used by the existing notice helpers."""

    def __init__(self, vector_store=None, before_send=None):
        self.posthog = None
        self.user_id = None

    def capture_event(self, event_name, properties=None, user_email=None, flags=None):
        return None

    def capture_identify(self, anon_id, email):
        return False

    def close(self):
        return None


_disabled_telemetry = AnonymousTelemetry()
client_telemetry = _disabled_telemetry


def _sampling_before_send(message):
    return None


def _get_oss_telemetry():
    return _disabled_telemetry


def _shutdown_oss_telemetry():
    return None


def capture_event(event_name, memory_instance, additional_data=None):
    return None


def capture_client_event(event_name, instance, additional_data=None):
    return None
