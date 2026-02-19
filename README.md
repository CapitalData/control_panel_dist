# Control Panel - Command Station

A military/industrial-themed dashboard to manage Python tools and Dash applications from a single interface. Styled as a retro tank console with brushed metal panels, Phillips-head screws, knobs, and glowing indicators.

![Theme: Military Console](https://img.shields.io/badge/Theme-Military%20Console-olive)
![Port: 8060](https://img.shields.io/badge/Port-8060-green)

## Features

- **Two Tabs**:
  - ⚡ UTILITIES - Python Tools (invoice-parser, invoice-generator)
  - 🚀 REACTORS - Dash Apps (learning-platform, finance-tracker, dash-cards)
- **Persona cockpits**:
  - `admin` keeps the existing tank-console look with every panel unlocked.
  - `scientist` swaps in a silver/blue aviator cockpit and exposes only the Learning Platform + Ollama LLM reactors for researchers.
- **Per-App Controls**:
  - Glowing status indicator (gray=stopped, green=running with pulsing glow)
  - "LAUNCH INTERFACE" button (for Dash apps) lights up green when active
  - Terminal-style output window with green-on-black text
  - ⚠ FORCE KILL button to immediately terminate a hung process and free the port for relaunch (it only kills the subprocess for that specific card, tears down its matching reverse proxy tunnel, and if the panel lost track of the PID it will hunt down any other listeners still bound to that port)
- **Reverse Proxy Tunnels**:
  - Bright tunnel indicator (blue=healthy, amber=degraded, red=failed)
  - Built-in TCP health probe (defaults to every 30s) so you know external access is live
- **Visual Design**:
  - Corner screws on all gauge panels
  - Decorative knobs and rivets
  - Warning stripes (hazard yellow/black)
  - Brass label plates
- **Auto-refresh**: Status updates every 2 seconds

## Persona Modes & Distribution

| Persona | Visual Treatment | Utilities Tab | Reactors Tab |
| --- | --- | --- | --- |
| `admin` (Company Admin) | Original olive military console | All Python tools | All Dash reactors |
| `scientist` (Research Scientist) | Blue/silver aviator cockpit | Hidden (only status note) | Learning Platform & Ollama LLM only |

Control which cockpit loads (and whether downstream operators can switch personas) with two environment variables before launching the panel:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONTROL_PANEL_DEFAULT_PERSONA` | `admin` | Persona to load on startup. Set to `scientist` when you ship the cockpit to researchers. |
| `CONTROL_PANEL_ALLOW_PERSONA_SWITCH` | `1` (true) | When `0`, the persona dropdown is disabled/hidden so downstream users cannot hop into admin view. Leave enabled on your local admin workstation so you can test every persona. |

Changing persona live swaps both the visible cards and the global styling so you can confirm exactly what another user will see without restarting the process.

## Controlling & Locking Personas for Distribution

When distributing the control panel to different users or clients, you can lock down which persona they see and prevent them from switching to other personas.

### Environment Variables

| Variable | Values | Default | Description |
|----------|--------|---------|-------------|
| `CONTROL_PANEL_DEFAULT_PERSONA` | `admin`, `scientist`, `developer`, `public_user` | `admin` | Which persona loads on startup |
| `CONTROL_PANEL_ALLOW_PERSONA_SWITCH` | `0` or `1` | `1` | When `0`, hides and disables the persona dropdown |

### Deployment Examples

**Scientist workstation** (locked to scientist persona):
```bash
export CONTROL_PANEL_DEFAULT_PERSONA=scientist
export CONTROL_PANEL_ALLOW_PERSONA_SWITCH=0
python controlpanel_app.py
```

**Admin workstation** (full access, can test all personas):
```bash
export CONTROL_PANEL_DEFAULT_PERSONA=admin
export CONTROL_PANEL_ALLOW_PERSONA_SWITCH=1
python controlpanel_app.py
```

**Using a `.env` file** (recommended for persistent config):
```bash
# .env file in the control_panel directory
CONTROL_PANEL_DEFAULT_PERSONA=scientist
CONTROL_PANEL_ALLOW_PERSONA_SWITCH=0
```

**Docker deployment**:
```dockerfile
ENV CONTROL_PANEL_DEFAULT_PERSONA=scientist
ENV CONTROL_PANEL_ALLOW_PERSONA_SWITCH=0
```

**systemd service**:
```ini
[Service]
Environment="CONTROL_PANEL_DEFAULT_PERSONA=scientist"
Environment="CONTROL_PANEL_ALLOW_PERSONA_SWITCH=0"
ExecStart=/usr/bin/python3 /opt/control-panel/controlpanel_app.py
```

### Security Considerations

⚠️ **Important**: The current persona lock is **client-side only**. This means:

| Attack Vector | Risk | Mitigation |
|---------------|------|------------|
| Browser DevTools | User can re-enable dropdown | Add server-side validation in callbacks |
| Environment tampering | User running locally can change env vars | Distribute as Docker container or hosted service |
| Direct API calls | Crafted requests bypass UI | Validate persona in all callbacks |

**For high-security deployments**, ensure your callbacks enforce the locked persona server-side:

```python
@callback(...)
def update_persona(selected_persona):
    # Server-side enforcement
    if not ALLOW_PERSONA_SWITCH:
        selected_persona = DEFAULT_PERSONA_ID  # Ignore user's choice
    # ... rest of callback
```

### Customizing Available Apps per Persona

Edit `apps_config.yaml` to control which apps each persona can access:

```yaml
personas:
  scientist:
    name: Research Scientist
    skin: titanium
    allowed_tools: []  # No utility tools
    allowed_dash_apps:
      - learning-platform
      - ollama-llm
```

To create a minimal distribution, you can also remove apps from the `python_tools` and `dash_apps` sections entirely—if an app isn't defined, no persona can access it.

## Technical Notes
### Subprocess Management
When launching Dash apps from this control panel, the following environment modifications are applied:

```python
env.pop('WERKZEUG_SERVER_FD', None)      # Remove socket file descriptor
env.pop('WERKZEUG_RUN_MAIN', None)       # Remove reloader flag
env['LAUNCHED_FROM_CONTROL_PANEL'] = 'true'  # Custom detection flag
# Popen with close_fds=True, start_new_session=True
```

**Important**: Managed Dash apps must detect this environment and disable debug mode:

```python
# In your Dash app's main block (required for all apps):
import os  # Make sure os is imported

if __name__ == "__main__":
    # Disable debug mode entirely if running from control panel
    from_control_panel = os.environ.get('LAUNCHED_FROM_CONTROL_PANEL') == 'true'
    app.run_server(
        debug=not from_control_panel,  # Disable debug when from control panel
        port=8050  # or appropriate port
```
KeyError: 'WERKZEUG_SERVER_FD'
```

**Trade-off**: When launched from the control panel, apps run without debug mode (no hot-reload, no debug toolbar). This is acceptable since the control panel provides process management and output viewing. Run apps standalone for development with full debug features.

**Apps that have been updated**:
- ✅ learning_platform/app.py
- ✅ dash_cards_frontends/app.py  
- ✅ finance_tracker/dash_app.py

### Reverse Proxy Switches

Each Dash reactor can punch a reverse SSH tunnel into your virtual server so you can reach it from outside the LAN. The control panel spawns `ssh -R <remote_host_port>:localhost:<local_port>` in a background session, watches the process, and pings the exposed remote port over TCP to make sure the route is still alive. This TCP "ping" is more reliable than an ICMP echo because it proves the listening socket on the remote host is reachable, not just that the host responds to pings.

Configure the tunnels via environment variables before launching the control panel. One set of variables per app:

| App | Required vars | Optional vars |
| --- | --- | --- |
| Learning Platform | `LEARNING_PLATFORM_PROXY_HOST`, `LEARNING_PLATFORM_PROXY_USER`, `LEARNING_PLATFORM_PROXY_REMOTE_PORT` | `LEARNING_PLATFORM_PROXY_KEY_PATH`, `LEARNING_PLATFORM_PROXY_BIND`, `LEARNING_PLATFORM_PROXY_SSH_ARGS`, `LEARNING_PLATFORM_PROXY_KEEPALIVE_INTERVAL`, `LEARNING_PLATFORM_PROXY_KEEPALIVE_COUNT`, `LEARNING_PLATFORM_PROXY_HEALTH_INTERVAL`, `LEARNING_PLATFORM_PROXY_HEALTH_TIMEOUT`, `LEARNING_PLATFORM_PROXY_HEALTH_ENABLED` |
| Finance Tracker | `FINANCE_TRACKER_PROXY_HOST`, `FINANCE_TRACKER_PROXY_USER`, `FINANCE_TRACKER_PROXY_REMOTE_PORT` | `FINANCE_TRACKER_PROXY_KEY_PATH`, `FINANCE_TRACKER_PROXY_BIND`, `FINANCE_TRACKER_PROXY_SSH_ARGS`, `FINANCE_TRACKER_PROXY_KEEPALIVE_INTERVAL`, `FINANCE_TRACKER_PROXY_KEEPALIVE_COUNT`, `FINANCE_TRACKER_PROXY_HEALTH_INTERVAL`, `FINANCE_TRACKER_PROXY_HEALTH_TIMEOUT`, `FINANCE_TRACKER_PROXY_HEALTH_ENABLED` |
| Dash Cards | `DASH_CARDS_PROXY_HOST`, `DASH_CARDS_PROXY_USER`, `DASH_CARDS_PROXY_REMOTE_PORT` | `DASH_CARDS_PROXY_KEY_PATH`, `DASH_CARDS_PROXY_BIND`, `DASH_CARDS_PROXY_SSH_ARGS`, `DASH_CARDS_PROXY_KEEPALIVE_INTERVAL`, `DASH_CARDS_PROXY_KEEPALIVE_COUNT`, `DASH_CARDS_PROXY_HEALTH_INTERVAL`, `DASH_CARDS_PROXY_HEALTH_TIMEOUT`, `DASH_CARDS_PROXY_HEALTH_ENABLED` |
- The remote server must allow `GatewayPorts yes` if you want to reach the tunnel from the public internet.
- Use SSH keys (`*_PROXY_KEY_PATH`) instead of passwords. The control panel simply shells out to your local `ssh` binary.
- Health checks are TCP connections to the remote host/port (not ICMP). Adjust the interval/timeout via the `*_PROXY_HEALTH_*` variables if you need faster/slower probes or disable them with `*_PROXY_HEALTH_ENABLED=0`.

### Adding New Reactor Elements

You don’t have to touch the Dash layout to add another reactor card—the UI is generated from two Python lists:

1. **Dash “reactors”** live in `DASH_APPS` (see `controlpanel_app.py`). Append a new dict with:
  - `id`: short unique slug (used for component IDs and global state keys).
  - `name`: label shown on the brass plate.
  - `path`: absolute `Path` to the entry script that should be launched (e.g., `BASE_DIR / "ollama" / "serve.py"`).
  - `port`: listening port for the service; the Launch button/link will target `http://localhost:<port>`.
  - `description`: short text for the right-hand info block.
  - `reverse_proxy`: optional `build_proxy_config("YOUR_PREFIX", defaults={"remote_port": 19xxx})` call if the card needs a tunnel toggle. Leave the key out to disable the proxy switch.
2. **Utility/terminal cards** live in `PYTHON_TOOLS`. Drop in another dict with `id`, `name`, `path`, `type` (`"script"` or `"notebook"`), and `description` if you’re wiring a CLI helper instead of a web port.

Once the config entry exists, the panel automatically renders a matching gauge card with the toggle, status light, kill controls, terminal stream, and (for reactor entries) proxy switch. Just make sure the underlying script honors `LAUNCHED_FROM_CONTROL_PANEL=true` so it disables hot reload and that it binds to the port declared in your config.

### Next Steps

1. Export the environment variables listed above for every reactor (host, user, remote port plus any optional key path, keepalive, or health intervals), confirm your jump host allows `GatewayPorts yes`, and make sure `ssh` is available on the control-panel machine.
2. Launch the panel with `python controlpanel_app.py`, flip the IGNITION switch to start a reactor, then toggle PROXY to open the tunnel and watch the indicator for the blue "healthy" glow.

## Apps Managed

### Python Tools
- **Invoice Parser** - Parse invoice PDFs and extract data
- **Invoice Generator** - Generate invoices (Jupyter Notebook - manual open)

If the Invoice Tool shows demo/empty data, use the reconnect guide:
- [finance_tracker/QUICKSTART.md](../finance_tracker/QUICKSTART.md#reconnect-to-existing-historical-data)

### Dash Applications  
- **Learning Platform** (port 8050) - Educational platform with courses
- **Finance Tracker** (port 8051) - Financial tracking dashboard
- **Dash Cards Frontend** (port 8052) - Card-based UI components
- **Ollama LLM Frontend** (port 11434) - SciOps cockpit to drive the local Ollama stack

## Usage

```bash
cd control_panel
conda activate bootcamp3 # or wherever you installed requirements
python controlpanel_app.py
```

Then open: http://localhost:8060

### How to Use
1. Flip the toggle switch next to an app to start it
2. Watch the status indicator glow green when running
3. View system logs in the terminal window (opens automatically)
4. For Dash apps, click "▶ LAUNCH INTERFACE" button when lit
5. Flip the switch off to stop the app

## Onboard a New User

1. **Pick their persona**: Decide whether the user should receive the full admin cockpit or the restricted scientist cockpit. Set `CONTROL_PANEL_DEFAULT_PERSONA` accordingly in the `.env`, shell profile, or `.service` unit you ship to them.
2. **Lock (or unlock) persona switching**: For downstream operators, export `CONTROL_PANEL_ALLOW_PERSONA_SWITCH=0` so the dropdown never appears. Keep it at `1` on your admin workstation so you can audit every persona when developing.
3. **Share the right reactors**: Verify only the Dash apps listed for that persona are configured and reachable on their machine (for scientists, only confirm Learning Platform + Ollama LLM ports and proxy settings).
4. **Hand off quick-start steps**: Provide the user with the “Usage” section (above) and any VPN/SSH credentials needed for their tunnels. Encourage them to watch the persona badge at the top of the console—it confirms which cockpit is active.
5. **Verify first launch**: Jump on a quick call or run a remote screen share the first time they launch to ensure the correct theme, panels, and proxy settings load. Adjust persona env vars if you need to promote/demote access later.

## Requirements

```bash
pip install dash dash-bootstrap-components psutil
```

## Notes

- Each Dash app runs on its own port to avoid conflicts
- The control panel runs on port 8060
- Jupyter notebooks must be opened manually (cannot be run as background process)
- Output is limited to last 100 lines per app
- Apps are gracefully terminated when stopped
