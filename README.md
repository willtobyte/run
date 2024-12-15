
### Run

```shell
docker compose --file compose.yaml --file playground.yaml up --build
```

### Deploy

```shell
DOCKER_HOST=ssh://root@hetzner.remote docker compose --file docker-compose.yaml --file cloudflare.yaml --file logging.yaml up --build -d
```
