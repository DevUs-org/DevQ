'''
Tags: Main

ConfigLoader — Three-level configuration system for DevQ.

Merge order (later levels win):
    1. DevQ core defaults
    2. Provider preferred config  (provider.preferred_config())
    3. User local config file     (any path, JSON)

Provenance is tracked for every key so qconfig can show
where each active value came from.
'''

import json
import os


# ── Core defaults ─────────────────────────────────────────────────────────────

CORE_DEFAULTS = {
    "scheduler": "packing",
    "allocator": "noise_graph",
    "shots":     1024
}

# ── Valid values ──────────────────────────────────────────────────────────────

VALID_VALUES = {
    "scheduler": ["fcfs", "sdf", "packing"],
    "allocator": ["static", "graph", "noise_graph"],
    "shots":     None   # any positive integer
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


# ── Public entry point ────────────────────────────────────────────────────────

def load_config(provider, config_path=None):
    '''
    Build the active configuration by merging three levels.

    Args:
        provider:    a BaseProvider instance — queried for preferred_config()
        config_path: optional path to a user JSON config file

    Returns:
        (config, provenance)
        config:     dict of active key → value
        provenance: dict of key → source label string
    '''
    config     = dict(CORE_DEFAULTS)
    provenance = {k: "DevQ Core" for k in CORE_DEFAULTS}

    # Level 2 — provider preferred config
    provider_prefs  = _load_provider_config(provider)
    provider_label  = type(provider).__name__

    for key, val in provider_prefs.items():
        if _validate_key_value(key, val, source=provider_label):
            config[key]     = val
            provenance[key] = provider_label

    # Level 3 — user local config file
    if config_path:
        user_cfg = _load_user_config(config_path)
        for key, val in user_cfg.items():
            if _validate_key_value(key, val, source="User Defined"):
                config[key]     = val
                provenance[key] = "User Defined"

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


def _load_user_config(config_path: str) -> dict:
    '''Load and parse a user JSON config file from any path.'''
    path = os.path.expanduser(config_path)

    if not os.path.exists(path):
        print(f"[Config] Warning: config file not found at '{path}' — using defaults.")
        return {}

    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f"[Config] Warning: config file must be a JSON object — ignoring.")
            return {}
        return data
    except json.JSONDecodeError as e:
        print(f"[Config] Warning: could not parse config file '{path}': {e} — using defaults.")
        return {}


def _validate_key_value(key, val, source: str) -> bool:
    '''
    Validate a single key-value pair.
    Returns True if valid, False and prints a warning if not.
    '''
    if key not in VALID_VALUES:
        print(f"[Config] Warning: unknown config key '{key}' from {source} — ignoring.")
        return False

    allowed = VALID_VALUES[key]

    if allowed is None:
        # shots — must be a positive integer
        if not isinstance(val, int) or val <= 0:
            print(f"[Config] Warning: invalid value for 'shots' from {source}: "
                  f"'{val}' — must be a positive integer. Falling back to default.")
            return False
        return True

    if val not in allowed:
        print(f"[Config] Warning: invalid value for '{key}' from {source}: "
              f"'{val}' — must be one of {allowed}. Falling back to default.")
        return False

    return True