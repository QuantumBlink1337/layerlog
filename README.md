# layerlog

Logs Bambu P2S prints to Notion with slicer settings, driven by Bambuddy webhooks.
See the handoff doc for the full design. Current state: 3MF parser, Bambuddy client,
and a webhook **capture** endpoint (saves raw payloads so we can see their real shape).

## Capture Bambuddy's webhook payloads (on nucleus)

```bash
docker build -t layerlog .
docker run -d --name layerlog -p 8099:8000 -v /mnt/user/appdata/layerlog:/data \
  -e WEBHOOK_SECRET=pick-a-long-random-string layerlog
```

Point Bambuddy's webhook at `http://<nucleus-ip>:8099/hooks/bambuddy`, using the same
secret in its custom header if it supports one. Start or cancel a print; each request is
saved to `/mnt/user/appdata/layerlog/captures/*.json` (secret values are redacted, and
the file records which header carried the secret).

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
```
