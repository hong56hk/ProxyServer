#!/bin/bash

python3 HTTPMonitor.py --proxy 127.0.0.1:4430 --listen 0.0.0.0:4420 &>> httpmonitor.log &