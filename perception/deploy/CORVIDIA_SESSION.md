# Optional Corvidia graphical session

This adds a **Corvidia console (Xorg)** choice at the GDM login screen. It runs
Openbox and the native console, without GNOME Shell, a dock, or desktop autostart
programs. The normal Ubuntu session remains available.

The existing onboard service must already be active, expose mission API 2, and
have user service lingering enabled. The launcher reads its mode and opens the
console in observation or hardware mode accordingly. Hardware still requires
all five readiness observations before an explicit mission submission.
Telemetry-only mode is rejected with an on-screen explanation.

## Installation and selection

An administrator installs the distribution's `openbox` and `xterm` packages,
then registers the desktop entry. Keep these files together in the
deployed checkout's `perception/deploy` directory:

- `corvidia-session.sh` (executable)
- `corvidia-openbox.xml`
- `corvidia-session.desktop.in`
- `corvidia-session-wrapper.sh.in`

The GDM greeter must be able to execute the desktop entry's `TryExec` path
**before** user login. A private home directory such as `/home/jlam` with mode
0750 blocks this lookup, even when the script itself is executable. Preserve
the home directory's permissions and install a small public wrapper:

1. Replace `@CORVIDIA_PRIVATE_SESSION_LAUNCHER@` in the wrapper template with
   `/home/jlam/corvidia/perception/deploy/corvidia-session.sh` (or the chosen
   absolute checkout path). Install the result as root-owned mode 0755 at
   `/usr/local/bin/corvidia-session`.
2. Replace **both** `@CORVIDIA_PUBLIC_SESSION_WRAPPER@` placeholders in the
   desktop template with `/usr/local/bin/corvidia-session`. Validate the result
   with `desktop-file-validate`, then install it as root-owned mode 0644 at
   `/usr/share/xsessions/corvidia.desktop`.
3. Verify `sudo -u gdm test -x /usr/local/bin/corvidia-session` succeeds. The
   private checkout need not be accessible to the greeter: the wrapper accesses
   it only after GDM starts the session as the authenticated user.

The deployed checkout path should be stable. Update the wrapper before removing
the release it references. Do not symlink the private launcher alone into
`/usr/local/bin`: it resolves the Python environment and Openbox configuration
relative to its own location. The public wrapper uses `exec` and does not add a
desktop shell, start services, or change authentication settings.

At the login screen, select the regular user, choose **Corvidia console (Xorg)**
from the session gear, and log in normally. Select **Ubuntu** from the same menu
to return to the normal desktop. This does not require changing authentication,
autologin, the default boot target, or any backend service settings.

### Optional display preference on this Jetson

GDM sources the user's `~/.xprofile` before starting the graphical session. Keep
monitor-specific preferences there rather than in the shared launcher. For the
current DP-0 display, this stanza keeps the Corvidia session at 1920×1080, 60 Hz
and identifies its desktop environment correctly:

```sh
if [ "${DESKTOP_SESSION:-}" = "corvidia" ]; then
    export XDG_CURRENT_DESKTOP=Corvidia
    if command -v xrandr >/dev/null 2>&1; then
        xrandr --output DP-0 --mode 1920x1080 --rate 60 || :
    fi
fi
```

Preserve any existing `.xprofile` content, check it with `sh -n ~/.xprofile`, and
log in again to apply it. The conditional leaves other desktop sessions alone;
adjust the output and supported mode if the physical display changes. It does
not start, stop, or configure the mission backend.

## Closing and failures

The main console opens maximized with a visible window Close button; readiness
and details dialogs keep their own decorations. Close or Alt+F4 ends this
graphical session. **Closing the console does not stop an onboard mission.**
Use **Abort mission** when a mission must stop, and wait for its verified result
before leaving the console.

The launcher starts one GUI and does not restart it after an error. If the
backend is missing, its mode is unsupported, or the GUI fails, a small terminal
explains the failure. Press Enter to return to login. No backend service is
started, stopped, switched, or sent a motor command by the session launcher.
Its normal exit terminates only the Openbox process it created; session signals
also terminate its own GUI child. Display variables come from GDM and are never
imported into the onboard service.

Openbox's per-window matching and decoration settings follow its
[application configuration](https://openbox.org/help/Applications) and
[configuration reference](https://openbox.org/help/Configuration).
