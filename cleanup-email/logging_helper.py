"""A drop-in logging helper that produces rotating log files.

Other Python projects can copy this module (together with logging.yaml) into
their own project and start logging immediately:

    from logging_helper import get_logger

    log = get_logger("myapp")
    log.info("hello world")

Configuration is read from logging.yaml (searched in this order):
  1. path from the LOGGING_CONFIG environment variable
  2. directory of this module
  3. current working directory

Supported keys under ``logging``:

    dir          log root directory, ``~`` is expanded     (default ~/.log)
    maxBytes     per-file size, plain bytes or units like 1K/1M/1G (default 1M)
    backupCount  max rotating files kept                    (default 10)
    encoding     log file encoding                          (default utf-8)

Logs are written to ``{dir}/<name>/<name>.log`` and rotate once the file
exceeds ``maxBytes``, keeping at most ``backupCount`` backups.
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parent

_DEFAULT_CONFIG = {
    "dir": "~/.log",
    "maxBytes": "1M",
    "backupCount": 10,
    "encoding": "utf-8",
}

_UNITS = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024 ** 2,
    "mb": 1024 ** 2,
    "g": 1024 ** 3,
    "gb": 1024 ** 3,
}

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_yaml(path):
    """Minimal YAML-subset parser for the flat logging.yaml (no deps)."""
    data = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.split("#", 1)[0].strip().strip("\"'").strip()
            if key:
                data[key] = value
    return data


def _parse_size(value):
    """Accept plain byte counts or human sizes like '1M', '10MB', '2.5G'."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    if s.isdigit():
        return int(s)
    for unit, mult in _UNITS.items():
        if s.endswith(unit):
            num = s[: -len(unit)].strip()
            if num and (num.isdigit() or num.replace(".", "", 1).isdigit()):
                return int(float(num) * mult)
    raise ValueError("invalid maxBytes value: %r" % (value,))


def _load_config():
    candidates = []
    env_path = os.environ.get("LOGGING_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(_MODULE_DIR / "logging.yaml")
    candidates.append(Path.cwd() / "logging.yaml")

    for path in candidates:
        if path.is_file():
            cfg = _DEFAULT_CONFIG.copy()
            try:
                parsed = _parse_yaml(path)
            except OSError:
                continue
            if "dir" in parsed:
                cfg["dir"] = parsed["dir"]
            if "maxBytes" in parsed:
                cfg["maxBytes"] = parsed["maxBytes"]
            if "backupCount" in parsed:
                try:
                    cfg["backupCount"] = int(parsed["backupCount"])
                except ValueError:
                    pass
            if "encoding" in parsed:
                cfg["encoding"] = parsed["encoding"]
            return cfg

    return dict(_DEFAULT_CONFIG)


def _build_handler(name, cfg):
    log_dir = Path(cfg["dir"]).expanduser().resolve()
    log_file = log_dir / name / (name + ".log")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    return logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=_parse_size(cfg["maxBytes"]),
        backupCount=int(cfg["backupCount"]),
        encoding=cfg["encoding"],
    ), log_file


def get_logger(name):
    """Return a logging.Logger writing rotating logs under {dir}/{name}."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("logger name must be a non-empty string")

    logger = logging.getLogger(name)

    if not logger.handlers:
        cfg = _load_config()
        handler, log_file = _build_handler(name, cfg)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    return logger


__all__ = ["get_logger"]