#!/bin/bash

echo "stopping server..."
./stop_server.sh

echo "wait 30s until starting the server..."
sleep 30
./start_server.sh
