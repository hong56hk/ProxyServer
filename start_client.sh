#!/bin/bash

today=$(date "+%Y%m%d")

python3 ProxyClient.py -v 20 --proxy 123.202.59.29:443 --log ./log/client_$today.log >> log/client_$today.err 2>&1 &

pid=$!
echo "pid: " $pid
echo $pid > client.pid
echo "done"