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
CONF_ARCHIVE_EVENTS = "archive_events"
CONF_RECORD_LIVE = "record_live"
CONF_RETENTION_DAYS = "retention_days"
DEFAULT_ARCHIVE_EVENTS = True
DEFAULT_RECORD_LIVE = True
DEFAULT_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 90

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
