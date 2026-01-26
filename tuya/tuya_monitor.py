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

INTERVAL = int(os.getenv("INTERVAL", "10"))
CONFIRM_SECONDS = int(os.getenv("CONFIRM_SECONDS", "60"))
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
            raise RuntimeError(data)

        self.token = data["result"]["access_token"]
        self.token_expire = time.time() + data["result"]["expire_time"] - 60
        jlog(logging.INFO, event="tuya_token_refreshed")

    def ensure_token(self):
        if not self.token or time.time() > self.token_expire:
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
    if r.ok:
        jlog(logging.INFO, event="telegram_sent")
    else:
        jlog(logging.ERROR, event="telegram_failed", status=r.status_code)

# ===================== MAIN LOOP =====================

def main():
    client = TuyaClient()
    last_state = None
    heartbeat_logged = None  # Track last logged online/offline state

    jlog(logging.INFO, event="monitor_started")

    while True:
        try:
            online = client.get_device_online(TUYA_DEVICE_ID)

            # Only log heartbeat on state change
            if heartbeat_logged is None or online != heartbeat_logged:
                jlog(logging.INFO, event="heartbeat", online=online)
                heartbeat_logged = online

            # First iteration
            if last_state is None:
                last_state = online

            # State changed
            elif online != last_state:
                jlog(
                    logging.INFO,
                    event="confirm_window_started",
                    target_state=online,
                    seconds=CONFIRM_SECONDS,
                )

                # Wait for CONFIRM_SECONDS and re-check
                end_time = time.time() + CONFIRM_SECONDS
                confirmed = False
                while time.time() < end_time:
                    time.sleep(5 + random.uniform(0, 2))
                    try:
                        check = client.get_device_online(TUYA_DEVICE_ID)
                        if check == online:
                            confirmed = True
                            break
                    except Exception:
                        pass

                if confirmed:
                    if not online:
                        telegram(f"⚠️ Monitor OFFLINE — disabling devices")
                        for d in DISABLE_DEVICE_IDS:
                            client.set_switch(d, False)
                    else:
                        telegram(f"✅ Monitor ONLINE — enabling devices")
                        for d in DISABLE_DEVICE_IDS:
                            client.set_switch(d, True)
                        # Enable the monitored device itself
                        client.set_switch(TUYA_DEVICE_ID, True)

                    last_state = online
                    heartbeat_logged = online  # Ensure correct heartbeat logging

            time.sleep(INTERVAL + random.uniform(0, JITTER))

        except Exception as e:
            jlog(logging.ERROR, event="monitor_error", error=str(e))
            time.sleep(INTERVAL)

if __name__ == "__main__":
    main()

