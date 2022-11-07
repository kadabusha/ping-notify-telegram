# ping-notify-telegram
The script that communicates with BetterUptime over API, checks the presence of new incidents with configured name.
Case an incident is found, selected channel gets notification about the start of incident, and another message is sent once the incident is resolved.

Script requires at least python 3.9 to run with pytz package installed.

The script is expected to be executed every minute.
Example of crontab entry:
```
@every_minute /usr/home/kadabusha/host-check-telegram-notify.py 2>1 >>/usr/home/kadabusha/host-check-telegram-notify.log
```
