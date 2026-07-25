#!/bin/bash
# Health probe script for mock exchange containers

CONTAINER=$1

if [ -z "$CONTAINER" ]; then
    echo "Usage: $0 <container_name>"
    exit 1
fi

# Check if container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "Container $CONTAINER is not running"
    exit 1
fi

# Check health endpoint
RESPONSE=$(docker exec $CONTAINER curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/healthz)

if [ "$RESPONSE" = "200" ]; then
    echo "Container $CONTAINER is healthy"
    exit 0
else
    echo "Container $CONTAINER health check failed (HTTP $RESPONSE)"
    exit 1
fi
