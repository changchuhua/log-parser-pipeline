#!/bin/bash
set -e

# Create directories if they do not exist
mkdir -p data/raw data/processed data/parsed

# Write dummy LogHub-2.0 CSV using echo to prevent raw cat operations
echo "Date,Time,Content,Level,Component,LineId" > data/raw/dummy_loghub.csv
echo "2026-01-01,12:00:00,User admin logged in,INFO,Auth,1" >> data/raw/dummy_loghub.csv
echo "2026-01-01,12:00:01,User guest logged in,INFO,Auth,2" >> data/raw/dummy_loghub.csv
echo "2026-01-01,12:00:02,User guest logged in,INFO,Auth,3" >> data/raw/dummy_loghub.csv

echo "[*] Created dummy LogHub data."

# Determine docker compose cmd
COMPOSE_CMD="docker compose"
if ! command -v docker compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
fi

echo "[*] Launching E2E integration test using $COMPOSE_CMD..."
$COMPOSE_CMD -f docker-compose.test.yml up --build --abort-on-container-exit

# Assert output evaluation report exists
if [ -f "data/evaluation_report.json" ]; then
    echo "[+] E2E Test Passed: data/evaluation_report.json generated."
    exit 0
else
    echo "[-] E2E Test Failed: data/evaluation_report.json not found."
    exit 1
fi
