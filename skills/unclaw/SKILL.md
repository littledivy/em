---
name: unclaw
description: Add/remove/list credentials in the local unclaw proxy via its HTTP API. Use when user asks to add an API key, bot token, or other secret.
when_to_use: User says "add this to unclaw", or provides an API key / bot token for any service (Gemini, OpenAI, Anthropic, Discord, Telegram, Slack, GitHub, Notion, etc.).
requires:
  - curl
  - python3
---

# unclaw — local credential proxy

Unclaw runs at `http://localhost:8080`. Dev auth is enabled on this machine.

## One-shot: add an integration

```bash
python3 - <<'PY'
import json, urllib.request, sys

BASE = "http://localhost:8080"
PLUGIN = "gemini"           # change: gemini | openai | anthropic | discord | telegram | slack | github | notion | apikey
NAME   = "my-gemini"         # short unique name
CONFIG = {"api_key": "AIza..."}   # see field table below

def req(method, path, data=None, token=None):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, headers=h, method=method)
    try:
        return json.loads(urllib.request.urlopen(r).read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()}

token = req("POST", "/api/auth/dev-token")["token"]
created = req("POST", "/api/integrations",
              {"pluginId": PLUGIN, "name": NAME, "config": CONFIG},
              token)
print("created:", created)
profiles = req("GET", "/api/profiles", token=token)["profiles"]
pid = profiles[0]["id"]
bound = req("POST", f"/api/profiles/{pid}/integrations",
            {"integrationId": created["id"]}, token)
print("bound:", bound)
PY
```

## Plugin field table

| pluginId           | config                                                      |
| ------------------ | ----------------------------------------------------------- |
| `gemini`           | `{"api_key":"..."}`                                         |
| `openai`           | `{"api_key":"..."}`                                         |
| `anthropic`        | `{"api_key":"..."}`                                         |
| `openrouter`       | `{"api_key":"..."}`                                         |
| `discord`          | `{"bot_token":"..."}`                                       |
| `telegram`         | `{"bot_token":"..."}`                                       |
| `slack`            | `{"bot_token":"..."}`                                       |
| `github`           | `{"token":"..."}`                                           |
| `notion`           | `{"token":"..."}`                                           |
| `apikey` (generic) | `{"header":"x-api-key","api_key":"...","domains":["host"]}` |

Domains and placeholders default server-side; only pass the credential fields.

## List current integrations

```bash
curl -s -H "Authorization: Bearer $(curl -s -X POST http://localhost:8080/api/auth/dev-token | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')" http://localhost:8080/api/integrations | python3 -m json.tool
```

## Delete

```bash
python3 - <<'PY'
import json, urllib.request
BASE = "http://localhost:8080"
NAME = "my-gemini"           # the integration name you want gone
tok = json.loads(urllib.request.urlopen(
    urllib.request.Request(BASE + "/api/auth/dev-token", method="POST")).read())["token"]
h = {"Authorization": f"Bearer {tok}"}
ints = json.loads(urllib.request.urlopen(
    urllib.request.Request(BASE + "/api/integrations", headers=h)).read())["integrations"]
for i in ints:
    if i["name"] == NAME:
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/integrations/" + i["id"], headers=h, method="DELETE")).read()
        print("deleted", i["id"])
PY
```

## Rules

- Run exactly one Python script for the whole flow — don't chain multiple bash
  commands.
- Never echo the real secret after receiving it.
- Never put real secrets into agent.toml — placeholders only.
- If the API returns `error`, print it and stop (don't retry or probe).
