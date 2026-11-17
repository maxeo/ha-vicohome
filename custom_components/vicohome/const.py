"""Constants for the VicoHome integration."""

import base64

DOMAIN = "vicohome"

CONF_REGION = "region"
CONF_COUNTRY = "country"
CONF_LANGUAGE = "language"

DEFAULT_REGION = "eu"
DEFAULT_COUNTRY = "IT"
DEFAULT_LANGUAGE = "it"
REGIONS = ["eu", "us"]

# Options (Configure button of the integration).
CONF_RECORD = "record"
CONF_RETENTION_DAYS = "retention_days"
# What can be kept locally: the event types of the cloud library plus the live views.
RECORD_CHOICES = ["ring", "person", "vehicle", "pet", "package", "motion", "live"]
DEFAULT_RECORD = RECORD_CHOICES
# Options of version 0.4.0 (two switches), still read as a fallback.
CONF_ARCHIVE_EVENTS = "archive_events"
CONF_RECORD_LIVE = "record_live"
DEFAULT_RETENTION_DAYS = 7
# Choices offered in the options; 0 = never delete (manual cleanup).
RETENTION_CHOICES = [1, 3, 7, 14, 30, 90, 180, 365, 0]
# Extra choice that asks for a number of days in a second step.
RETENTION_CUSTOM = "custom"
MAX_CUSTOM_DAYS = 3650

# Cloud polling. Reading the device list, cover images and the event library does not
# wake battery cameras.
UPDATE_INTERVAL_S = 30
# Events newer than this keep the "recent event" binary sensor on.
RECENT_EVENT_S = 120
# How far back the library is read on each poll.
LIBRARY_WINDOW_S = 24 * 3600

# Longest live session; battery cameras stay awake while it is open.
LIVE_MAX_S = 5 * 60

# Data channel name the HA frontend creates; the camera expects one in the offer.
DATA_CHANNEL = "dataChannel"

# Firebase Cloud Messaging identity of the VicoHome Android app (public values from the APK
# resources: google_app_id, gcm_defaultSenderId, project_id, google_api_key).
FCM_PROJECT_ID = "vicohome-2e0ac"
FCM_APP_ID = "1:744602285642:android:14399645bbc84abf"
FCM_SENDER_ID = "744602285642"
# Public key of the VicoHome Android app (it is in the APK), stored encoded so that it is not
# mistaken for a leaked secret of this project.
FCM_API_KEY = base64.b64decode("QUl6YVN5RE1FaHI5cFliUE9ja2NiYkIyNnZwN2JsR0VoNW45Z1Nr").decode()
FCM_BUNDLE_ID = "com.smartaddx.vicohome"
# msgType the app sends to /user/setusertoken for FCM tokens (A4x push manager).
PUSH_MSG_TYPE_FCM = "101"


def recorded_types(options) -> set[str]:  # noqa: ANN001
    """What the user chose to keep (falls back to the 0.4.0 switches)."""
    if CONF_RECORD in options:
        return set(options[CONF_RECORD])
    chosen = set(RECORD_CHOICES)
    if not options.get(CONF_ARCHIVE_EVENTS, True):
        chosen -= set(RECORD_CHOICES[:-1])
    if not options.get(CONF_RECORD_LIVE, True):
        chosen.discard("live")
    return chosen
