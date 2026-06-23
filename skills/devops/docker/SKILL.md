---
name: docker
description: Comprehensive Docker operations: containers, images, volumes, networks, Compose, debugging, optimization, and container orchestration
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [docker, containers, devops, infrastructure, compose, images, volumes, networks, debugging, orchestration]
    category: devops
    requires_toolsets: [terminal]
---

# Docker Operations

Comprehensive guide for Docker container operations, image management, Docker Compose orchestration, debugging, and optimization.

## When to Use

- Run, stop, restart, or manage Docker containers
- Build, pull, push, or manage Docker images
- Work with Docker Compose stacks and multi-service applications
- Manage volumes, networks, or other Docker resources
- Debug container issues or analyze logs
- Optimize Dockerfiles or container performance
- Clean up Docker resources and manage disk usage

## Prerequisites

- Docker Engine installed and running
- User added to the `docker` group (or use `sudo`)
- Docker Compose v2 (included with modern Docker installations)

Verification:
```bash
docker --version && docker compose version
```

## Quick Reference

| Operation | Command |
|-----------|---------|
| Run container | `docker run -d --name NAME IMAGE` |
| Stop container | `docker stop NAME` |
| Remove container | `docker rm NAME` |
| View logs | `docker logs --tail 50 -f NAME` |
| Shell access | `docker exec -it NAME /bin/sh` |
| List containers | `docker ps -a` |
| Build image | `docker build -t TAG .` |
| Compose up | `docker compose up -d` |
| Compose down | `docker compose down` |
| Disk usage | `docker system df` |
| Cleanup | `docker system prune -a` |

## Container Lifecycle

### Running Containers

```bash
# Basic detached container
docker run -d --name web nginx

# With port mapping
docker run -d --name web -p 8080:80 nginx

# With environment variables
docker run -d -e POSTGRES_PASSWORD=secret -e POSTGRES_DB=mydb --name db postgres:16

# With persistent volume
docker run -d -v pgdata:/var/lib/postgresql/data --name db postgres:16

# With bind mount (development)
docker run -d -v $(pwd)/src:/app/src -p 3000:3000 --name dev my-app

# Interactive debugging (auto-remove)
docker run -it --rm ubuntu:22.04 /bin/bash

# With resource limits
docker run -d --memory=512m --cpus=1.5 --restart=unless-stopped --name app my-app

# With multiple ports
docker run -d -p 8080:80 -p 8443:443 --name web nginx

# With network
docker run -d --network mynet --name app my-app
```

### Managing Containers

```bash
docker ps                          # running containers
docker ps -a                       # all containers including stopped
docker stop NAME                   # graceful stop
docker start NAME                  # start stopped container
docker restart NAME                # stop + start
docker rm NAME                     # remove stopped container
docker rm -f NAME                  # force remove running container
docker pause NAME                  # pause container
docker unpause NAME                # unpause container
docker container prune             # remove all stopped containers
```

### Container Interaction

```bash
# Shell access
docker exec -it NAME /bin/sh       # use /bin/bash if available
docker exec -it NAME /bin/bash

# Run commands
docker exec NAME ls -la
docker exec -u root NAME apt update

# View environment
docker exec NAME env

# Copy files
docker cp NAME:/path/file ./local      # from container
docker cp ./file NAME:/path/           # to container

# Logs
docker logs --tail 100 -f NAME         # follow last 100 lines
docker logs --since 2h NAME            # logs from last 2 hours
docker logs --until 1h ago NAME        # logs until 1 hour ago

# Inspect
docker inspect NAME                    # full details (JSON)
docker stats --no-stream               # resource usage snapshot
docker top NAME                        # running processes
docker port NAME                       # port mappings
```

## Image Management

### Building Images

```bash
# Basic build
docker build -t my-app:latest .

# With custom Dockerfile
docker build -t my-app:prod -f Dockerfile.prod .

# Clean build (no cache)
docker build --no-cache -t my-app .

# Build with BuildKit (faster)
DOCKER_BUILDKIT=1 docker build -t my-app .

# With build arguments
docker build --build-arg VERSION=1.0 -t my-app .

# Multi-platform build
docker buildx build --platform linux/amd64,linux/arm64 -t my-app .
```

### Image Operations

```bash
# Pull and push
docker pull node:20-alpine
docker pull ubuntu:22.04
docker login ghcr.io
docker push registry/my-app:v1.0

# Tag images
docker tag my-app:latest registry/my-app:v1.0
docker tag SOURCE:TAG TARGET:TAG

# List and inspect
docker images                          # list local images
docker history IMAGE                   # see layers
docker inspect IMAGE                   # full details

# Remove images
docker rmi IMAGE                       # remove image
docker image prune                     # remove dangling images
docker image prune -a                  # remove all unused images
docker image prune -a --filter "until=168h"   # older than 7 days
```

### Dockerfile Optimization

Key optimization strategies:

1. **Multi-stage builds** - separate build and runtime environments
2. **Layer ordering** - put rarely-changing layers first (dependencies before code)
3. **Combine RUN commands** - fewer layers, smaller images
4. **Use .dockerignore** - exclude unnecessary files
5. **Pin versions** - use specific versions, not `latest`
6. **Run as non-root** - add `USER` instruction
7. **Use slim/alpine bases** - smaller base images

Example optimized Dockerfile:
```dockerfile
# Build stage
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production
COPY . .
RUN npm run build

# Runtime stage
FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
USER node
EXPOSE 3000
CMD ["node", "dist/index.js"]
```

## Docker Compose

### Basic Operations

```bash
# Start services
docker compose up -d                   # detached
docker compose up -d --build           # rebuild before starting
docker compose up --force-recreate     # recreate containers

# Stop services
docker compose down                    # stop and remove containers
docker compose down -v                 # also remove volumes
docker compose stop                    # stop without removing

# Monitoring
docker compose ps                      # list services
docker compose logs -f api             # follow specific service logs
docker compose logs --tail 50          # last 50 lines all services

# Interaction
docker compose exec api /bin/sh        # shell into service
docker compose run --rm api npm test   # one-off command
docker compose restart api             # restart specific service

# Validation
docker compose config                  # validate and view config
```

### Compose File Structure

```yaml
services:
  api:
    build: .
    ports:
      - "3000:3000"
    environment:
      - DATABASE_URL=postgres://user:pass@db:5432/mydb
      - NODE_ENV=production
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: pass
      POSTGRES_DB: mydb
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U user"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redisdata:/data

volumes:
  pgdata:
  redisdata:

networks:
  default:
    driver: bridge
```

## Volumes and Networks

### Volume Management

```bash
# List and create
docker volume ls                       # list volumes
docker volume create mydata            # create volume
docker volume inspect mydata           # details

# Remove volumes
docker volume rm mydata                # remove (fails if in use)
docker volume prune                    # remove unused volumes
```

### Network Management

```bash
# List and create
docker network ls                      # list networks
docker network create mynet            # create bridge network
docker network create --driver overlay mynet  # overlay network (swarm)

# Connect containers
docker network connect mynet NAME      # attach container
docker network disconnect mynet NAME   # detach container

# Inspect and remove
docker network inspect mynet           # details
docker network rm mynet                # remove network
docker network prune                   # remove unused networks
```

## Debugging Containers

### Common Issues

**Container exits immediately:**
```bash
docker logs NAME                       # check logs
docker run -it --entrypoint /bin/sh IMAGE  # test interactively
```

**Port already allocated:**
```bash
docker ps                              # find using container
lsof -i :PORT                          # find using process
```

**Permission issues:**
```bash
docker exec -u root NAME ls -la        # run as root
docker run --user $(id -u):$(id -g) ...  # match host UID/GID
```

**Network connectivity:**
```bash
docker network inspect NAME            # check network
docker exec NAME ping other-container  # test connectivity
```

### Log Analysis

```bash
# Real-time logs
docker logs -f NAME

# Filter logs
docker logs NAME 2>&1 | grep ERROR

# Timestamps
docker logs -t NAME

# Export logs
docker logs NAME > container.log
```

## Disk Management

### Check Usage

```bash
docker system df                       # summary
docker system df -v                    # detailed breakdown
docker system events                   # recent events
```

### Cleanup Strategies

```bash
# Safe cleanup (always start with diagnostic)
docker container prune                 # stopped containers
docker image prune                     # dangling images
docker volume prune                    # unused volumes
docker network prune                   # unused networks

# Aggressive cleanup (confirm first!)
docker system prune                    # containers + images + networks
docker system prune -a                 # also unused images
docker system prune -a --volumes       # everything including named volumes
```

**Warning:** `docker system prune -a --volumes` removes named volumes with potentially important data. Always confirm with the user first.

## Security Best Practices

- Run containers as non-root users
- Use specific image versions (not `latest`)
- Scan images for vulnerabilities
- Limit container resources
- Use read-only filesystems where possible
- Avoid privileged containers
- Keep base images updated

```bash
# Security scan
docker scan IMAGE

# Run with security options
docker run --read-only --security-opt=no-new-privileges IMAGE
```

## Performance Optimization

```bash
# Resource limits
docker run -m 512m --cpus=1.5 IMAGE

# Performance monitoring
docker stats NAME

# Optimize build cache
docker build --cache-from SOURCE:TAG

# Use BuildKit
DOCKER_BUILDKIT=1 docker build .
```

## Verification Steps

After any Docker operation:

1. **Container running?** → `docker ps` (check status "Up")
2. **Logs clean?** → `docker logs --tail 20 NAME` (no errors)
3. **Port accessible?** → `curl -s http://localhost:PORT`
4. **Image built?** → `docker images | grep TAG`
5. **Compose healthy?** → `docker compose ps` (all services running)
6. **Disk space?** → `docker system df` (compare before/after)
