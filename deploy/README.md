# Running dzsm at machine startup (Windows)

`dzsm daemon` is the long-running form: it supervises instances, restarts them on
schedule, and serves the dashboard, all in one process. These two files make it start
with the machine.

```
copy dzsm-daemon.cmd  ->  D:\dzsm\dzsm-daemon.cmd     (then edit the paths and key)
powershell -File install-task.ps1                      (elevated)
Start-ScheduledTask -TaskName dzsm
```

## Why a wrapper script

The task points at `dzsm-daemon.cmd`, not at `dzsm.exe`. The wrapper sets the environment
variables explicitly.

A scheduled task's environment depends on which account it runs as, whether that
account's profile is loaded, and whether anyone has logged on since boot. `setx`
variables usually do arrive — but "usually" is a poor foundation for a server meant to
come back up unattended after a power cut, and when they don't arrive the symptom is
oblique (Workshop search quietly stops working, and nothing says why). Setting them in the
wrapper makes it deterministic under any account, logged on or not, and leaves exactly one
place to look.

## Why no password

`Register-ScheduledTask -LogonType Password` does not prompt the way the Task Scheduler
GUI does — it needs `-Password` passed explicitly, and with a Microsoft account the
password it wants is the Microsoft account one rather than your PIN. Both are avoidable:

- **SYSTEM** (the default here) needs no password and exists before any logon, so the task
  can genuinely run at startup on an unattended box. The trade-off is that your DayZ
  servers end up running as SYSTEM too, which is more privilege than a game server needs.
- **`-AsCurrentUser`** uses S4U — runs as you, logged on or not, still no stored password.
  The trade-off is no access to network shares or mapped drives, which only matters if
  something dzsm touches lives on a NAS.

Prefer `-AsCurrentUser` if everything is on local disk.

## The setting that matters most

Task Scheduler defaults to **"Stop the task if it runs longer than 3 days."** That would
kill the daemon every 72 hours, and it is the usual reason a startup daemon appears to die
on its own. `install-task.ps1` sets no limit and then reads the value back to check,
because this is the one worth verifying rather than assuming.

## What does not need solving

**Steam Guard.** steamcmd caches its sentry under `<root>\steamcmd\config\`, not in a user
profile, so the task does not have to run as the account that ran
`dzsm login --interactive`. Any account that can read and write the dzsm root will find
the cached credentials.

Run `dzsm login --interactive` by hand once, and leave `DZSM_STEAM_PASSWORD` empty — then
no Steam password exists anywhere on disk or on a command line.

## Checking it

```powershell
Get-ScheduledTaskInfo -TaskName dzsm
```

`LastTaskResult` of `267009` means "currently running", which is the healthy state for a
daemon — not `0`. Then open `http://127.0.0.1:8080/` and confirm the dashboard answers.

If it starts and immediately stops, run the wrapper by hand in a console first: the daemon
prints its bind address and any config error, and a scheduled task throws that output away.

## Stopping it

```powershell
Stop-ScheduledTask -TaskName dzsm
```

Instances are deliberately **left running** when the daemon exits — dzsm going down must
not take the game servers with it. It adopts them again from `state.json` when it comes
back. To bring a server down, stop the instance (which records a hold so the supervisor
does not restart it), not the daemon.

## Reaching the dashboard from another PC

Off by default: the dashboard binds `127.0.0.1` and only this machine can reach it. Four
steps to open it to the rest of your LAN, and dzsm refuses to start if you skip either
of the first two.

**1. A token.** Every write, and every read that shows passwords or player data, needs
it. Generate a long random one and put it in `dzsm-daemon.cmd`:

```bat
set "DZSM_API_TOKEN=paste-a-long-random-string-here"
```

```powershell
-join ((1..40) | ForEach-Object { [char](Get-Random -InputObject (48..57 + 65..90 + 97..122)) })
```

**2. HTTPS.** Without it, that token crosses the network in plain text on every request,
and anyone who captures it has full control. In `config.toml`:

```toml
[api]
host = "192.168.1.150"   # this machine's LAN address
tls = true
```

dzsm makes its own certificate under `<root>\tls\` on first start and reuses it after.
Prefer the specific LAN address over `0.0.0.0`, which also listens on VPN and virtual
adapters.

**3. The firewall,** scoped to your local subnet on Private networks only, so a router
port-forward does not expose it:

```powershell
.\install-task.ps1 -OpenFirewall -Port 8080
```

**4. Check the fingerprint the first time — this is the step not to skip.** The browser
will warn, because the certificate is self-signed. The daemon prints a line like:

```
certificate fingerprint (SHA-256): 3A:F1:...:9C
```

and the dashboard shows the same string at the bottom of every page. In the browser
warning, open the certificate details and compare its SHA-256 fingerprint with that.
**If they match, it is your server — accept it.** If they do not, something else on the
network is answering in its place; do not type the token in.

Each PC asks once. The certificate is kept for as long as it is valid and still names
this machine's address, so you will only see the warning again if the server's IP
changes or the certificate nears expiry (about two years).

Prefer no warning at all? Issue a certificate from your own CA and point
`cert_file`/`key_file` under `[api]` at it; dzsm uses those as given.
