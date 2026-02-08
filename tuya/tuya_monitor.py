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

DISABLE_DEVICE_IDS = [
    d.strip() for d in os.getenv("DISABLE_DEVICE_IDS", "").split(",") if d.strip()
]

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


def ping_confirm(host):
    """
    Confirm outage using a single ping invocation with multiple packets.
    Returns True if ANY packet is received.
    """
    try:
        # -c N: send N packets
        # -i 1: 1s between packets (avoid flood)
        # -W 1: 1s per-reply timeout (Linux ping)
        cmd = ["ping", "-c", str(PING_CONFIRM_COUNT), "-i", "1", "-W", "1", host]
        jlog(logging.INFO, event="ping_confirm_run", host=host, cmd=" ".join(cmd))

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
            transmitted=PING_CONFIRM_COUNT,
            received=received,
            returncode=result.returncode,
        )
        return received > 0

    except (OSError, subprocess.SubprocessError) as exc:
        jlog(logging.WARNING, event="ping_confirm_error", host=host, error=str(exc))
        return None


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
        self.token_expire = (
            time.time() + int(data["result"]["expire_time"]) - 120
        )

        jlog(logging.INFO, event="tuya_token_refreshed")

    def ensure_token(self):
        """Ensure valid token."""
        if self.token and time.time() < self.token_expire:
            return
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

        if not data.get("success"):
            msg = str(data).lower()
            if "already" in msg or "offline" in msg:
                return
            notify_error("tuya_switch", "failed", data)
            raise RuntimeError(data)

        jlog(
            logging.INFO,
            event="tuya_switch_set",
            device=device_id,
            value=value,
        )


# ===================== MAIN LOOP =====================

# pylint: disable=too-many-branches,too-many-statements
def main():
    """Main monitoring loop."""
    client = TuyaClient()

    last_state = None
    outage_handled = False
    restore_handled = False
    last_api_check = 0

    jlog(logging.INFO, event="monitor_started")

    while True:
        try:
            online = None
            now = time.time()

            # ---- PING PRIMARY ----
            if PING_TARGET_HOST:
                ping_ok = ping_once(PING_TARGET_HOST)
                jlog(
                    logging.INFO,
                    event="probe_ping_once",
                    host=PING_TARGET_HOST,
                    result=ping_ok,
                )

                if ping_ok is False:
                    # Only run confirm when we were previously online/unknown.
                    # If we're already offline, keep it lightweight: one ping per loop.
                    if last_state is not False:
                        jlog(
                            logging.INFO,
                            event="probe_ping_confirm_start",
                            host=PING_TARGET_HOST,
                            confirm_count=PING_CONFIRM_COUNT,
                        )
                        confirm_res = ping_confirm(PING_TARGET_HOST)
                        # ping_confirm returns True/False or None (if ping broken)
                        if confirm_res is None:
                            online = client.get_device_online(
                                TUYA_DEVICE_ID,
                                reason="ping_confirm_unavailable",
                            )
                            jlog(
                                logging.INFO,
                                event="probe_decision",
                                source="tuya_api_fallback",
                                online=online,
                                reason="ping_confirm_unavailable",
                            )
                        else:
                            online = confirm_res
                            jlog(
                                logging.INFO,
                                event="probe_ping_confirm_result",
                                host=PING_TARGET_HOST,
                                online=online,
                            )
                            jlog(
                                logging.INFO,
                                event="probe_decision",
                                source="ping_confirm",
                                online=online,
                            )
                    else:
                        online = False
                        jlog(
                            logging.INFO,
                            event="probe_decision",
                            source="ping",
                            online=online,
                            reason="already_offline_single_ping_failed",
                        )

                elif ping_ok is True:
                    online = True
                    jlog(
                        logging.INFO,
                        event="probe_decision",
                        source="ping",
                        online=online,
                    )
                else:
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
                except (requests.RequestException, RuntimeError) as exc:
                    jlog(
                        logging.WARNING,
                        event="probe_api_safety_check_failed",
                        device=TUYA_DEVICE_ID,
                        error=str(exc),
                    )

            jlog(logging.INFO, event="heartbeat", online=online)

            if last_state is None:
                last_state = online
                outage_handled = not online
                restore_handled = online
                time.sleep(PING_INTERVAL)
                continue

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
                last_state = online

            # ---- RESTORE ----
            elif online and not restore_handled:
                telegram_main("✅ Світло є")

                for dev in DISABLE_DEVICE_IDS:
                    try:
                        client.set_switch(dev, True)
                    except RuntimeError:
                        pass

                try:
                    client.set_switch(TUYA_DEVICE_ID, True)
                except RuntimeError:
                    pass

                restore_handled = True
                outage_handled = False
                last_state = online

            time.sleep(PING_INTERVAL)

        except (requests.RequestException, RuntimeError) as exc:
            jlog(logging.ERROR, event="monitor_error", error=str(exc))
            notify_error("main_loop", exc)
            time.sleep(PING_INTERVAL)


if __name__ == "__main__":
    main()
