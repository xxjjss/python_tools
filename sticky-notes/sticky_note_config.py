"""User configuration for the sticky-note widget.

Reads `config.yaml` living next to this module (source dir in dev, the install
dir `~/.local/share/sticky-note/` once installed). On first use the file is
created from `config.yaml.template` in the same directory, so defaults and
their explaining comments are preserved for the user to edit.

Config keys (see the template for the authoritative comments):
- user:              GUS email. Empty by default; filled after first launch.
- journal_path:      directory holding the daily journal files. Default ~/Journal.
- refresh_interval_minutes: GUS poll cadence in minutes. Default 15.
- agent_poll_interval_minutes: background-agent poll cadence in minutes. Default 5.
- auto_invoke_wi_worker: auto-launch tcm-wi-worker for WIs. Default False.
- socket_port:       loopback TCP port for the external control socket
                     (immediate-refresh signals today, and any future
                     control command). Required; seeded from the template
                     (48327). Missing key -> ConfigError at startup.
- agents:            agents the widget may launch. `default` holds shared
                     settings; each named entry inherits any key it omits.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent
CONFIG_PATH = CONFIG_DIR / "config.yaml"
TEMPLATE_PATH = CONFIG_DIR / "config.yaml.template"

DEFAULT_JOURNAL_PATH = "~/Journal"
DEFAULT_REFRESH_INTERVAL_MINUTES = 15
DEFAULT_AGENT_POLL_INTERVAL_MINUTES = 5
DEFAULT_AUTO_INVOKE_WI_WORKER = False

# Fallback agent settings used when config.yaml has no `agents.default` block.
# Keys mirror the `claude` CLI flags (see config.yaml.template).
DEFAULT_AGENT_SETTINGS = {
    "model": "opus",
    "effort": "medium",
    "permission-mode": "dontAsk",
    "output-format": "json",
    "max-budget-usd": 5,
    "timeout-minutes": 15,
}


class ConfigError(Exception):
    """Raised on unrecoverable config problems (e.g. user/journal mismatch)."""


def _coerce_bool(value, key: str) -> bool:
    """Coerce a config value to bool. Accepts real bools and the usual YAML/
    string spellings (true/false/yes/no/1/0). Raises ConfigError otherwise."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int,)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "on", "1"):
            return True
        if v in ("false", "no", "off", "0"):
            return False
    raise ConfigError(f"{key} must be a boolean, got {value!r}")


@dataclass
class StickyNoteConfig:
    user: str = ""
    journal_path: str = DEFAULT_JOURNAL_PATH
    refresh_interval_minutes: int = DEFAULT_REFRESH_INTERVAL_MINUTES
    agent_poll_interval_minutes: int = DEFAULT_AGENT_POLL_INTERVAL_MINUTES
    auto_invoke_wi_worker: bool = DEFAULT_AUTO_INVOKE_WI_WORKER
    # No in-code default: the value comes from config.yaml (seeded from the
    # template). load_config() raises if the key is missing so a stale config
    # fails loudly rather than silently picking a port the user can't see.
    socket_port: int = 0
    # Raw `agents` mapping as parsed from config.yaml: {name: {settings}}.
    # `default` (if present) holds settings shared by every agent. Preserved
    # verbatim so save_config() never drops it.
    agents: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.agents is None:
            self.agents = {}

    @property
    def refresh_interval_seconds(self) -> float:
        return float(self.refresh_interval_minutes) * 60.0

    @property
    def agent_poll_interval_seconds(self) -> float:
        return float(self.agent_poll_interval_minutes) * 60.0

    def journal_dir(self) -> Path:
        """Absolute journal directory with ~ expanded."""
        return Path(os.path.expanduser(self.journal_path))

    def agent_config(self, name: str) -> dict:
        """Resolve the effective settings for `name`: agents.default merged
        with the named entry (named entry wins on conflict).

        Raises ConfigError with the exact message "agent <name> was not defined
        in config" when `name` is not listed under `agents`.
        """
        if name not in self.agents:
            raise ConfigError(f"agent {name} was not defined in config")
        merged = dict(DEFAULT_AGENT_SETTINGS)
        base = self.agents.get("default") or {}
        if isinstance(base, dict):
            merged.update(base)
        entry = self.agents.get(name) or {}
        if isinstance(entry, dict):
            merged.update(entry)
        return merged


def ensure_config() -> Path:
    """Create config.yaml from the template on first use. Returns its path.

    If neither the config nor the template exists (unexpected), a minimal
    config file is written so the widget can still start with defaults.
    """
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    if TEMPLATE_PATH.exists():
        shutil.copyfile(TEMPLATE_PATH, CONFIG_PATH)
    else:
        save_config(StickyNoteConfig())
    return CONFIG_PATH


def load_config() -> StickyNoteConfig:
    """Load config.yaml, creating it from the template first if absent.

    Unknown keys are ignored; missing keys fall back to defaults. Raises
    ConfigError only if the file exists but cannot be parsed as a YAML mapping,
    so a corrupt config never silently reverts the user to defaults.
    """
    ensure_config()
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml is not valid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config.yaml must be a mapping, got {type(raw).__name__}")

    cfg = StickyNoteConfig()
    cfg.user = str(raw.get("user") or "").strip()
    jp = raw.get("journal_path")
    cfg.journal_path = str(jp).strip() if jp else DEFAULT_JOURNAL_PATH

    interval = raw.get("refresh_interval_minutes", DEFAULT_REFRESH_INTERVAL_MINUTES)
    try:
        cfg.refresh_interval_minutes = int(interval)
    except (TypeError, ValueError):
        raise ConfigError(
            f"refresh_interval_minutes must be an integer, got {interval!r}"
        )
    if cfg.refresh_interval_minutes <= 0:
        raise ConfigError(
            f"refresh_interval_minutes must be positive, got {cfg.refresh_interval_minutes}"
        )

    poll = raw.get("agent_poll_interval_minutes", DEFAULT_AGENT_POLL_INTERVAL_MINUTES)
    try:
        cfg.agent_poll_interval_minutes = int(poll)
    except (TypeError, ValueError):
        raise ConfigError(
            f"agent_poll_interval_minutes must be an integer, got {poll!r}"
        )
    if cfg.agent_poll_interval_minutes <= 0:
        raise ConfigError(
            f"agent_poll_interval_minutes must be positive, got {cfg.agent_poll_interval_minutes}"
        )

    cfg.auto_invoke_wi_worker = _coerce_bool(
        raw.get("auto_invoke_wi_worker", DEFAULT_AUTO_INVOKE_WI_WORKER),
        "auto_invoke_wi_worker",
    )

    if "socket_port" not in raw:
        raise ConfigError(
            "socket_port is missing from config.yaml. Add it (e.g. "
            "'socket_port: 48327') — see config.yaml.template for the default."
        )
    port = raw.get("socket_port")
    try:
        cfg.socket_port = int(port)
    except (TypeError, ValueError):
        raise ConfigError(f"socket_port must be an integer, got {port!r}")
    if not (1 <= cfg.socket_port <= 65535):
        raise ConfigError(
            f"socket_port must be between 1 and 65535, got {cfg.socket_port}"
        )

    agents = raw.get("agents")
    if agents is None:
        cfg.agents = {}
    elif isinstance(agents, dict):
        cfg.agents = agents
    else:
        raise ConfigError(
            f"agents must be a mapping, got {type(agents).__name__}"
        )
    return cfg


def save_config(cfg: StickyNoteConfig) -> None:
    """Persist config to config.yaml (plain YAML, no template comments)."""
    data = {
        "user": cfg.user,
        "journal_path": cfg.journal_path,
        "refresh_interval_minutes": cfg.refresh_interval_minutes,
        "agent_poll_interval_minutes": cfg.agent_poll_interval_minutes,
        "auto_invoke_wi_worker": cfg.auto_invoke_wi_worker,
        "socket_port": cfg.socket_port,
    }
    # Preserve the agents mapping so set_user()/first-launch writes never drop
    # it. (Comments are lost — the template keeps them; runtime config is data.)
    if cfg.agents:
        data["agents"] = cfg.agents
    CONFIG_PATH.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def set_user(user: str) -> StickyNoteConfig:
    """Write the resolved user email back into config.yaml. Returns updated config."""
    cfg = load_config()
    cfg.user = (user or "").strip()
    save_config(cfg)
    return cfg


def check_user_conflict(config_user: str, journal_user: str) -> None:
    """Raise ConfigError when config.user and journal user_email disagree.

    Only a genuine mismatch is an error: if either side is empty they are
    considered compatible (one will be backfilled from the other elsewhere).
    """
    cu = (config_user or "").strip().lower()
    ju = (journal_user or "").strip().lower()
    if cu and ju and cu != ju:
        raise ConfigError(
            "user mismatch: config.yaml user is "
            f"'{config_user}' but the journal was written for '{journal_user}'. "
            "Resolve this by editing config.yaml or pointing journal_path at the "
            "matching journal directory."
        )
