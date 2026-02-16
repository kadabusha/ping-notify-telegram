#!/usr/bin/env python3
"""
Tuya power monitor with ping-first detection, API fallback and Telegram alerts.
"""

import os
import time
import json
import hmac
import hashlib
import logging
import subprocess
import requests

# ===================== ENV (DO NOT RENAME) =====================

TUYA_REGION = os.getenv("TUYA_REGION", "eu")
TUYA_ACCESS_ID = os.environ["TUYA_ACCESS_ID"]
TUYA_ACCESS_KEY = os.environ["TUYA_ACCESS_KEY"]
TUYA_DEVICE_ID = os.environ["TUYA_DEVICE_ID"]

DISABLE_DEVICE_IDS = [d.strip() for d in os.getenv("DISABLE_DEVICE_IDS", "").split(",") if d.strip()]  # noqa: E501  # pylint: disable=C0301

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_CHAT_ID_DEBUG = os.environ.get("TELEGRAM_CHAT_ID_DEBUG")

INTERVAL = int(os.getenv("INTERVAL", "60"))
JITTER = float(os.getenv("JITTER", "2"))

# ping (from .txt)
PING_TARGET_HOST = os.getenv("PING_TARGET_HOST")
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "10"))
PING_CONFIRM_COUNT = int(os.getenv("PING_CONFIRM_COUNT", "10"))
API_OFFLINE_INTERVAL = int(os.getenv("API_OFFLINE_INTERVAL", "300"))
API_RETRY_DELAY = int(os.getenv("API_RETRY_DELAY", "30"))

BASE_URL = f"https://openapi.tuya{TUYA_REGION}.com"

# ===================== LOGGING =====================

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","event":%(message)s}',
)
log = logging.getLogger("tuya-monitor")


def jlog(level, **payload):
    """Structured JSON logger."""
    log.log(level, json.dumps(payload, ensure_ascii=False))


# ===================== TELEGRAM =====================

def telegram_main(text):
    """Send normal notification."""
    if not TELEGRAM_CHAT_ID:
        return

    try:
        jlog(logging.INFO, event="api_call", service="telegram")
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except requests.RequestException:
        pass

    # ---- duplicate to debug channel (disable later if not needed) ----
    if TELEGRAM_CHAT_ID_DEBUG:
        try:
            jlog(logging.INFO, event="api_call", service="telegram_debug")
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID_DEBUG,
                    "text": text,
                },
                timeout=10,
            )
        except requests.RequestException:
            pass


def telegram_debug(text):
    """Send debug/error notification."""
    if not TELEGRAM_CHAT_ID_DEBUG:
        return

    try:
        jlog(logging.INFO, event="api_call", service="telegram_debug")
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID_DEBUG,
                "text": text,
            },
            timeout=10,
        )
    except requests.RequestException:
        pass


def notify_error(source, err, payload=None):
    """Structured error notifier."""
    msg = {
        "source": source,
        "error": str(err),
    }

    if payload:
        msg["payload"] = payload

    telegram_debug(
        f"🐞 ERROR\n{json.dumps(msg, ensure_ascii=False, indent=2)}"
    )


# ===================== PING =====================

def ping_once(host):
    """
    Single ICMP ping check.

    Returns:
        True  - ping ok
        False - ping failed
        None  - ping unavailable / error running ping
    """
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        jlog(logging.WARNING, event="ping_error", host=host, error=str(exc))
        return None


# pylint: disable=too-many-nested-blocks
def ping_confirm(host, count=None):
    """
    Confirm outage using a single ping invocation with multiple packets.
    Returns True if ANY packet is received.
    """
    try:
        # -c N: send N packets
        # -i 1: 1s between packets (avoid flood)
        # -W 1: 1s per-reply timeout (Linux ping)
        use_count = PING_CONFIRM_COUNT if count is None else int(count)
        cmd = ["ping", "-c", str(use_count), "-i", "1", "-W", "1", host]
        jlog(
            logging.INFO,
            event="ping_confirm_run",
            host=host,
            cmd=" ".join(cmd),
        )

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

        output = result.stdout or ""
        # Typical summary line (Linux):
        # "10 packets transmitted, 0 received, 100% packet loss, time 9017ms"
        received = None
        for line in output.splitlines():
            if "packets transmitted" in line and "received" in line:
                parts = [p.strip() for p in line.split(",")]
                for part in parts:
                    if part.endswith("received"):
                        # "0 received" -> 0
                        try:
                            received = int(part.split()[0])
                        except (ValueError, IndexError):
                            received = None
                break

        # Fallback: if we couldn't parse, approximate from returncode:
        # 0 usually means some received; 1 means none received (Linux ping)
        if received is None:
            jlog(
                logging.WARNING,
                event="ping_confirm_parse_failed",
                host=host,
                returncode=result.returncode,
            )
            return result.returncode == 0

        jlog(
            logging.INFO,
            event="ping_confirm_summary",
            host=host,
            transmitted=use_count,
            received=received,
            returncode=result.returncode,
        )
        return received > 0

    except (OSError, subprocess.SubprocessError) as exc:
        jlog(logging.WARNING, event="ping_confirm_error", host=host, error=str(exc))  # noqa: E501  # pylint: disable=C0301
        return None


def confirm_offline_status(client, host, device_id, last_online):
    """
    Delayed-retry confirmation: run 3 multi-packet ping confirmations,
    then API check if needed.

    Returns:
        True if online, False if offline, or last_online if API fails
        (keep state).
    """
    # Run 3 confirmatory ping checks. If any succeeds, we're online.
    for attempt in range(1, 4):
        jlog(
            logging.INFO,
            event="confirm_offline_attempt",
            attempt=attempt,
            host=host,
            packets=PING_CONFIRM_COUNT,
        )
        ping_result = ping_confirm(host, PING_CONFIRM_COUNT)
        if ping_result is True:
            jlog(
                logging.INFO,
                event="confirm_offline_result",
                host=host,
                status="online_via_ping",
            )
            return True  # Online

    # All pings failed. Verify with API call.
    jlog(
        logging.INFO,
        event="confirm_offline_via_api",
        device=device_id,
    )
    try:
        online = client.get_device_online(
            device_id,
            reason="offline_confirmation",
        )
        jlog(
            logging.INFO,
            event="confirm_offline_result",
            device=device_id,
            status="online" if online else "offline",
        )
        return online
    except (requests.RequestException, RuntimeError) as exc:
        jlog(
            logging.WARNING,
            event="confirm_offline_api_error",
            device=device_id,
            error=str(exc),
        )
        telegram_debug(
            f"⚠️ API Error during offline check\n"
            f"Device: {device_id}\nError: {str(exc)[:200]}"
        )
        # API error -> keep current state
        return last_online


# ===================== TUYA CLIENT =====================

class TuyaClient:
    """Minimal Tuya REST client."""

    def __init__(self):
        self.token = None
        self.token_expire = 0

    @staticmethod
    def _ts():
        return str(int(time.time() * 1000))

    @staticmethod
    def _sha256(body):
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _sign(self, method, path, body="", access_token=None):
        t = self._ts()
        body_hash = self._sha256(body)

        sign_str = (
            TUYA_ACCESS_ID
            + (access_token or "")
            + t
            + method.upper()
            + "\n"
            + body_hash
            + "\n\n"
            + path
        )

        signature = hmac.new(
            TUYA_ACCESS_KEY.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().upper()

        return t, signature

    def refresh_token(self):
        """Refresh Tuya API token."""
        path = "/v1.0/token?grant_type=1"
        t, sign = self._sign("GET", path)

        jlog(logging.INFO, event="api_call", method="GET", url=BASE_URL + path)

        r = requests.get(
            BASE_URL + path,
            headers={
                "client_id": TUYA_ACCESS_ID,
                "sign": sign,
                "t": t,
                "sign_method": "HMAC-SHA256",
            },
            timeout=10,
        )

        data = r.json()

        if not data.get("success"):
            notify_error("tuya_token_refresh", "failed", data)
            raise RuntimeError(data)

        self.token = data["result"]["access_token"]
        expire_time = int(data["result"]["expire_time"])
        # Tuya API returns expire_time in seconds
        # Apply 2-minute buffer before actual expiry to avoid edge cases
        self.token_expire = time.time() + expire_time - 120
        jlog(
            logging.INFO,
            event="tuya_token_refreshed",
            expire_time_sec=expire_time,
            valid_for_sec=expire_time - 120,
        )

    def ensure_token(self):
        """Ensure valid token."""
        now = time.time()
        time_until_expiry = self.token_expire - now if self.token else -1
        if self.token and now < self.token_expire:
            jlog(
                logging.INFO,
                event="token_check",
                result="valid",
                time_until_expiry_sec=int(time_until_expiry),
            )
            return
        jlog(
            logging.INFO,
            event="token_check",
            result="refreshing",
            has_token=bool(self.token),
            time_until_expiry_sec=int(time_until_expiry),
        )
        self.refresh_token()

    def get_device_online(self, device_id, reason=None):
        """Fetch device online state."""
        self.ensure_token()
        path = f"/v1.0/devices/{device_id}"
        t, sign = self._sign("GET", path, access_token=self.token)

        jlog(
            logging.INFO,
            event="api_call",
            method="GET",
            url=BASE_URL + path,
            reason=reason,
        )

        r = requests.get(
            BASE_URL + path,
            headers={
                "client_id": TUYA_ACCESS_ID,
                "access_token": self.token,
                "sign": sign,
                "t": t,
                "sign_method": "HMAC-SHA256",
            },
            timeout=10,
        )

        data = r.json()

        if not data.get("success"):
            notify_error("tuya_status", "failed", data)
            raise RuntimeError(data)

        result = data.get("result")
        if not result or "online" not in result:
            raise RuntimeError("online_field_missing")

        return result["online"]

    def set_switch(self, device_id, value):
        """Set device relay."""
        self.ensure_token()
        path = f"/v1.0/devices/{device_id}/commands"
        body = json.dumps({
            "commands": [{"code": "switch_1", "value": value}],
        })

        t, sign = self._sign("POST", path, body, access_token=self.token)

        jlog(
            logging.INFO,
            event="api_call",
            method="POST",
            url=BASE_URL + path,
            device=device_id,
        )

        try:
            r = requests.post(
                BASE_URL + path,
                data=body,
                headers={
                    "client_id": TUYA_ACCESS_ID,
                    "access_token": self.token,
                    "sign": sign,
                    "t": t,
                    "sign_method": "HMAC-SHA256",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            data = r.json()
        except requests.RequestException as exc:
            notify_error("tuya_switch_request", exc, {"device": device_id})
            raise
        except ValueError as exc:
            # JSON parse error
            error_text = getattr(r, "text", None)
            notify_error(
                "tuya_switch_response_parse",
                exc,
                {"device": device_id, "text": error_text},
            )
            raise RuntimeError(exc) from exc

        if not data.get("success"):
            msg = str(data).lower()
            if "already" in msg or "offline" in msg:
                return
            notify_error("tuya_switch", "failed", data)
            raise RuntimeError(data)  # pylint: disable=raise-missing-from

        jlog(
            logging.INFO,
            event="tuya_switch_set",
            device=device_id,
            value=value,
        )


# ===================== MAIN LOOP =====================

# pylint: disable=too-many-branches,too-many-statements,too-many-nested-blocks
def main():
    """Main monitoring loop."""
    client = TuyaClient()
    client.ensure_token()

    last_state = None
    outage_handled = False
    restore_handled = False
    last_api_check = time.time()
    last_api_retry_time = None  # Track when to retry after offline confirmed

    jlog(logging.INFO, event="monitor_started")

    while True:
        try:
            online = None
            now = time.time()

            # ---- PING PRIMARY DETECTION ----
            if PING_TARGET_HOST:
                ping_ok = ping_once(PING_TARGET_HOST)
                jlog(
                    logging.INFO,
                    event="probe_ping_once",
                    host=PING_TARGET_HOST,
                    result=ping_ok,
                )

                if ping_ok is True:
                    # Ping succeeded, we're online
                    online = True
                    jlog(
                        logging.INFO,
                        event="probe_decision",
                        source="ping",
                        online=online,
                    )

                elif ping_ok is False:
                    # Ping failed. Confirm with multiple attempts.
                    online = confirm_offline_status(
                        client,
                        PING_TARGET_HOST,
                        TUYA_DEVICE_ID,
                        last_state,
                    )
                    jlog(
                        logging.INFO,
                        event="probe_decision",
                        source="ping_confirm",
                        online=online,
                    )

                else:
                    # Ping command unavailable, fall back to API
                    online = client.get_device_online(
                        TUYA_DEVICE_ID,
                        reason="ping_unavailable",
                    )
                    jlog(
                        logging.INFO,
                        event="probe_decision",
                        source="tuya_api_fallback",
                        online=online,
                        reason="ping_unavailable",
                    )
            else:
                # Ping disabled, use API only
                online = client.get_device_online(
                    TUYA_DEVICE_ID,
                    reason="ping_disabled",
                )
                jlog(
                    logging.INFO,
                    event="probe_decision",
                    source="tuya_api",
                    online=online,
                    reason="ping_disabled",
                )

            # ---- DELAYED RETRY AFTER OFFLINE CONFIRMATION ----
            # If we recently confirmed offline and waited API_RETRY_DELAY,
            # retry the whole failure detection process.
            if (
                last_api_retry_time is not None
                and now >= last_api_retry_time
            ):
                jlog(
                    logging.INFO,
                    event="offline_retry_attempt",
                    host=PING_TARGET_HOST,
                    device=TUYA_DEVICE_ID,
                )
                # Re-run the failure detection process
                online = confirm_offline_status(
                    client,
                    PING_TARGET_HOST,
                    TUYA_DEVICE_ID,
                    last_state,
                )
                last_api_retry_time = None  # Reset for next retry cycle
                jlog(
                    logging.INFO,
                    event="probe_decision",
                    source="offline_retry",
                    online=online,
                )

                if not online:
                    # Still offline, schedule another retry
                    last_api_retry_time = now + API_RETRY_DELAY
                    jlog(
                        logging.INFO,
                        event="offline_still_confirmed",
                        schedule_next_retry=last_api_retry_time,
                    )

            # ---- API SAFETY CHECK WHEN OFFLINE ----
            if not online and (now - last_api_check) > API_OFFLINE_INTERVAL:
                jlog(
                    logging.INFO,
                    event="probe_api_safety_check_start",
                    device=TUYA_DEVICE_ID,
                    offline_interval=API_OFFLINE_INTERVAL,
                )
                try:
                    online = client.get_device_online(
                        TUYA_DEVICE_ID,
                        reason="offline_safety_check",
                    )
                    last_api_check = now
                    jlog(
                        logging.INFO,
                        event="probe_api_safety_check_result",
                        device=TUYA_DEVICE_ID,
                        online=online,
                    )
                    # If confirmed offline, schedule retry
                    if not online:
                        last_api_retry_time = now + API_RETRY_DELAY
                except (requests.RequestException, RuntimeError) as exc:
                    jlog(
                        logging.WARNING,
                        event="probe_api_safety_check_failed",
                        device=TUYA_DEVICE_ID,
                        error=str(exc),
                    )
                    telegram_debug(
                        f"⚠️ API Error (safety check)\n"
                        f"Device: {TUYA_DEVICE_ID}\n"
                        f"Error: {str(exc)[:200]}"
                    )
                    # API error: keep current state, schedule retry
                    online = last_state
                    last_api_retry_time = now + API_RETRY_DELAY

            jlog(logging.INFO, event="heartbeat", online=online)

            if last_state is None:
                last_state = online
                outage_handled = not online
                restore_handled = online
                time.sleep(PING_INTERVAL)
                continue

            # Detect state change and reset handled flags accordingly
            if online != last_state:
                if online:
                    # Transitioned to online
                    restore_handled = False
                    outage_handled = True
                else:
                    # Transitioned to offline
                    outage_handled = False
                    restore_handled = True

            last_state = online

            # ---- OUTAGE ----
            if not online and not outage_handled:
                telegram_main("⚠️ Світла нема")

                for dev in DISABLE_DEVICE_IDS:
                    try:
                        client.set_switch(dev, False)
                    except RuntimeError:
                        pass

                outage_handled = True
                restore_handled = False

            # ---- RESTORE ----
            elif online and not restore_handled:
                telegram_main("✅ Світло є")

                for dev in DISABLE_DEVICE_IDS:
                    try:
                        client.set_switch(dev, True)
                    except (RuntimeError, requests.RequestException) as exc:
                        jlog(
                            logging.WARNING,
                            event="tuya_enable_failed",
                            device=dev,
                            error=str(exc),
                        )
                    # small delay between enabling devices
                    time.sleep(15)

                try:
                    client.set_switch(TUYA_DEVICE_ID, True)
                except (RuntimeError, requests.RequestException) as exc:
                    jlog(
                        logging.WARNING,
                        event="tuya_enable_primary_failed",
                        device=TUYA_DEVICE_ID,
                        error=str(exc),
                    )

                restore_handled = True
                outage_handled = False

            time.sleep(PING_INTERVAL)

        except (requests.RequestException, RuntimeError) as exc:
            jlog(logging.ERROR, event="monitor_error", error=str(exc))
            notify_error("main_loop", exc)
            time.sleep(PING_INTERVAL)


if __name__ == "__main__":
    main()
