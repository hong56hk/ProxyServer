#!/bin/bash

echo "stopping client..."
./stop_client.sh

echo "wait 3s until starting the client..."
sleep 3
./start_client.sh
