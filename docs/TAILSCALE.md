# Reading the reader from anywhere (Tailscale)

Out of the box the reader is reachable only from the same Wi-Fi network — on boot the app
prints *"(Phones/tablets on the same Wi-Fi: pick a Network URL above.)"*. This guide lifts
exactly that limitation: read any in-flight project from a phone on cellular data, with the
translation work still running on your PC.

The approach puts the machine on a [Tailscale](https://tailscale.com) tailnet and fronts the
app with `tailscale serve`. No code changes are required to make it work — the first two steps
are pure configuration. The remaining step is about keeping it up unattended.

**What you give up:** the reader is only available while the PC is awake and online, and every
reading device needs a Tailscale client. There is no link you can hand to someone outside your
tailnet. If those become real problems, host the app instead.

## Architecture

```
   Phone / laptop  (Tailscale client)
          │   WireGuard, E2E encrypted; direct when NAT allows,
          │   DERP-relayed when it doesn't
          ▼
   https://<machine>.<tailnet>.ts.net     ← tailscale serve, real Let's Encrypt cert
          │
   Windows PC ── waitress ──► Flask (full capability: aligner, LLM routes,
          │                    LanguageTool, Enchant)
          ▼
      ./projects/     ← the one and only copy. No sync, no manifest.
          ▲
   Claude Code · harness · judges   (unchanged)
```

One filesystem, one writer process.

---

## Step 1 — Get on the tailnet

1. Install Tailscale on the PC and on the phone, signed in to the same account.
2. Enable MagicDNS in the admin console so the machine gets a stable name.
3. If the connection is refused, allow inbound TCP 5000 on the Tailscale adapter in Windows
   Defender Firewall.

**Verify on cellular data with Wi-Fi off.** Testing over Wi-Fi proves nothing, since that
already worked.

> **Check:** you can read a chapter of a real book on mobile data.

That is the whole feature. The rest is durability and hygiene.

---

## Step 2 — HTTPS and a stable name

Run `tailscale serve` (or `tailscale cert`) to front the app at
`https://<machine>.<tailnet>.ts.net` with a real Let's Encrypt certificate. This also stops the
raw `100.x` address from moving under you.

The certificate matters more than it might look. The reader is a PWA — `reader.html` ships
`apple-mobile-web-app-capable` and a `manifest.webmanifest` with `display: standalone` — and
Android's manifest-based install wants a secure context. The cert makes "Add to Home Screen"
behave consistently across platforms.

> **Check:** home-screen install works on the phone and a chapter renders in standalone mode.

---

## Step 3 — Keep it running unattended

This is the approach's one genuine weakness, so treat it as real work.

### The tunnel, not the app, is the usual blocker

With "Run unattended" off, the node leaves the tailnet as soon as nobody is logged in — the
Flask process never gets a chance to matter.

- Run `tailscale set --unattended=true` from an elevated prompt. Verify with
  `tailscale debug prefs` → `"ForceDaemon": true`.
- **Disable node key expiry** in the admin console (Machines → ⋯ → Disable key expiry).
  Otherwise the node silently drops off the tailnet on the expiry date, and the failure mode is
  "I'm away from home, the reader is gone, and I can't fix it remotely."

### Use the service entry point, not `python app.py`

`scripts/serve.py` runs the app under **waitress**. Use waitress rather than gunicorn: gunicorn
needs `fcntl`/`os.fork` and does not run on Windows. The `waitress-serve` console script does
not work either — its `sys.path[0]` is the `Scripts\` directory, so `import web_ui.app` fails,
and it offers no way to configure logging.

`scripts/serve.py` handles all of that, and:

- Binds **127.0.0.1 only**, so `tailscale serve` is the single door in.
- Uses `threads=16`. Waitress is thread-per-connection from a fixed pool, and the
  batch-translate SSE stream holds its thread for the whole run; the default of 4 would let two
  dashboard tabs plus a phone starve the pool and make the server look hung.
- Logs to `logs/web_ui.log`, rebinding `sys.stdout`/`sys.stderr` so the app's bare `print()`
  calls land there too. A service has no console, so this is the only window into it.

### Install it as a scheduled task

```powershell
scripts\reader.ps1 install
```

This registers the `TranslateBooksReader` Task Scheduler entry from the definition in the
script, prompting once for your Windows password. Configuring the task by hand through the GUI
is the wrong move — it is state on one machine that nothing version-controls and no clone
carries, and it drifts silently.

The settings the script writes, and why each one is there:

| Setting | Why |
|---|---|
| "At startup", 1-minute delay | Slack for the network stack and Tailscale to come up first. |
| Run as your user, **Run whether user is logged on or not**, stored password | Required: Python packages live in per-user site-packages, so a `LocalSystem` service would not import Flask. |
| **Start in = repo root** | Without it Windows starts the process in `C:\Windows\system32` and cwd-relative paths resolve against that. |
| No run-time limit (untick "Stop the task if it runs longer than 3 days") | It is a service. |
| Restart every 1 minute, up to 60 times | The watchdog, paired with `/healthz`. |
| "Do not start a new instance" | One process only — the in-process caches assume it. |
| Untick "Stop if the computer switches to battery power" | Unplugging should not kill the reader. |
| Untick "Start the task only if the computer is on AC power" | Easy to miss. With it set, a reboot on battery does not start the reader at all. |

`scripts\reader.ps1 status` audits the live task against the same definition `install` writes
and names anything that has moved; `install` backs the previous definition up to `logs/` first.

### Manage it

```powershell
scripts\reader.ps1 status | start | stop | restart | dev | log | install | spec
```

Under a service you lose auto-reload, so every code edit needs a bounce — use `restart`. Use
`dev` to run it in the foreground instead.

### Power settings

- Turn off AC sleep and hibernate, and set the lid action to "do nothing" on AC only.
- On the Wi-Fi adapter, turn off "Allow the computer to turn off this device to save power" — a
  NIC that powers down takes the tunnel with it.
- Check Windows Update active hours so a restart doesn't land mid-read.

> **Check:** reboot the PC, do not log in, and the reader still answers from the phone.

---

## Notes

- **Free tier limits.** The Tailscale personal plan's device and user limits change from time
  to time; check the current limits if you plan to add many devices.
- **SSE under waitress.** The dashboard uses server-sent events for batch progress. `threads=16`
  addresses pool exhaustion, but waitress coalesces small writes and every SSE event here is
  tiny. Watch a real batch-translate run from the dashboard; if events arrive in clumps rather
  than steadily, fall back to `app.run(debug=False, threaded=True)` under the same task. Keep to
  one process either way.
- **Back up `projects/`.** Putting the reader on a tailnet does nothing for durability. Your
  translation work lives in `projects/`, and a scheduled off-site backup (restic encrypts by
  default, which matters because the repo root holds a real `.env`) is worth more than any
  amount of uptime. Cover `projects/` plus the gitignored runtime configs. Verify by restoring
  into a scratch directory and opening a chapter from the restored tree — a backup job that
  reports success is not a verified backup.
