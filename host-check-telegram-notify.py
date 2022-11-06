#!/usr/bin/env python
import requests
from datetime import timedelta, datetime
from pytz import timezone

MY_ADDR = "IP_TO_CHECK"

def send_msg(text):
    token = "VERY_SECRET_TOKEN"
    chat_id = "CHAT_ID"
    url_req = "https://api.telegram.org/bot" + token + "/sendMessage" + "?chat_id=" + chat_id + "&text=" + text
    results = requests.get(url_req)

# check part
# getting UTC, so that later we compare with UTC
today = datetime.utcnow()
yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
url_inc = "https://betteruptime.com/api/v2/incidents?from=%s&to=now" % yesterday

bt_token = "BetterUptimeToken"
h_auth = {"authorization": "Bearer %s" % bt_token}
h_json = {"Content-Type": "application/json"}
# merge two dicts, works in >=python 3.9
h_all = h_auth | h_json

# get all incidents
inc_all = requests.get(url_inc, headers=h_auth).json()

name_of_monitor = "The name to check"

# expected format of all dates here - UTC
for id in inc_all['data']:
    started_at = id['attributes']['started_at']
    name = id['attributes']['name']
    resolved_at = id['attributes']['resolved_at']
    acknowledged_at = id['attributes']['acknowledged_at']
    if name == name_of_monitor:
        if resolved_at:
            dt = datetime.strptime(resolved_at, '%Y-%m-%dT%H:%M:%S.%fZ')
            if (today-dt).total_seconds() < 61:
                dt1 = dt.replace(tzinfo=timezone('UTC')).astimezone(timezone('Europe/Kyiv')).strftime("%Y-%m-%d %H:%M")
                send_msg("\U0001f635 \U0001f56f \U0001fa94 \U0001f50b Event on incident resolved at %s" % dt1)
        else:
            if not acknowledged_at:
                url_ack = "https://betteruptime.com/api/v2/incidents/%s/acknowledge" % id['id']
                # acknowledge incident
                ack_inc = requests.post(url_ack, headers=h_all)
                st2 = datetime.strptime(started_at, '%Y-%m-%dT%H:%M:%S.%fZ')
                dt2 = st2.replace(tzinfo=timezone('UTC')).astimezone(timezone('Europe/Kyiv')).strftime("%Y-%m-%d %H:%M")
                # send telegram notification
                send_msg("\U0001f973 \U0001f4a1 \U0001f50c \U0001f4e1 Event on new incident at %s" % dt2)
