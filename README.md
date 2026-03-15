# Roblox Auto Rejoin — Android ROOT

Automatically rejoins a Roblox server when the game window closes or the app stops. Runs on Android via Termux with root access.

---

## How It Works

The script opens a persistent root shell on startup (single `su` call — no repeated permission toasts) and polls the game state every 3 seconds using `pidof`, `ps`, and `dumpsys`.

It tracks three states:

- **Foreground** — game is running and visible, do nothing
- **Background** — window closed but process still alive, force-stop and rejoin
- **Not running** — process gone, rejoin immediately

When a rejoin is triggered, the app is force-stopped first to avoid launch conflicts, then an `am start` intent is fired to reopen the game. If the game fails to reach foreground within 90 seconds, it force-stops and tries again.

Config (server link + cookie) is saved encrypted to `.roblox_config.json` after the first run and reused automatically. The cookie is encrypted using the device's Android ID as the key and never leaves the device.

---

## Pause / Resume

```bash
touch /tmp/roblox_pause   # pause
rm /tmp/roblox_pause      # resume
```
