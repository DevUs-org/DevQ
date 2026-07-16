'''
Tags: Main

ConfigLoader — Four-level configuration system for DevQ.

Keys are split into two scopes:

  DEVICE keys (scheduler, allocator, shots) — resolved independently
  for every attached device through the full cascade
  (later levels win):
      1. DevQ core defaults
      2. That device's provider preferred_config()
      3. Global user config file      (DevQ(config_path=...))
      4. Per-device user config file  (add_device(device, config_path))

  GLOBAL keys (router, router_queue_weight, router_noise_weight) —
  resolved once for the whole system:
      1. DevQ core defaults
      2. Global user config file
  Providers deliberately CANNOT set global keys — a provider
  expressing system routing policy would be a layer violation;
  such keys are warned about and ignored.

Provenance is tracked for every key so qconfig can show where each
active value came from: "DevQ Core", "<ProviderName>",
"User (global)", "User (dN)".
'''

import json
import os


# ── Core defaults ─────────────────────────────────────────────────────────────

DEVICE_DEFAULTS = {
    "scheduler": "packing",
    "allocator": "noise_graph",
    "shots":     1024
}

GLOBAL_DEFAULTS = {
    "router":              "noise",
    "router_queue_weight": 0.5,
    "router_noise_weight": 0.5
}

DEVICE_KEYS = set(DEVICE_DEFAULTS)
GLOBAL_KEYS = set(GLOBAL_DEFAULTS)

# ── Valid values ──────────────────────────────────────────────────────────────

VALID_VALUES = {
    "scheduler":           ["fcfs", "sdf", "packing"],
    "allocator":           ["static", "graph", "noise_graph"],
    "shots":               None,   # any positive integer
    "router":              ["noise", "round_robin"],
    "router_queue_weight": None,   # any float in [0, 1]
    "router_noise_weight": None    # any float in [0, 1]
}

# ── Human-readable labels ─────────────────────────────────────────────────────

SCHEDULER_LABELS = {
    "fcfs":        "First Come First Served",
    "sdf":         "Shortest Depth First",
    "packing":     "Circuit Packing Scheduler"
}

ALLOCATOR_LABELS = {
    "static":      "Static Allocator",
    "graph":       "Graph Allocator",
    "noise_graph": "Noise Aware Graph Allocator"
}

ROUTER_LABELS = {
    "noise":       "Noise Aware Router",
    "round_robin": "Round Robin Router"
}


# ── Public entry points ───────────────────────────────────────────────────────

def load_global_config(config_path=None):
    '''
    Resolve the GLOBAL config scope: core defaults ← global user file.

    Args:
        config_path: optional path to the global user JSON config file

    Returns:
        (config, provenance) for GLOBAL keys only
    '''
    config     = dict(GLOBAL_DEFAULTS)
    provenance = {k: "DevQ Core" for k in GLOBAL_DEFAULTS}

    if config_path:
        user_cfg = _load_user_config(config_path)
        for key, val in user_cfg.items():
            if key in DEVICE_KEYS:
                continue    # device-scope keys handled by load_device_config
            if _validate_key_value(key, val, source="User (global)"):
                config[key]     = val
                provenance[key] = "User (global)"

    return config, provenance


def load_device_config(provider, index,
                       global_config_path=None, device_config_path=None):
    '''
    Resolve the DEVICE config scope for one attached device:
    core defaults ← provider preferred_config() ← global user file
    ← per-device user file.

    Args:
        provider:           BaseProvider instance backing this device
        index:              device index (for provenance labels)
        global_config_path: the DevQ-level user config file, if any
        device_config_path: this device's user config file, if any

    Returns:
        (config, provenance) for DEVICE keys only
    '''
    config     = dict(DEVICE_DEFAULTS)
    provenance = {k: "DevQ Core" for k in DEVICE_DEFAULTS}

    # Level 2 — provider preferred config
    provider_prefs = _load_provider_config(provider)
    provider_label = type(provider).__name__

    for key, val in provider_prefs.items():
        if key in GLOBAL_KEYS:
            print(f"[Config] Warning: provider {provider_label} attempted to "
                  f"set global key '{key}' — providers may only set device "
                  f"keys ({', '.join(sorted(DEVICE_KEYS))}). Ignoring.")
            continue
        if _validate_key_value(key, val, source=provider_label):
            config[key]     = val
            provenance[key] = provider_label

    # Levels 3 & 4 — global user file, then per-device user file
    for path, label in ((global_config_path, "User (global)"),
                        (device_config_path, f"User (d{index})")):
        if not path:
            continue
        user_cfg = _load_user_config(path)
        for key, val in user_cfg.items():
            if key in GLOBAL_KEYS:
                continue    # global-scope keys handled by load_global_config
            if _validate_key_value(key, val, source=label):
                config[key]     = val
                provenance[key] = label

    return config, provenance


# ── Private helpers ───────────────────────────────────────────────────────────

def _load_provider_config(provider) -> dict:
    '''Call provider.preferred_config() safely.'''
    try:
        prefs = provider.preferred_config()
        if not isinstance(prefs, dict):
            print(f"[Config] Warning: {type(provider).__name__}.preferred_config() "
                  f"did not return a dict — ignoring.")
            return {}
        return prefs
    except Exception as e:
        print(f"[Config] Warning: could not load provider preferred config: {e}")
        return {}


def _load_user_config(config_path) -> dict:
    '''Load and parse a user JSON config file.'''
    path = os.path.expanduser(config_path)

    if not os.path.isfile(path):
        print(f"[Config] Warning: config file '{config_path}' not found — ignoring.")
        return {}

    try:
        with open(path) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            print(f"[Config] Warning: config file '{config_path}' is not a "
                  f"JSON object — ignoring.")
            return {}
        return cfg
    except json.JSONDecodeError as e:
        print(f"[Config] Warning: config file '{config_path}' is not valid "
              f"JSON ({e}) — ignoring.")
        return {}


def _validate_key_value(key, val, source) -> bool:
    '''Validate one key/value pair; warn and reject unknown or invalid.'''
    if key not in VALID_VALUES:
        print(f"[Config] Warning: unknown config key '{key}' from {source} — ignoring.")
        return False

    valid = VALID_VALUES[key]

    if valid is not None:
        if val not in valid:
            print(f"[Config] Warning: invalid value '{val}' for '{key}' from "
                  f"{source} — expected one of {valid}. Ignoring.")
            return False
        return True

    if key == "shots":
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            print(f"[Config] Warning: invalid value '{val}' for 'shots' from "
                  f"{source} — expected a positive integer. Ignoring.")
            return False
        return True

    if key in ("router_queue_weight", "router_noise_weight"):
        if not isinstance(val, (int, float)) or isinstance(val, bool) \
                or not 0.0 <= float(val) <= 1.0:
            print(f"[Config] Warning: invalid value '{val}' for '{key}' from "
                  f"{source} — expected a float in [0, 1]. Ignoring.")
            return False
        return True

    return True