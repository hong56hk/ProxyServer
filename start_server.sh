#!/bin/bash

today=$(date "+%Y%m%d")

python3 ProxyServer.py -v 20 --routing routingtable.conf --config config.json --proxy 0.0.0.0:8443 --log ./log/server_$today.log >> log/server_$today.err 2>&1 &

pid=$!
echo "pid: " $pid
echo $pid > server.pid
echo "done"