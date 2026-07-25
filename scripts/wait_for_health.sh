#!/bin/bash
# Wait for all mock exchange containers to be healthy

CONTAINERS="mockx1 mockx2"
MAX_RETRIES=30
RETRY_DELAY=2

echo "Waiting for containers to be healthy..."

for container in $CONTAINERS; do
    echo -n "Checking $container... "
    retries=0
    
    while [ $retries -lt $MAX_RETRIES ]; do
        if ./scripts/health_probe.sh $container > /dev/null 2>&1; then
            echo "OK"
            break
        fi
        
        retries=$((retries + 1))
        if [ $retries -eq $MAX_RETRIES ]; then
            echo "FAILED"
            echo "Container $container failed to become healthy after $MAX_RETRIES attempts"
            exit 1
        fi
        
        sleep $RETRY_DELAY
    done
done

echo "All containers are healthy!"
exit 0
