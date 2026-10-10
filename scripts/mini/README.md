# Inference box scripts

What runs on the Mac mini to keep a model served for the save check
(`src/save_check_model.py`) and anything else that asks `llama-server`. The
copies here are the source; the mini runs what was last copied to it.

| file | where it goes on the mini |
|---|---|
| `llama-serve.sh` | `~/models/` |
| `start-server.sh` | `~/models/` |
| `serve.conf` | `~/models/` (first install only: it holds the default model) |
| `local.translate-books.llama-server.plist` | `~/Library/LaunchAgents/` |

## Install or update

```
scp scripts/mini/llama-serve.sh scripts/mini/start-server.sh mini:models/
scp scripts/mini/local.translate-books.llama-server.plist mini:Library/LaunchAgents/
ssh mini 'chmod +x ~/models/llama-serve.sh ~/models/start-server.sh && ~/models/start-server.sh'
```

On a first install, copy `serve.conf` to `~/models/` before the last line.

## Choosing the model

launchd starts the default model at login and again if the server exits.

- **Change the default:** `ssh mini '~/models/start-server.sh --set-default <gguf> <alias>'`,
  or edit `~/models/serve.conf` and run `start-server.sh --default`.
- **Serve another model for a while:** `start-server.sh <gguf> <alias> [flags]`.
  It stays until `start-server.sh --default` or a reboot. A gguf that is not
  there is refused. If llama-server cannot load the model, the default comes
  back within a minute (`~/models/server.out` says why).
- **Free the memory:** `start-server.sh --stop`.
- **See what is up:** `start-server.sh --status`.

The save check never starts or swaps a model. It uses whichever one is loaded,
if that model's alias has a profile, and otherwise leaves the check to the
rules. Give a model the alias its profile is listed under.

The agent is per-user, so after a reboot it starts once `nathaniel` has logged
in at the console.
