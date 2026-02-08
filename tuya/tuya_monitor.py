#!/usr/bin/env python3
import os
import time
import json
import random
import hmac
import hashlib
import logging
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

# original envs preserved
INTERVAL = int(os.getenv("INTERVAL", "60"))          # base interval
CONFIRM_SECONDS = int(os.getenv("CONFIRM_SECONDS", "60"))  # kept for compatibility
JITTER = float(os.getenv("JITTER", "2"))

BASE_URL = f"https://openapi.tuya{TUYA_REGION}.com"

# ===================== LOGGING =====================

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","event":%(message)s}'
)
log = logging.getLogger("tuya-monitor")


def jlog(level, **payload):
    log.log(level, json.dumps(payload, ensure_ascii=False))


# ===================== TUYA CLIENT =====================

class TuyaClient:
    def __init__(self):
        self.token = None
        self.token_expire = 0

    def _ts(self):
        return str(int(time.time() * 1000))

    def _sha256(self, body: str):
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
        path = "/v1.0/token?grant_type=1"
        t, sign = self._sign("GET", path)

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
            jlog(logging.ERROR, event="tuya_token_refresh_failed", response=data)
            raise RuntimeError(data)

        self.token = data["result"]["access_token"]
        self.token_expire = time.time() + int(data["result"]["expire_time"]) - 120

        # log only on real refresh
        jlog(logging.INFO, event="tuya_token_refreshed")

    def ensure_token(self):
        if self.token and time.time() < self.token_expire:
            return
        self.refresh_token()

    def get_device_online(self, device_id):
        self.ensure_token()
        path = f"/v1.0/devices/{device_id}"
        t, sign = self._sign("GET", path, access_token=self.token)

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
            raise RuntimeError(data)

        result = data.get("result")
        if not result or "online" not in result:
            raise RuntimeError("online_field_missing")

        return result["online"]

    def set_switch(self, device_id, value: bool):
        self.ensure_token()
        path = f"/v1.0/devices/{device_id}/commands"
        body = json.dumps({
            "commands": [{"code": "switch_1", "value": value}]
        })

        t, sign = self._sign("POST", path, body, access_token=self.token)

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
            raise RuntimeError(data)

        jlog(logging.INFO, event="tuya_switch_set", device=device_id, value=value)


# ===================== TELEGRAM =====================

def telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=10,
    )
    if not r.ok:
        jlog(logging.ERROR, event="telegram_failed", status=r.status_code)


# ===================== MAIN LOOP (OPTION A) =====================

def main():
    client = TuyaClient()

    last_state = None
    last_seen_online = None
    devices_disabled = False

    jlog(logging.INFO, event="monitor_started")

    while True:
        try:
            online = client.get_device_online(TUYA_DEVICE_ID)

            # heartbeat logging only on change
            if online != last_seen_online:
                jlog(logging.INFO, event="heartbeat", online=online)
                last_seen_online = online

            if last_state is None:
                last_state = online

            # transition logic
            if online != last_state:
                if not online:
                    telegram("⚠️ Світла нема")
                    for dev in DISABLE_DEVICE_IDS:
                        client.set_switch(dev, False)
                    devices_disabled = True
                    jlog(logging.INFO, event="devices_disabled")
                else:
                    telegram("✅ Світло є")
                    # no re-enable (handled by Tuya automation)
                    devices_disabled = False
                    jlog(logging.INFO, event="power_restored")

                last_state = online

            # adaptive polling using existing INTERVAL
            if online:
                sleep_time = INTERVAL
            else:
                if devices_disabled:
                    sleep_time = INTERVAL * 5   # slow mode
                else:
                    sleep_time = INTERVAL

            time.sleep(sleep_time + random.uniform(0, JITTER))

        except Exception as exc:
            jlog(logging.ERROR, event="monitor_error", error=str(exc))
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

