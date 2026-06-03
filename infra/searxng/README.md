# SearXNG — self-hosted search backend for the `web_search` skill

`$0`, local-only meta-search. The `web_search` skill (`friday/skills/web_search.py`)
calls `http://localhost:8080/search?format=json`. This container is that endpoint.

## Launch (no compose plugin on this box → plain `docker run`)

```bash
sudo docker run -d --name sutra-searxng --restart unless-stopped \
  -p 8080:8080 \
  -v /home/sarjan/friday-os/infra/searxng:/etc/searxng:rw \
  searxng/searxng:latest
```

(Or, once a compose plugin is installed: `docker compose up -d searxng`.)

## Why the settings overrides

`settings.yml` only changes two things that matter for programmatic use:

- `search.formats` includes **`json`** → enables the API the skill hits
  (stock SearXNG ships HTML-only, so the skill would otherwise get a 403).
- `server.limiter: false` → the bot/rate limiter won't throttle our own
  localhost calls.

## Verify

```bash
curl -s 'http://localhost:8080/search?q=hello&format=json' | head -c 200
sutra ask "search the web: what is the weather in Sudbury Ontario today"
```

## Manage

```bash
sudo docker stop sutra-searxng
sudo docker start sutra-searxng
sudo docker logs --tail 50 sutra-searxng
```
