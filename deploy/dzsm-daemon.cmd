@echo off
REM ---------------------------------------------------------------------------
REM Launcher for the dzsm daemon, meant to be the action of a scheduled task.
REM
REM Why a wrapper instead of pointing the task straight at dzsm.exe:
REM
REM A scheduled task's environment depends on the account it runs as, whether
REM that account's profile gets loaded, and whether anyone has logged on yet
REM since boot. `setx` variables usually arrive, but "usually" is a poor
REM foundation for a server that is supposed to come up unattended after a
REM power cut. Setting them here makes it deterministic: the daemon gets
REM exactly these values, under any account, logged on or not.
REM
REM It is also the only place to look when something is missing, rather than
REM three places (registry, task definition, config.toml).
REM ---------------------------------------------------------------------------

REM --- where things are ------------------------------------------------------
REM Adjust these two and nothing else should need changing.
set "DZSM_HOME=D:\dzsm"
set "DZSM_CONFIG=D:\dzsm\config.toml"

REM --- credentials -----------------------------------------------------------
REM Steam Web API key. Only needed for Workshop *search*; pasting a Workshop
REM link or a bare id uses a public endpoint and works without one.
REM Get one at https://steamcommunity.com/dev/apikey
set "DZSM_STEAM_API_KEY="

REM API token for the web UI. Leave empty to keep the dashboard on loopback
REM only - dzsm refuses to bind a non-loopback address without a token, which
REM is what keeps an unauthenticated dashboard off your network.
set "DZSM_API_TOKEN="

REM Steam password. Normally leave this EMPTY: run `dzsm login --interactive`
REM once by hand and steamcmd caches a sentry under <root>\steamcmd\config\,
REM after which it signs in with no password anywhere on disk. Setting this
REM puts the password on a steamcmd command line, which any local process can
REM read on Windows.
set "DZSM_STEAM_PASSWORD="

REM --- go --------------------------------------------------------------------
REM Working directory matters: a bare `config.toml` would otherwise resolve
REM relative to C:\Windows\System32.
cd /d "%DZSM_HOME%" || (echo cannot cd to %DZSM_HOME% & exit /b 1)

REM No `start`, no `cmd /c` wrapper: the task should track this process
REM directly so stopping the task actually stops the daemon.
"%DZSM_HOME%\dzsm.exe" daemon --config "%DZSM_CONFIG%"
