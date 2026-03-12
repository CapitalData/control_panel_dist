"""
Control Panel - Application Manager
====================================
Manage Python tools and Dash apps from a single interface.

Technical Notes:
----------------
- Dash apps are spawned as subprocesses with modified environment variables:
  - WERKZEUG_RUN_MAIN='true' - Disables Flask's reloader in child processes
  - WERKZEUG_SERVER_FD removed - Prevents socket inheritance conflicts
  - close_fds=True and start_new_session=True - Full process isolation

- Each managed app must handle WERKZEUG_RUN_MAIN='true' to disable hot-reload:
    use_reloader = os.environ.get('WERKZEUG_RUN_MAIN') != 'true'
    app.run_server(..., use_reloader=use_reloader)

- This control panel runs on port 8060
- Status updates are configurable via CONTROL_PANEL_STATUS_INTERVAL_MS

Usage:
------
1. Toggle switch to start/stop applications
2. Output console shows stdout/stderr from each process
3. "Open" link available when Dash apps are running

Military Console Theme:
-----------------------
Styled as a retro tank/industrial control panel with:
- Brushed metal textures
- Corner screws (Phillips head)
- Industrial warning labels
- Gauge-style indicator panels
- Heavy-duty toggle switches
"""
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import json
import psutil
import yaml

import dash
import dash_bootstrap_components as dbc
from dash import (ALL, MATCH, ClientsideFunction, Input, Output, State, callback,
                  clientside_callback, ctx, dcc, html, no_update)

# Initialize the app with dark military theme
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    suppress_callback_exceptions=True,
)

# Custom CSS is now loaded from assets/custom.css


def env_int(name, default=None):
    """Read an environment variable as int with a fallback."""
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name, default=None):
    """Read an environment variable as float with a fallback."""
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_bool(name, default=False):
    """Read an environment variable as boolean."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_proxy_config(prefix, *, defaults=None):
    """Build reverse proxy configuration from environment variables."""
    defaults = defaults or {}
    base = prefix.upper()
    config = {
        "env_prefix": f"{base}_PROXY",
        "host": os.environ.get(f"{base}_PROXY_HOST", defaults.get("host")),
        "user": os.environ.get(f"{base}_PROXY_USER", defaults.get("user")),
        "remote_port": env_int(f"{base}_PROXY_REMOTE_PORT", defaults.get("remote_port")),
        "bind_address": os.environ.get(
            f"{base}_PROXY_BIND", defaults.get("bind_address", "0.0.0.0")
        ),
        "ssh_key_path": os.environ.get(
            f"{base}_PROXY_KEY_PATH", defaults.get("ssh_key_path")
        ),
        "keepalive_interval": env_int(
            f"{base}_PROXY_KEEPALIVE_INTERVAL", defaults.get("keepalive_interval", 30)
        ),
        "keepalive_count": env_int(
            f"{base}_PROXY_KEEPALIVE_COUNT", defaults.get("keepalive_count", 3)
        ),
        "healthcheck_interval": env_int(
            f"{base}_PROXY_HEALTH_INTERVAL", defaults.get("healthcheck_interval", 30)
        ),
        "healthcheck_timeout": env_float(
            f"{base}_PROXY_HEALTH_TIMEOUT", defaults.get("healthcheck_timeout", 2.0)
        ),
        "healthcheck_enabled": env_bool(
            f"{base}_PROXY_HEALTH_ENABLED", defaults.get("healthcheck_enabled", True)
        ),
        "healthcheck_host": os.environ.get(
            f"{base}_PROXY_HEALTH_HOST", defaults.get("healthcheck_host")
        ),
        "ssh_args": list(defaults.get("ssh_args", [])),
    }

    raw_args = os.environ.get(f"{base}_PROXY_SSH_ARGS")
    if raw_args:
        config["ssh_args"] = shlex.split(raw_args)

    config["configured"] = all(
        [config.get("host"), config.get("user"), config.get("remote_port") is not None]
    )
    return config


def _find_pids_listening_on_port(port):
    """Return a set of process IDs currently bound to a local TCP port."""
    pids = set()
    try:
        for conn in psutil.net_connections(kind='inet'):
            laddr = getattr(conn, "laddr", None)
            if not laddr:
                continue
            if getattr(laddr, "port", None) != port:
                continue
            if conn.pid:
                pids.add(conn.pid)
    except psutil.AccessDenied:
        pass
    except psutil.Error:
        return set()

    if pids:
        return pids

    # Fallback: iterate processes we can access
    try:
        for proc in psutil.process_iter(['pid']):
            pid = proc.info.get('pid')
            if pid is None:
                continue
            try:
                for conn in proc.connections(kind='inet'):
                    laddr = getattr(conn, "laddr", None)
                    if not laddr:
                        continue
                    if getattr(laddr, "port", None) != port:
                        continue
                    pids.add(pid)
                    break
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except psutil.Error:
        return pids
    return pids


def kill_processes_by_port(port, *, exclude_pids=None):
    """Force kill any processes listening on the specified port."""
    exclude = set(exclude_pids or [])
    target_pids = _find_pids_listening_on_port(port) - exclude
    killed = []
    for pid in target_pids:
        try:
            proc = psutil.Process(pid)
            proc.kill()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                pass
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


# Robust base directory: always resolve to the parent of control_panel_dist, regardless of launch location or env var
BASE_DIR = Path(__file__).resolve().parent.parent


def load_config():
    """Load application configuration from YAML file."""
    config_path = Path(__file__).parent / "apps_config.yaml"
    
    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Please create apps_config.yaml in the control_panel directory."
        )
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError("apps_config.yaml must define a top-level mapping")

    def _clean_entries(entries, section_name, required_keys):
        """Skip malformed/null list entries so commented YAML stubs don't crash startup."""
        cleaned = []
        for index, entry in enumerate(entries or []):
            if not isinstance(entry, dict):
                print(
                    f"[CONFIG][WARN] Skipping invalid {section_name}[{index}] entry: {entry!r}"
                )
                continue
            missing = [key for key in required_keys if not entry.get(key)]
            if missing:
                print(
                    f"[CONFIG][WARN] Skipping {section_name}[{index}] missing {', '.join(missing)}"
                )
                continue
            cleaned.append(entry)
        return cleaned

    config['python_tools'] = _clean_entries(
        config.get('python_tools', []),
        'python_tools',
        required_keys=('id', 'path')
    )
    config['dash_apps'] = _clean_entries(
        config.get('dash_apps', []),
        'dash_apps',
        required_keys=('id', 'path', 'port')
    )
    
    # Convert relative paths to absolute Path objects
    for tool in config.get('python_tools', []):
        tool['path'] = BASE_DIR / tool['path']
    
    for app in config.get('dash_apps', []):
        app['path'] = BASE_DIR / app['path']
        # Build reverse_proxy config from YAML structure
        if 'reverse_proxy' in app:
            rp = app['reverse_proxy']
            app['reverse_proxy'] = build_proxy_config(
                rp['env_prefix'],
                defaults={'remote_port': rp['remote_port']}
            )
    
    # Build personas from config
    personas = {}
    for persona_id, persona_data in config.get('personas', {}).items():
        if not isinstance(persona_data, dict):
            print(f"[CONFIG][WARN] Skipping invalid persona '{persona_id}'")
            continue
        if not persona_data.get('name'):
            print(f"[CONFIG][WARN] Skipping persona '{persona_id}' missing name")
            continue
        skin_id = persona_data.get('skin', 'default')
        skin_def = config.get('skins', {}).get(skin_id, {})
        theme_class = skin_def.get('css_class', f"persona-{persona_id}")
        personas[persona_id] = {
            "id": persona_id,
            "name": persona_data['name'],
            "description": persona_data.get('description', ''),
            "theme_class": theme_class,
            "allowed_tools": persona_data.get('allowed_tools', []),
            "allowed_dash_apps": persona_data.get('allowed_dash_apps', []),
        }
    
    return config, personas


# Load configuration from YAML
config, PERSONAS = load_config()
PYTHON_TOOLS = config['python_tools']
DASH_APPS = config['dash_apps']

TOOL_LOOKUP = {tool["id"]: tool for tool in PYTHON_TOOLS}
DASH_LOOKUP = {app["id"]: app for app in DASH_APPS}

_panel_groups = config.get('panel_groups', {})
LLM_TOOL_IDS = set(_panel_groups.get('llm', {}).get('tool_ids') or [])
LLM_DASH_IDS = set(_panel_groups.get('llm', {}).get('dash_ids') or [])
FINANCE_TOOL_IDS = set(_panel_groups.get('finance', {}).get('tool_ids') or [])
FINANCE_DASH_IDS = set(_panel_groups.get('finance', {}).get('dash_ids') or [])
MANAGER_TOOL_IDS = set(_panel_groups.get('manager', {}).get('tool_ids') or [])
MANAGER_DASH_IDS = set(_panel_groups.get('manager', {}).get('dash_ids') or [])
DEFAULT_PHOENIX_PROJECT_NAME = os.environ.get(
    "CONTROL_PANEL_PHOENIX_PROJECT_NAME",
    _panel_groups.get('llm', {}).get('project_name', 'default'),
)

DEFAULT_PERSONA_ID = os.environ.get("CONTROL_PANEL_DEFAULT_PERSONA", "admin").lower()
if DEFAULT_PERSONA_ID not in PERSONAS:
    DEFAULT_PERSONA_ID = "admin"

ALLOW_PERSONA_SWITCH = env_bool("CONTROL_PANEL_ALLOW_PERSONA_SWITCH", True)
STATUS_UPDATE_INTERVAL_MS = max(
    1000,
    env_int("CONTROL_PANEL_STATUS_INTERVAL_MS", 5000) or 5000,
)

PERSONA_OPTIONS = [
    {"label": data["name"], "value": persona_id}
    for persona_id, data in PERSONAS.items()
]


def get_persona(persona_id):
    """Return a persona configuration by id with admin fallback."""
    if persona_id in PERSONAS:
        return PERSONAS[persona_id]
    return PERSONAS["admin"]


# Global state management
app_processes = {}
app_outputs = {}
app_status = {}
proxy_processes = {}
proxy_status = {}
proxy_health = {}
proxy_last_check = {}
ui_render_state = {}

def init_state():
    """Initialize global state for all apps"""
    for tool in PYTHON_TOOLS:
        app_processes[tool["id"]] = None
        app_outputs[tool["id"]] = []
        app_status[tool["id"]] = "stopped"
        ui_render_state[tool["id"]] = {
            "running": False,
            "output_signature": (0, ""),
            "proxy_state": None,
            "proxy_message": None,
        }
    
    for app_config in DASH_APPS:
        app_processes[app_config["id"]] = None
        app_outputs[app_config["id"]] = []
        app_status[app_config["id"]] = "stopped"
        proxy_processes[app_config["id"]] = None
        proxy_status[app_config["id"]] = "inactive"
        ui_render_state[app_config["id"]] = {
            "running": False,
            "output_signature": (0, ""),
            "proxy_state": "inactive",
            "proxy_message": "Tunnel offline",
        }
        proxy_cfg = app_config.get("reverse_proxy") or {}
        if proxy_cfg.get("configured"):
            proxy_health[app_config["id"]] = {"state": "inactive", "message": "Tunnel offline"}
        else:
            env_hint = proxy_cfg.get("env_prefix", app_config["id"].upper())
            proxy_health[app_config["id"]] = {
                "state": "disabled",
                "message": f"Set {env_hint}_HOST/{env_hint}_USER/{env_hint}_REMOTE_PORT to enable"
            }
        proxy_last_check[app_config["id"]] = 0

init_state()


def sanitize_output_text(value):
    """Normalize process output so Dash JSON serialization is resilient."""
    text = str(value)
    text = text.replace("\x00", "")
    text = text.replace("\r", "")
    # Normalize backslashes to avoid malformed escape sequences in downstream JSON handling.
    text = text.replace("\\", "/")
    return text


def output_signature(app_id):
    """Cheap signature to detect console output changes."""
    lines = app_outputs.get(app_id, [])
    if not lines:
        return (0, "")
    return (len(lines), lines[-1])


def sanitize_project_name(value):
    """Normalize the user-selected Phoenix project name."""
    text = (value or "").strip()
    return text or "default"


def build_observability_env(project_name):
    """Environment variables that tag traces to a Phoenix project."""
    return {"PHOENIX_PROJECT_NAME": sanitize_project_name(project_name)}

def read_output(process, app_id):
    """Read process output in a separate thread"""
    try:
        for line in iter(process.stdout.readline, b''):
            if line:
                decoded = line.decode('utf-8', errors='replace').strip()
                decoded = sanitize_output_text(decoded)
                app_outputs[app_id].append(f"[{datetime.now().strftime('%H:%M:%S')}] {decoded}")
                # Keep only last 100 lines
                if len(app_outputs[app_id]) > 100:
                    app_outputs[app_id] = app_outputs[app_id][-100:]
    except Exception as e:
        app_outputs[app_id].append(f"[ERROR] {str(e)}")

def start_python_tool(tool_id, extra_env=None):
    """Start a Python tool"""
    tool = next((t for t in PYTHON_TOOLS if t["id"] == tool_id), None)
    if not tool:
        return False, "Tool not found"
    
    if app_processes.get(tool_id) and app_processes[tool_id].poll() is None:
        return False, "Already running"
    
    try:
        if tool["type"] == "notebook":
            return False, "Notebooks must be opened manually in Jupyter"

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        
        process = subprocess.Popen(
            [sys.executable, str(tool["path"])],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=tool["path"].parent,
            env=env,
            bufsize=1,
            close_fds=True,
            start_new_session=True
        )
        
        app_processes[tool_id] = process
        app_status[tool_id] = "running"
        app_outputs[tool_id] = [f"[{datetime.now().strftime('%H:%M:%S')}] Started {tool['name']}"]
        
        # Start output reader thread
        thread = threading.Thread(target=read_output, args=(process, tool_id), daemon=True)
        thread.start()
        
        return True, "Started successfully"
    except Exception as e:
        return False, f"Error: {str(e)}"


def get_dash_app(app_id):
    """Return Dash app configuration by id."""
    return next((a for a in DASH_APPS if a["id"] == app_id), None)


def start_dash_app(app_id, extra_env=None):
    """Start a Dash application"""
    app_config = get_dash_app(app_id)
    if not app_config:
        return False, "App not found"
    
    if app_processes.get(app_id) and app_processes[app_id].poll() is None:
        return False, "Already running"
    
    try:
        # Check if port is available (some apps can attach to an existing listener)
        allow_port_in_use = app_id in {"ollama-llm", "phoenix-arize"}
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                for conn in proc.connections():
                    if conn.laddr.port == app_config["port"]:
                        if allow_port_in_use:
                            app_outputs.setdefault(app_id, [])
                            app_outputs[app_id].append(
                                f"[{datetime.now().strftime('%H:%M:%S')}] Port {app_config['port']} already in use; attaching to existing service"
                            )
                            if len(app_outputs[app_id]) > 100:
                                app_outputs[app_id] = app_outputs[app_id][-100:]
                            break
                        return False, f"Port {app_config['port']} already in use"
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        
        env = os.environ.copy()
        # Remove Flask/Werkzeug reloader environment variables
        env.pop('WERKZEUG_SERVER_FD', None)
        env.pop('WERKZEUG_RUN_MAIN', None)
        # Set custom flag for apps to detect control panel launch
        env['LAUNCHED_FROM_CONTROL_PANEL'] = 'true'
        if extra_env:
            env.update(extra_env)
        
        process = subprocess.Popen(
            [sys.executable, str(app_config["path"])],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=app_config["path"].parent,
            env=env,
            bufsize=1,
            close_fds=True,
            start_new_session=True
        )
        
        app_processes[app_id] = process
        app_status[app_id] = "running"
        app_outputs[app_id] = [f"[{datetime.now().strftime('%H:%M:%S')}] Started {app_config['name']} on port {app_config['port']}"]
        
        # Start output reader thread
        thread = threading.Thread(target=read_output, args=(process, app_id), daemon=True)
        thread.start()
        
        return True, "Started successfully"
    except Exception as e:
        return False, f"Error: {str(e)}"


def read_proxy_output(process, app_id):
    """Capture stdout from the reverse proxy tunnel."""
    app_outputs.setdefault(app_id, [])
    try:
        for line in iter(process.stdout.readline, b''):
            if line:
                decoded = line.decode('utf-8', errors='replace').strip()
                decoded = sanitize_output_text(decoded)
                app_outputs[app_id].append(
                    f"[{datetime.now().strftime('%H:%M:%S')}] [PROXY] {decoded}"
                )
                if len(app_outputs[app_id]) > 100:
                    app_outputs[app_id] = app_outputs[app_id][-100:]
    except Exception as exc:
        app_outputs[app_id].append(f"[ERROR][PROXY] {str(exc)}")


def start_reverse_proxy(app_id):
    """Start an SSH reverse proxy for a Dash application."""
    app_config = get_dash_app(app_id)
    if not app_config:
        return False, "App not found"

    proxy_cfg = app_config.get("reverse_proxy") or {}
    if not proxy_cfg.get("configured"):
        env_hint = proxy_cfg.get("env_prefix", app_config["id"].upper())
        return False, (
            f"Reverse proxy not configured. Set {env_hint}_HOST, {env_hint}_USER and "
            f"{env_hint}_REMOTE_PORT."
        )

    existing = proxy_processes.get(app_id)
    if existing and existing.poll() is None:
        return False, "Reverse proxy already active"

    if not shutil.which("ssh"):
        return False, "'ssh' command not available on PATH"

    bind_address = proxy_cfg.get("bind_address", "0.0.0.0")
    remote_port = proxy_cfg.get("remote_port")
    local_port = app_config.get("port")
    destination = f"{proxy_cfg['user']}@{proxy_cfg['host']}"

    ssh_command = ["ssh", "-o", "ExitOnForwardFailure=yes"]
    ssh_command.extend(
        [
            "-o",
            f"ServerAliveInterval={proxy_cfg.get('keepalive_interval', 30)}",
            "-o",
            f"ServerAliveCountMax={proxy_cfg.get('keepalive_count', 3)}",
        ]
    )

    key_path = proxy_cfg.get("ssh_key_path")
    if key_path:
        ssh_command.extend(["-i", os.path.expanduser(key_path)])

    for arg in proxy_cfg.get("ssh_args", []):
        ssh_command.append(arg)

    ssh_command.extend(
        [
            "-R",
            f"{bind_address}:{remote_port}:localhost:{local_port}",
            "-N",
            destination,
        ]
    )

    try:
        process = subprocess.Popen(
            ssh_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            close_fds=True,
            start_new_session=True,
        )
    except Exception as exc:
        return False, f"Proxy error: {exc}"

    proxy_processes[app_id] = process
    proxy_status[app_id] = "active"
    proxy_health[app_id] = {
        "state": "starting",
        "message": f"Establishing tunnel to {proxy_cfg['host']}:{remote_port}",
    }

    thread = threading.Thread(target=read_proxy_output, args=(process, app_id), daemon=True)
    thread.start()

    app_outputs.setdefault(app_id, [])
    app_outputs[app_id].append(
        f"[{datetime.now().strftime('%H:%M:%S')}] [PROXY] Tunnel started at {proxy_cfg['host']}:{remote_port}"
    )
    if len(app_outputs[app_id]) > 100:
        app_outputs[app_id] = app_outputs[app_id][-100:]
    return True, f"Reverse proxy established on {proxy_cfg['host']}:{remote_port}"


def stop_reverse_proxy(app_id):
    """Stop the SSH reverse proxy."""
    process = proxy_processes.get(app_id)

    if not process or process.poll() is not None:
        proxy_processes[app_id] = None
        proxy_status[app_id] = "inactive"
        proxy_health[app_id] = {"state": "inactive", "message": "Tunnel offline"}
        proxy_last_check[app_id] = 0
        return True, "Reverse proxy already stopped"

    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    except Exception as exc:
        return False, f"Proxy stop error: {exc}"

    proxy_processes[app_id] = None
    proxy_status[app_id] = "inactive"
    proxy_health[app_id] = {"state": "inactive", "message": "Tunnel offline"}
    proxy_last_check[app_id] = 0
    app_outputs.setdefault(app_id, [])
    app_outputs[app_id].append(
        f"[{datetime.now().strftime('%H:%M:%S')}] [PROXY] Tunnel stopped"
    )
    if len(app_outputs[app_id]) > 100:
        app_outputs[app_id] = app_outputs[app_id][-100:]
    return True, "Reverse proxy stopped"


def update_proxy_health(app_id, *, force=False):
    """Refresh the recorded health of the reverse proxy tunnel."""
    app_config = get_dash_app(app_id)
    proxy_cfg = app_config.get("reverse_proxy") if app_config else None
    if not proxy_cfg:
        proxy_health[app_id] = {"state": "disabled", "message": "Reverse proxy unavailable"}
        return

    process = proxy_processes.get(app_id)
    if not process or process.poll() is not None:
        if proxy_cfg.get("configured"):
            exit_code = process.poll() if process else None
            if exit_code not in (None, 0) and proxy_status.get(app_id) == "active":
                proxy_health[app_id] = {
                    "state": "error",
                    "message": f"Tunnel exited (code {exit_code})",
                }
            elif not proxy_health.get(app_id):
                proxy_health[app_id] = {"state": "inactive", "message": "Tunnel offline"}
            else:
                proxy_health[app_id]["state"] = "inactive"
                proxy_health[app_id]["message"] = "Tunnel offline"
        else:
            env_hint = proxy_cfg.get("env_prefix", app_id.upper())
            proxy_health[app_id] = {
                "state": "disabled",
                "message": f"Set {env_hint}_HOST/{env_hint}_USER/{env_hint}_REMOTE_PORT",
            }
        proxy_status[app_id] = "inactive"
        return

    proxy_status[app_id] = "active"
    interval = proxy_cfg.get("healthcheck_interval", 30)
    now = time.time()
    if not force and now - proxy_last_check.get(app_id, 0) < interval:
        return

    proxy_last_check[app_id] = now

    if not proxy_cfg.get("healthcheck_enabled", True):
        proxy_health[app_id] = {"state": "active", "message": "Health check disabled"}
        return

    target_host = proxy_cfg.get("healthcheck_host") or proxy_cfg.get("host")
    remote_port = proxy_cfg.get("remote_port")
    if not target_host or remote_port is None:
        proxy_health[app_id] = {"state": "active", "message": "Health target undefined"}
        return

    timeout = proxy_cfg.get("healthcheck_timeout", 2.0)
    try:
        with socket.create_connection((target_host, remote_port), timeout=timeout):
            proxy_health[app_id] = {
                "state": "healthy",
                "message": f"{target_host}:{remote_port} reachable",
            }
    except OSError as exc:
        proxy_health[app_id] = {
            "state": "degraded",
            "message": f"Health probe failed ({exc.__class__.__name__})",
        }

def stop_app(app_id):
    """Stop an application"""
    process = app_processes.get(app_id)
    if not process or process.poll() is not None:
        return False, "Not running"
    
    try:
        # Try graceful shutdown first
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Force kill if needed
            process.kill()
            process.wait()
        
        app_processes[app_id] = None
        app_status[app_id] = "stopped"
        app_outputs[app_id].append(f"[{datetime.now().strftime('%H:%M:%S')}] Stopped")
        return True, "Stopped successfully"
    except Exception as e:
        return False, f"Error: {str(e)}"


def force_kill_app(app_id):
    """Aggressively kill an application process and release its port."""
    process = app_processes.get(app_id)
    app_config = get_dash_app(app_id)
    port = app_config.get("port") if app_config else None

    killed_pids = []
    log_messages = []

    if process and process.poll() is None:
        try:
            pid = process.pid
            process.kill()
            process.wait(timeout=5)
            killed_pids.append(pid)
            log_messages.append(f"Terminated tracked PID {pid}")
        except Exception as exc:
            return False, f"Force kill error: {exc}"
    else:
        log_messages.append("Tracked process already stopped")

    app_processes[app_id] = None
    app_status[app_id] = "stopped"

    if port:
        rogue_pids = kill_processes_by_port(port, exclude_pids=killed_pids)
        if rogue_pids:
            killed_pids.extend(rogue_pids)
            joined = ", ".join(str(pid) for pid in rogue_pids)
            log_messages.append(f"Cleared rogue PID(s) {joined} on port {port}")

    if app_id in proxy_processes:
        stop_reverse_proxy(app_id)

    app_outputs.setdefault(app_id, [])
    timestamp = datetime.now().strftime('%H:%M:%S')
    for message in log_messages:
        app_outputs[app_id].append(f"[{timestamp}] [KILL] {message}")
    if len(app_outputs[app_id]) > 100:
        app_outputs[app_id] = app_outputs[app_id][-100:]

    if killed_pids:
        summary = ", ".join(str(pid) for pid in killed_pids)
        return True, f"Force killed PID(s): {summary}"
    return False, "No matching process found on tracked PID or port"

def get_app_url(app_id):
    """Get the URL for a Dash app"""
    app_config = next((a for a in DASH_APPS if a["id"] == app_id), None)
    if app_config:
        return f"http://localhost:{app_config['port']}"
    return None

# Layout
def create_screw():
    """Create a decorative screw element"""
    return html.Div(className="screw")

def create_tool_card(tool):
    """Create a military-style gauge panel for a Python tool"""
    return html.Div([
        # Corner screws
        html.Div(className="screw screw-tl"),
        html.Div(className="screw screw-tr"),
        html.Div(className="screw screw-bl"),
        html.Div(className="screw screw-br"),
        
        # Main gauge panel content
        html.Div([
            # Label plate
            html.Div([
                html.Span(tool["name"], className="label-plate")
            ], className="mb-3"),
            
            # Control row
            dbc.Row([
                # Toggle switch section
                dbc.Col([
                    html.Div([
                        html.Div([
                            html.Div(className="control-knob me-3", style={"verticalAlign": "middle"}),
                            html.Div([
                                dbc.Switch(
                                    id={"type": "tool-checkbox", "index": tool["id"]},
                                    value=False,
                                    className="mb-0",
                                    style={"transform": "scale(1.8)"}
                                ),
                                html.Div("POWER", className="status-text mt-1")
                            ], className="toggle-base text-center")
                        ], className="d-flex align-items-center")
                    ])
                ], width=4),
                
                # Status indicator section
                dbc.Col([
                    html.Div([
                        html.Div([
                            html.Span(
                                "●",
                                id={"type": "tool-indicator", "index": tool["id"]},
                                style={"color": "#333", "fontSize": "28px"}
                            )
                        ], className="indicator-housing"),
                        html.Div("STATUS", className="status-text mt-1")
                    ], className="text-center")
                ], width=4),
                
                # Info section
                dbc.Col([
                    html.Div([
                        html.Small(tool["description"], style={"color": "#888", "fontFamily": "'Courier New', monospace", "fontSize": "11px"})
                    ])
                ], width=4)
            ], className="align-items-center"),
            
            # Warning stripe
            html.Div(className="warning-stripe mt-3"),

            dbc.Row([
                dbc.Col([
                    dbc.Button(
                        "⚠ FORCE KILL",
                        id={"type": "tool-kill", "index": tool["id"]},
                        color="danger",
                        size="sm",
                        className="kill-button w-100"
                    )
                ], width=12)
            ], className="mt-2"),
            
            # Output terminal
            dbc.Collapse([
                html.Div([
                    html.Div([
                        html.Span("◀ ", style={"color": "#d4a017"}),
                        html.Span("TERMINAL OUTPUT", className="status-text"),
                        html.Span(" ▶", style={"color": "#d4a017"})
                    ], className="text-center mb-2"),
                    html.Div(
                        id={"type": "tool-output", "index": tool["id"]},
                        className="terminal-output",
                        style={
                            "padding": "12px",
                            "borderRadius": "4px",
                            "fontSize": "11px",
                            "maxHeight": "150px",
                            "overflowY": "auto",
                            "whiteSpace": "pre-wrap"
                        }
                    )
                ], className="mt-3")
            ], id={"type": "tool-collapse", "index": tool["id"]}, is_open=False)
        ], className="p-3")
    ], className="gauge-panel military-panel mb-4", style={"position": "relative", "padding": "30px"})

def create_dash_app_card(app_config):
    """Create a military-style gauge panel for a Dash application"""
    proxy_cfg = app_config.get("reverse_proxy") or {}
    proxy_ready = proxy_cfg.get("configured", False)
    proxy_env_hint = proxy_cfg.get("env_prefix", app_config["id"].upper())
    initial_proxy_message = (
        "Tunnel offline"
        if proxy_ready
        else f"Set {proxy_env_hint}_HOST/USER/REMOTE_PORT to enable"
    )
    proxy_tooltip = None if proxy_ready else f"Configure {proxy_env_hint}_* variables to enable"
    return html.Div([
        # Corner screws
        html.Div(className="screw screw-tl"),
        html.Div(className="screw screw-tr"),
        html.Div(className="screw screw-bl"),
        html.Div(className="screw screw-br"),
        
        # Main gauge panel content
        html.Div([
            # Label plate with port number
            html.Div([
                html.Span(app_config["name"], className="label-plate me-2"),
                html.Span(f"PORT {app_config['port']}", 
                         style={"color": "#00ff00", "fontFamily": "'Courier New', monospace", 
                                "fontSize": "12px", "backgroundColor": "#111", 
                                "padding": "4px 8px", "borderRadius": "3px",
                                "border": "1px solid #00ff00"})
            ], className="mb-3 d-flex align-items-center"),
            
            # Control row
            dbc.Row([
                # Toggle switch section
                dbc.Col([
                    html.Div([
                        html.Div([
                            html.Div(className="control-knob me-3", style={"verticalAlign": "middle"}),
                            html.Div([
                                dbc.Switch(
                                    id={"type": "dash-checkbox", "index": app_config["id"]},
                                    value=False,
                                    className="mb-0",
                                    style={"transform": "scale(1.8)"}
                                ),
                                html.Div("IGNITION", className="status-text mt-1")
                            ], className="toggle-base text-center")
                        ], className="d-flex align-items-center")
                    ])
                ], width=3),
                
                # Status indicator section
                dbc.Col([
                    html.Div([
                        html.Div([
                            html.Span(
                                "●",
                                id={"type": "dash-indicator", "index": app_config["id"]},
                                style={"color": "#333", "fontSize": "28px"}
                            )
                        ], className="indicator-housing"),
                        html.Div("REACTOR", className="status-text mt-1")
                    ], className="text-center")
                ], width=2),
                
                # Open link as big button
                dbc.Col([
                    html.A(
                        html.Div([
                            html.Div("▶ LAUNCH", style={"fontWeight": "bold", "fontSize": "14px"}),
                            html.Div("INTERFACE", className="status-text")
                        ], className="text-center"),
                        id={"type": "dash-open", "index": app_config["id"]},
                        href=f"http://localhost:{app_config['port']}",
                        target="_blank",
                        style={
                            "display": "block",
                            "background": "linear-gradient(180deg, #4a4a4a, #2a2a2a)",
                            "border": "3px solid #555",
                            "borderRadius": "6px",
                            "padding": "10px 20px",
                            "color": "#666",
                            "textDecoration": "none",
                            "pointerEvents": "none",
                            "opacity": "0.5",
                            "cursor": "not-allowed",
                            "boxShadow": "inset 0 2px 4px rgba(0,0,0,0.3)"
                        }
                    )
                ], width=3),
                
                # Description
                dbc.Col([
                    html.Div([
                        html.Small(app_config["description"], 
                                  style={"color": "#888", "fontFamily": "'Courier New', monospace", "fontSize": "11px"})
                    ])
                ], width=4)
            ], className="align-items-center"),

            # Reverse proxy controls + kill radio
            dbc.Row([
                dbc.Col([
                    html.Div([
                        html.Div([
                            dbc.Switch(
                                id={"type": "proxy-checkbox", "index": app_config["id"]},
                                value=False,
                                className="mb-0",
                                style={"transform": "scale(1.5)"},
                                disabled=not proxy_ready,
                            ),
                            html.Div("PROXY", className="status-text mt-1")
                        ], className="toggle-base text-center", title=proxy_tooltip)
                    ])
                ], width=3),

                dbc.Col([
                    html.Div([
                        html.Div([
                            html.Span(
                                "●",
                                id={"type": "proxy-indicator", "index": app_config["id"]},
                                style={"color": "#333", "fontSize": "24px"}
                            )
                        ], className="indicator-housing"),
                        html.Div("TUNNEL", className="status-text mt-1")
                    ], className="text-center")
                ], width=2),

                dbc.Col([
                    html.Div(
                        id={"type": "proxy-status", "index": app_config["id"]},
                        className="status-text",
                        style={"minHeight": "28px"},
                        children=initial_proxy_message
                    )
                ], width=5),

                dbc.Col([
                    html.Div([
                        html.Div("KILL SWITCH", className="status-text mb-1"),
                        dbc.RadioItems(
                            id={"type": "dash-kill", "index": app_config["id"]},
                            options=[
                                {"label": "ARM", "value": "safe"},
                                {"label": "PURGE", "value": "purge"}
                            ],
                            value="safe",
                            className="kill-radio",
                            inline=False,
                        )
                    ], className="text-center")
                ], width=2)
            ], className="align-items-center mt-3"),
            
            # Rivets decoration
            html.Div([
                html.Span(className="rivet"),
                html.Span(className="rivet"),
                html.Span(className="rivet"),
                html.Span("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", style={"color": "#444", "fontSize": "8px", "letterSpacing": "-2px"}),
                html.Span(className="rivet"),
                html.Span(className="rivet"),
                html.Span(className="rivet")
            ], className="text-center mt-3"),
            
            # Output terminal
            dbc.Collapse([
                html.Div([
                    html.Div([
                        html.Span("◀ ", style={"color": "#d4a017"}),
                        html.Span("SYSTEM LOG", className="status-text"),
                        html.Span(" ▶", style={"color": "#d4a017"})
                    ], className="text-center mb-2"),
                    html.Div(
                        id={"type": "dash-output", "index": app_config["id"]},
                        className="terminal-output",
                        style={
                            "padding": "12px",
                            "borderRadius": "4px",
                            "fontSize": "11px",
                            "maxHeight": "150px",
                            "overflowY": "auto",
                            "whiteSpace": "pre-wrap"
                        }
                    )
                ], className="mt-3")
            ], id={"type": "dash-collapse", "index": app_config["id"]}, is_open=False)
        ], className="p-3")
    ], className="gauge-panel military-panel mb-4", style={"position": "relative", "padding": "30px"})


def _render_empty_panel(message):
    """Display a themed alert when a persona has no panels for a section."""
    return dbc.Alert(
        message,
        color="secondary",
        className="persona-empty-alert",
        style={"fontFamily": "'Courier New', monospace", "fontSize": "12px"},
    )


def build_tool_cards(tool_ids):
    """Render only the tool cards assigned to the active persona."""
    cards = []
    for tool_id in tool_ids:
        tool_cfg = TOOL_LOOKUP.get(tool_id)
        if tool_cfg:
            cards.append(create_tool_card(tool_cfg))
    if cards:
        return cards
    return [_render_empty_panel("No utility systems assigned to this persona.")]


def build_dash_cards(app_ids):
    """Render only the Dash cards assigned to the active persona."""
    cards = []
    for app_id in app_ids:
        app_cfg = DASH_LOOKUP.get(app_id)
        if app_cfg:
            cards.append(create_dash_app_card(app_cfg))
    if cards:
        return cards
    return [_render_empty_panel("No reactors available for this persona.")]


def build_llm_panel_cards(tool_ids, app_ids):
    """Render combined utility/reactor cards for LLM workflow."""
    llm_tools = [tool_id for tool_id in tool_ids if tool_id in LLM_TOOL_IDS]
    llm_apps = [app_id for app_id in app_ids if app_id in LLM_DASH_IDS]

    children = []
    if llm_tools:
        children.append(html.H6("LLM Utilities", className="status-text mb-3"))
        children.extend(build_tool_cards(llm_tools))
    if llm_apps:
        children.append(html.H6("LLM Reactors", className="status-text mb-3 mt-4"))
        children.extend(build_dash_cards(llm_apps))
    if children:
        return children
    return [_render_empty_panel("No LLM tools/reactors assigned to this persona.")]


def build_finance_panel_cards(tool_ids, app_ids):
    """Render combined utility/reactor cards for finance workflow."""
    finance_tools = [tool_id for tool_id in tool_ids if tool_id in FINANCE_TOOL_IDS]
    finance_apps = [app_id for app_id in app_ids if app_id in FINANCE_DASH_IDS]

    children = []
    if finance_tools:
        children.append(html.H6("Financial Utilities", className="status-text mb-3"))
        children.extend(build_tool_cards(finance_tools))
    if finance_apps:
        children.append(html.H6("Financial Reactors", className="status-text mb-3 mt-4"))
        children.extend(build_dash_cards(finance_apps))
    if children:
        return children
    return [_render_empty_panel("No financial tools/reactors assigned to this persona.")]


def build_manager_panel_cards(tool_ids, app_ids):
    """Render management-focused cards driven by panel_groups.manager in apps_config.yaml."""
    manager_tools = [tool_id for tool_id in tool_ids if tool_id in MANAGER_TOOL_IDS]
    manager_apps = [app_id for app_id in app_ids if app_id in MANAGER_DASH_IDS]

    children = [
        dbc.Alert(
            [
                html.Strong("📋 MANAGEMENT OVERVIEW: ", style={"color": "#60a5fa"}),
                html.Span(
                    "Launch and monitor operational dashboards from this panel.",
                    style={"color": "#cbd5e1"},
                ),
                html.Br(),
                html.Span(
                    "Recommended: Finance Tracker for reporting → Invoice Tool for workflow management.",
                    style={"color": "#94a3b8", "fontSize": "11px"},
                ),
            ],
            color="dark",
            className="mb-4",
            style={
                "backgroundColor": "#0f172a",
                "border": "2px solid #2563eb",
                "borderRadius": "4px",
                "fontFamily": "'Segoe UI', Arial, sans-serif",
            },
        ),
    ]
    if manager_tools:
        children.append(html.H6("Management Utilities", className="status-text mb-3"))
        children.extend(build_tool_cards(manager_tools))
    if manager_apps:
        children.append(html.H6("Operational Dashboards", className="status-text mb-3 mt-4"))
        children.extend(build_dash_cards(manager_apps))
    if len(children) == 1:  # only the alert, no real content
        children.append(_render_empty_panel("No management tools assigned to this persona."))
    return children


initial_persona = get_persona(DEFAULT_PERSONA_ID)
initial_llm_children = build_llm_panel_cards(
    initial_persona["allowed_tools"], initial_persona["allowed_dash_apps"]
)
initial_finance_children = build_finance_panel_cards(
    initial_persona["allowed_tools"], initial_persona["allowed_dash_apps"]
)
initial_manager_children = build_manager_panel_cards(
    initial_persona["allowed_tools"], initial_persona["allowed_dash_apps"]
)
initial_active_tab = "llm-panel"
persona_switch_style = {} if ALLOW_PERSONA_SWITCH else {"display": "none"}
initial_persona_chip = [
    html.Span("PERSONA", className="status-text me-2"),
    html.Span(initial_persona["name"], className="persona-chip__name"),
    html.Span(initial_persona["description"], className="persona-chip__desc ms-2"),
]


app.layout = html.Div([
    dcc.Store(id="active-persona", data=initial_persona["id"]),
    dcc.Store(id="phoenix-project-name", data=DEFAULT_PHOENIX_PROJECT_NAME),
    dbc.Container([
        # Main console panel
        html.Div([
            # Corner screws for main panel
            html.Div(className="screw screw-tl"),
            html.Div(className="screw screw-tr"),
            html.Div(className="screw screw-bl"),
            html.Div(className="screw screw-br"),

            # Console header
            html.Div([
                html.Div([
                    html.Span(className="rivet"),
                    html.Span(className="rivet"),
                    html.Span(className="rivet"),
                ], className="mb-2"),
                html.H1("⚙️ CONTROL STATION", className="console-title mb-1"),
                html.Div(
                    "SYSTEM MANAGEMENT INTERFACE v2.0",
                    style={
                        "color": "#888",
                        "fontFamily": "'Courier New', monospace",
                        "fontSize": "12px",
                        "letterSpacing": "2px",
                    },
                ),
                html.Div([
                    html.Span(className="rivet"),
                    html.Span(className="rivet"),
                    html.Span(className="rivet"),
                ], className="mt-2"),
            ], className="console-header"),

            # Persona control row
            html.Div([
                html.Div([
                    html.Div(
                        initial_persona_chip,
                        id="persona-chip",
                        className="persona-chip",
                    )
                ], className="flex-grow-1"),
                html.Div([
                    dbc.Label("Persona Switch", className="status-text mb-1"),
                    dbc.Select(
                        id="persona-select",
                        options=PERSONA_OPTIONS,
                        value=initial_persona["id"],
                        disabled=not ALLOW_PERSONA_SWITCH,
                        className="persona-select-control",
                    ),
                ], className="persona-selector", style=persona_switch_style),
            ], className="persona-row d-flex flex-column flex-md-row align-items-md-center gap-3"),

            # Warning stripe
            html.Div(className="warning-stripe mb-4"),

            # Tabs styled as panel sections
            dbc.Tabs([
                dbc.Tab(
                    [
                        html.Div([
                            html.Span("◈", style={"color": "#d4a017", "fontSize": "20px"}),
                            html.Span(" LLM OPERATIONS ", className="label-plate mx-2"),
                            html.Span("◈", style={"color": "#d4a017", "fontSize": "20px"}),
                        ], className="text-center mb-4 mt-3"),

                        dbc.Alert(
                            [
                                html.Strong("Recommended LLM startup order: ", style={"color": "#d4a017"}),
                                html.Span("1) Phoenix/Arize  →  2) Ollama  →  3) Ollama Chat UI", style={"color": "#ccc"}),
                                html.Br(),
                                html.Span("Breadcrumb: Observability first, model second, interface third.", style={"color": "#888", "fontSize": "11px"}),
                            ],
                            color="dark",
                            className="mb-4",
                            style={
                                "backgroundColor": "#1a1a1a",
                                "border": "2px solid #d4a017",
                                "borderRadius": "4px",
                                "fontFamily": "'Courier New', monospace",
                            },
                        ),

                        dbc.Row(
                            [
                                dbc.Col(
                                    [
                                        dbc.Label("Phoenix Project", className="status-text mb-1"),
                                        dbc.Input(
                                            id="phoenix-project-input",
                                            type="text",
                                            value=DEFAULT_PHOENIX_PROJECT_NAME,
                                            debounce=True,
                                            placeholder="default",
                                        ),
                                        html.Small(
                                            "Traces from Ollama Chat will be tagged to this project.",
                                            style={"color": "#888", "fontFamily": "'Courier New', monospace"},
                                        ),
                                    ],
                                    width=12,
                                )
                            ],
                            className="mb-3",
                        ),

                        html.Div(initial_llm_children, id="llm-card-container"),
                    ],
                    label="🧠 LLM PANEL",
                    tab_id="llm-panel",
                    id="llm-tab",
                    label_style={
                        "fontFamily": "'Courier New', monospace",
                        "fontWeight": "bold",
                    },
                ),

                dbc.Tab(
                    [
                        html.Div([
                            html.Span("◈", style={"color": "#d4a017", "fontSize": "20px"}),
                            html.Span(
                                " FINANCIAL OPERATIONS ",
                                className="label-plate mx-2",
                            ),
                            html.Span("◈", style={"color": "#d4a017", "fontSize": "20px"}),
                        ], className="text-center mb-3 mt-3"),

                        # Technical note alert
                        dbc.Alert(
                            [
                                html.Div(
                                    [
                                        html.Strong(
                                            "⚠ OPERATOR NOTICE: ",
                                            style={"color": "#d4a017"},
                                        ),
                                        html.Span(
                                            "Applications run in isolated subprocess mode. ",
                                            style={"color": "#ccc"},
                                        ),
                                        html.Span(
                                            "Flask reloader disabled (WERKZEUG_RUN_MAIN=true). ",
                                            style={"color": "#888", "fontSize": "11px"},
                                        ),
                                        html.Span(
                                            "Hot-reload unavailable when launched from Control Station.",
                                            style={"color": "#888", "fontSize": "11px"},
                                        ),
                                    ],
                                    style={
                                        "fontFamily": "'Courier New', monospace",
                                        "fontSize": "12px",
                                    },
                                )
                            ],
                            color="dark",
                            className="mb-4",
                            style={
                                "backgroundColor": "#1a1a1a",
                                "border": "2px solid #d4a017",
                                "borderRadius": "4px",
                            },
                        ),

                        html.Div(initial_finance_children, id="finance-card-container"),
                    ],
                    label="💰 FINANCE PANEL",
                    tab_id="finance-panel",
                    id="finance-tab",
                    label_style={
                        "fontFamily": "'Courier New', monospace",
                        "fontWeight": "bold",
                    },
                ),

                dbc.Tab(
                    [
                        html.Div([
                            html.Span("◈", style={"color": "#3b82f6", "fontSize": "20px"}),
                            html.Span(
                                " MANAGER TOOLS ",
                                className="label-plate mx-2",
                            ),
                            html.Span("◈", style={"color": "#3b82f6", "fontSize": "20px"}),
                        ], className="text-center mb-3 mt-3"),

                        html.Div(initial_manager_children, id="manager-card-container"),
                    ],
                    label="📋 MANAGER TOOLS",
                    tab_id="manager-panel",
                    id="manager-tab",
                    label_style={
                        "fontFamily": "'Segoe UI', Arial, sans-serif",
                        "fontWeight": "bold",
                    },
                ),
            ], id="tabs", active_tab=initial_active_tab, className="mb-4"),

            # Footer with status
            html.Div([
                html.Div(className="warning-stripe mb-3"),
                html.Div([
                    html.Span("STATION OPERATIONAL", className="status-text me-3"),
                    html.Span("●", style={"color": "#00ff00", "fontSize": "12px"}),
                    html.Span(" │ ", style={"color": "#444"}),
                    html.Span("PORT 8060", className="status-text"),
                ], className="text-center"),
                html.Div(
                    [
                        dbc.Button(
                            "Pause Live Polling",
                            id="toggle-polling",
                            color="secondary",
                            size="sm",
                            className="mt-3",
                        ),
                        html.Div(
                            "Live polling: ON",
                            id="polling-status-label",
                            className="status-text mt-2",
                            style={"fontSize": "11px", "color": "#aaa"},
                        ),
                    ],
                    className="text-center",
                ),
            ], className="mt-4"),
        ], className="military-panel p-4", style={"position": "relative", "marginTop": "20px"}),

        dcc.Interval(
            id="status-update",
            interval=STATUS_UPDATE_INTERVAL_MS,
            n_intervals=0,
            disabled=False,
        ),
    ], fluid=True, className="p-4"),
], id="persona-root", className=f"persona-wrapper {initial_persona['theme_class']}")

# Callbacks


@callback(
    Output("phoenix-project-name", "data"),
    Input("phoenix-project-input", "value"),
    prevent_initial_call=False,
)
def update_phoenix_project_name(project_name):
    """Persist the Phoenix project name selected in the control panel UI."""
    return sanitize_project_name(project_name)


@callback(
    Output("status-update", "disabled"),
    Output("toggle-polling", "children"),
    Output("polling-status-label", "children"),
    Input("toggle-polling", "n_clicks"),
    prevent_initial_call=False,
)
def toggle_live_polling(n_clicks):
    """Pause/resume interval-driven status refreshes for a smoother UI."""
    paused = bool((n_clicks or 0) % 2)
    if paused:
        return True, "Resume Live Polling", "Live polling: PAUSED"
    return False, "Pause Live Polling", "Live polling: ON"


@callback(
    Output("active-persona", "data"),
    Output("persona-root", "className"),
    Output("llm-card-container", "children"),
    Output("finance-card-container", "children"),
    Output("manager-card-container", "children"),
    Output("persona-chip", "children"),
    Output("tabs", "active_tab"),
    Input("persona-select", "value"),
    prevent_initial_call=False,
)
def update_persona_view(selected_persona):
    """Swap themes and visible panels when the persona selector changes."""
    persona_key = (selected_persona or DEFAULT_PERSONA_ID).lower()
    persona = get_persona(persona_key)

    llm_children = build_llm_panel_cards(
        persona["allowed_tools"], persona["allowed_dash_apps"]
    )
    finance_children = build_finance_panel_cards(
        persona["allowed_tools"], persona["allowed_dash_apps"]
    )
    manager_children = build_manager_panel_cards(
        persona["allowed_tools"], persona["allowed_dash_apps"]
    )
    persona_chip = [
        html.Span("PERSONA", className="status-text me-2"),
        html.Span(persona["name"], className="persona-chip__name"),
        html.Span(persona["description"], className="persona-chip__desc ms-2"),
    ]
    root_class = f"persona-wrapper {persona['theme_class']}"
    active_tab = "manager-panel" if persona_key == "management" else "llm-panel"

    return (
        persona["id"],
        root_class,
        llm_children,
        finance_children,
        manager_children,
        persona_chip,
        active_tab,
    )


@callback(
    Output({"type": "tool-collapse", "index": MATCH}, "is_open"),
    Output({"type": "tool-indicator", "index": MATCH}, "style"),
    Output({"type": "tool-output", "index": MATCH}, "children"),
    Input({"type": "tool-checkbox", "index": MATCH}, "value"),
    Input({"type": "tool-kill", "index": MATCH}, "n_clicks"),
    Input("status-update", "n_intervals"),
    State({"type": "tool-checkbox", "index": MATCH}, "id"),
    State("phoenix-project-name", "data"),
    prevent_initial_call=False
)
def handle_python_tool(checked, kill_clicks, n, tool_id_dict, project_name):
    """Handle Python tool checkbox and status updates"""
    tool_id = tool_id_dict["index"]
    triggered_id = ctx.triggered_id
    
    # Handle checkbox toggle
    if triggered_id and isinstance(triggered_id, dict) and triggered_id.get("type") == "tool-checkbox":
        if checked:
            success, message = start_python_tool(
                tool_id,
                extra_env=build_observability_env(project_name),
            )
            if not success:
                app_outputs[tool_id].append(f"[ERROR] {message}")
        else:
            stop_app(tool_id)
    elif triggered_id and isinstance(triggered_id, dict) and triggered_id.get("type") == "tool-kill":
        success, message = force_kill_app(tool_id)
        if not success:
            app_outputs.setdefault(tool_id, [])
            app_outputs[tool_id].append(f"[{datetime.now().strftime('%H:%M:%S')}] [KILL][WARN] {message}")
            if len(app_outputs[tool_id]) > 100:
                app_outputs[tool_id] = app_outputs[tool_id][-100:]
    
    # Update status
    is_running = app_status.get(tool_id) == "running"
    current_output_signature = output_signature(tool_id)
    if triggered_id == "status-update":
        previous_state = ui_render_state.get(tool_id, {})
        if (
            previous_state.get("running") == is_running
            and previous_state.get("output_signature") == current_output_signature
        ):
            return no_update, no_update, no_update

    indicator_style = {
        "color": "#00ff00" if is_running else "#333",
        "fontSize": "28px",
        "textShadow": "0 0 10px rgba(0,255,0,0.8)" if is_running else "none"
    }
    
    output_text = sanitize_output_text("\n".join(app_outputs.get(tool_id, ["No output yet"])))
    ui_render_state[tool_id] = {
        "running": is_running,
        "output_signature": current_output_signature,
        "proxy_state": None,
        "proxy_message": None,
    }
    
    return checked and True, indicator_style, output_text

@callback(
    Output({"type": "dash-collapse", "index": MATCH}, "is_open"),
    Output({"type": "dash-indicator", "index": MATCH}, "style"),
    Output({"type": "dash-open", "index": MATCH}, "style"),
    Output({"type": "dash-output", "index": MATCH}, "children"),
    Output({"type": "proxy-indicator", "index": MATCH}, "style"),
    Output({"type": "proxy-status", "index": MATCH}, "children"),
    Output({"type": "dash-kill", "index": MATCH}, "value"),
    Input({"type": "dash-checkbox", "index": MATCH}, "value"),
    Input({"type": "proxy-checkbox", "index": MATCH}, "value"),
    Input({"type": "dash-kill", "index": MATCH}, "value"),
    Input("status-update", "n_intervals"),
    State({"type": "dash-checkbox", "index": MATCH}, "id"),
    State("phoenix-project-name", "data"),
    prevent_initial_call=False
)
def handle_dash_app(checked, proxy_checked, kill_value, n, app_id_dict, project_name):
    """Handle Dash app checkbox and status updates"""
    app_id = app_id_dict["index"]
    triggered_id = ctx.triggered_id
    proxy_checked = bool(proxy_checked)
    kill_value_reset = no_update
    app_config = get_dash_app(app_id)
    
    # Handle checkbox toggle
    if triggered_id and isinstance(triggered_id, dict) and triggered_id.get("type") == "dash-checkbox":
        if checked:
            success, message = start_dash_app(
                app_id,
                extra_env=build_observability_env(project_name),
            )
            if not success:
                app_outputs[app_id].append(f"[ERROR] {message}")
        else:
            stop_app(app_id)
    elif triggered_id and isinstance(triggered_id, dict) and triggered_id.get("type") == "proxy-checkbox":
        if proxy_checked:
            success, message = start_reverse_proxy(app_id)
        else:
            success, message = stop_reverse_proxy(app_id)
        if not success:
            app_outputs.setdefault(app_id, [])
            app_outputs[app_id].append(
                f"[{datetime.now().strftime('%H:%M:%S')}] [PROXY][ERROR] {message}"
            )
            if len(app_outputs[app_id]) > 100:
                app_outputs[app_id] = app_outputs[app_id][-100:]
    elif triggered_id and isinstance(triggered_id, dict) and triggered_id.get("type") == "dash-kill":
        if kill_value == "purge":
            success, message = force_kill_app(app_id)
            if not success:
                app_outputs.setdefault(app_id, [])
                app_outputs[app_id].append(
                    f"[{datetime.now().strftime('%H:%M:%S')}] [KILL][WARN] {message}"
                )
                if len(app_outputs[app_id]) > 100:
                    app_outputs[app_id] = app_outputs[app_id][-100:]
            kill_value_reset = "safe"
    
    # Update status
    is_running = app_status.get(app_id) == "running"
    current_output_signature = output_signature(app_id)
    indicator_style = {
        "color": "#00ff00" if is_running else "#333",
        "fontSize": "28px",
        "textShadow": "0 0 10px rgba(0,255,0,0.8)" if is_running else "none"
    }
    
    # Enable/disable link - military launch button style
    if is_running:
        link_style = {
            "display": "block",
            "background": "linear-gradient(180deg, #2a5a2a, #1a3a1a)",
            "border": "3px solid #00ff00",
            "borderRadius": "6px",
            "padding": "10px 20px",
            "color": "#00ff00",
            "textDecoration": "none",
            "pointerEvents": "auto",
            "opacity": "1",
            "cursor": "pointer",
            "boxShadow": "0 0 15px rgba(0,255,0,0.4), inset 0 2px 4px rgba(0,0,0,0.3)",
            "textShadow": "0 0 5px rgba(0,255,0,0.5)"
        }
    else:
        link_style = {
            "display": "block",
            "background": "linear-gradient(180deg, #4a4a4a, #2a2a2a)",
            "border": "3px solid #555",
            "borderRadius": "6px",
            "padding": "10px 20px",
            "color": "#666",
            "textDecoration": "none",
            "pointerEvents": "none",
            "opacity": "0.5",
            "cursor": "not-allowed",
            "boxShadow": "inset 0 2px 4px rgba(0,0,0,0.3)"
        }
    
    output_text = sanitize_output_text("\n".join(app_outputs.get(app_id, ["No output yet"])))

    # Proxy indicators
    update_proxy_health(app_id)
    proxy_state = proxy_health.get(app_id, {"state": "inactive", "message": "Tunnel offline"})
    proxy_indicator_style = {
        "color": "#333",
        "fontSize": "24px",
        "textShadow": "none",
    }

    state = proxy_state.get("state")
    if state == "healthy":
        proxy_indicator_style.update({
            "color": "#00e5ff",
            "textShadow": "0 0 12px rgba(0,229,255,0.7)",
        })
    elif state == "degraded":
        proxy_indicator_style.update({
            "color": "#ffbe0b",
            "textShadow": "0 0 10px rgba(255,190,11,0.6)",
        })
    elif state == "error":
        proxy_indicator_style.update({
            "color": "#ff3b30",
            "textShadow": "0 0 10px rgba(255,59,48,0.6)",
        })
    elif state == "starting":
        proxy_indicator_style.update({
            "color": "#00fff2",
            "textShadow": "0 0 10px rgba(0,255,242,0.5)",
        })
    elif state == "active":
        proxy_indicator_style.update({
            "color": "#1dd3b0",
            "textShadow": "0 0 10px rgba(29,211,176,0.5)",
        })
    elif state == "disabled":
        proxy_indicator_style["color"] = "#555"
    else:
        proxy_indicator_style["color"] = "#333"

    endpoint = None
    if app_config:
        proxy_cfg = app_config.get("reverse_proxy") or {}
        if proxy_cfg.get("configured"):
            endpoint = f"{proxy_cfg.get('host')}:{proxy_cfg.get('remote_port')}"
    proxy_status_text = proxy_state.get("message", "")
    if endpoint:
        proxy_status_text = f"{endpoint} • {proxy_status_text}"

    if triggered_id == "status-update":
        previous_state = ui_render_state.get(app_id, {})
        if (
            previous_state.get("running") == is_running
            and previous_state.get("output_signature") == current_output_signature
            and previous_state.get("proxy_state") == state
            and previous_state.get("proxy_message") == proxy_status_text
        ):
            return no_update, no_update, no_update, no_update, no_update, no_update, no_update

    ui_render_state[app_id] = {
        "running": is_running,
        "output_signature": current_output_signature,
        "proxy_state": state,
        "proxy_message": proxy_status_text,
    }
    
    return (
        checked and True,
        indicator_style,
        link_style,
        output_text,
        proxy_indicator_style,
        proxy_status_text,
        kill_value_reset,
    )

if __name__ == "__main__":
    print("🎛️  Control Panel starting on http://localhost:8060")
    print("=" * 50)
    app.run(debug=True, port=8060)