#!/usr/bin/env bash
set -euo pipefail

# GPU Cleanup Trap to unload model from GPU VRAM on exit/interruption
cleanup() {
    echo "Cleaning up GPU VRAM (unloading model)..."
    curl -s -X POST http://localhost:11434/api/generate -d '{"model": "gemma4:26b", "keep_alive": 0}' > /dev/null || true
    echo "Cleaning up any stale unified_parser containers..."
    docker rm -f unified_parser >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 1. Clean data dirs
echo "Cleaning old data..."
rm -f data/processed/* data/parsed/* data/raw/*

# 2. Copy LogHub csv datasets
echo "Copying raw datasets..."
LOGHUB_SRC="${LOGHUB_SRC_DIR:-$HOME/OneDrive/Drive/gatech/Practicum/loghub-2.0-main/full_dataset}"
if [ ! -d "$LOGHUB_SRC" ]; then
    echo "Error: LOGHUB_SRC_DIR does not exist at: $LOGHUB_SRC"
    echo "Please set the LOGHUB_SRC_DIR environment variable to the raw datasets directory."
    exit 1
fi
find "$LOGHUB_SRC" -name "*_full.log_structured.csv" -exec cp {} data/raw/ \;

# 3. Build containers
echo "Building docker images..."
docker-compose build

# 4. Generate ECS
echo "Generating ECS logs..."
docker-compose run --rm component_1 python transform_to_ecs.py --loghub data/raw --out-dir data/processed

# 5. Start unified_parser container in the background
echo "Starting unified_parser with monitoring..."
METHOD=${1:-logbatcher}
LIMIT=${2:-0}
docker-compose run --name unified_parser -e USE_CACHE -e WRITE_CACHE --rm component_3 python main_parser.py --method "$METHOD" --time-limit "$LIMIT" &
PARSER_PID=$!

# Wait for unified_parser container to spin up
sleep 3

# 6. Stream and inspect logs in real time
# If a timeout error occurs, stop the container immediately
TIMEOUT_DETECTED=0
docker logs -f unified_parser 2>&1 | tee data/parsed/unified_parser.log | while read -r line; do
    echo "[PARSER LOG] $line"
    if echo "$line" | grep -q "Read timed out"; then
        echo "=================================================="
        echo "CRITICAL: HTTP request timeout detected!"
        echo "Stopping pipeline and shutting down containers..."
        echo "=================================================="
        docker stop unified_parser || true
        docker-compose down || true
        TIMEOUT_DETECTED=1
        kill -9 $PARSER_PID || true
        exit 1
    fi
done

# If parser finished successfully, run evaluator
wait $PARSER_PID || true
echo "Parser completed. Running evaluator..."
docker-compose run --rm component_4 python evaluate_metrics.py
