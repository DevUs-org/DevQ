'''
Tags: Main

ConfigLoader — Four-level configuration system for DevQ.

Every configuration key, core or plugin-contributed, is described by a
single KeySpec (see registry/keyspec.py) recording its scope, its DevQ
Core default, how to validate a user-supplied value, and its
human-readable label. One declaration therefore buys a key its place in
the cascade, its validation, and its qconfig line — there is no second
table to keep in step.

Keys are split into three scopes:

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
  such keys are warned about and ignored. This holds for EVERY global
  key, including one the provider itself declared: declaring a key and
  being entitled to set it are different things.

  COMMON keys (qubit_error_weight, edge_error_weight) — the alpha/beta
  of the noise cost S = a*sum(qubit_error) + b*sum(edge_error); one
  concept with two consumers, so the pair is resolved in BOTH scopes:
  the global copy feeds the NoiseRouter's scoring yardstick (one uniform
  ruler across all candidates), each device's copy rides the full device
  cascade and feeds that device's allocator.

NORMALISATION. Keys may belong to a NormaliseGroup, whose members are
scaled to sum to 1 after that scope's cascade completes — only the
ratio between members carries meaning, so users may write 1/9, 0.1/0.9
or 2/18 equivalently. Groups are declared, not inferred, so that
membership is stated once rather than agreed between keys. A group whose
members all resolve to 0 has an undefined ratio and would make every
candidate score identical, silently degrading the consuming policy to
"first candidate found"; the whole group reverts to core defaults with a
warning.

Provenance is tracked for every key so qconfig can show where each
active value came from: "DevQ Core", "<ProviderName>",
"User (global)", "User (dN)".

INSTANCE-SCOPED. The loader is constructed with the DevQ instance's
Registry and consults it for two things: the set of legal values for
keys naming a component (so registering a scheduler makes its name a
legal value of the "scheduler" key immediately, with no second edit),
and the config keys plugins have contributed. It is therefore per-DevQ
state, not module state.
'''

import json
import os

from registry.keyspec import (KeySpec, NormaliseGroup,
                              positive_int, non_negative, unit_interval)


# -- Core key specifications --------------------------------------------------
#
# The eight keys DevQ itself defines. Plugin-contributed keys are merged
# on top of these from the registry; core keys are deliberately NOT
# registered as though they belonged to a component, because they don't
# -- "shots" is owned by no scheduler, and giving it a synthetic owner
# would make registry.schema() mean something other than "what plugins
# added".
#
# Core keys are un-namespaced. Plugin keys must be namespaced, which is
# what keeps the two sets from ever colliding.

def _core_specs(registry):
    '''
    Build the core KeySpec table for one registry.

    A function rather than a module constant because the validators for
    scheduler / allocator / router close over the registry: their legal
    values are whatever is registered at the moment of validation.
    '''
    return {
        "scheduler": KeySpec(
            scope    = "device",
            default  = "packing",
            validate = _in_registry(registry, "scheduler"),
            label    = "Scheduler",
        ),
        "allocator": KeySpec(
            scope    = "device",
            default  = "noise_graph",
            validate = _in_registry(registry, "allocator"),
            label    = "Allocator",
        ),
        "shots": KeySpec(
            scope    = "device",
            default  = 1024,
            validate = positive_int,
            label    = "Shots",
        ),
        "router": KeySpec(
            scope    = "global",
            default  = "noise",
            validate = _in_registry(registry, "router"),
            label    = "Router",
        ),
        "router_queue_weight": KeySpec(
            scope           = "global",
            default         = 0.5,
            validate        = unit_interval,
            label           = "Router queue weight",
            normalise_group = "router_blend",
        ),
        "router_noise_weight": KeySpec(
            scope           = "global",
            default         = 0.5,
            validate        = unit_interval,
            label           = "Router noise weight",
            normalise_group = "router_blend",
        ),
        "qubit_error_weight": KeySpec(
            scope           = "common",
            default         = 0.1,
            validate        = non_negative,
            label           = "Qubit error weight",
            normalise_group = "noise_cost",
        ),
        "edge_error_weight": KeySpec(
            scope           = "common",
            default         = 0.9,
            validate        = non_negative,
            label           = "Edge error weight",
            normalise_group = "noise_cost",
        ),
    }


_CORE_GROUPS = {
    "noise_cost":   NormaliseGroup(["qubit_error_weight", "edge_error_weight"]),
    "router_blend": NormaliseGroup(["router_queue_weight", "router_noise_weight"]),
}


def _in_registry(registry, kind):
    '''
    Build a validator accepting any name currently registered for a kind.

    This is the seam that removes the duplicate list of legal policy
    names. Registering a scheduler makes its name a legal value of the
    "scheduler" config key at once, and the rejection message renders
    the names that are actually available rather than a stale literal.
    '''
    def _validate(value):
        names = registry.names(kind)
        if value in names:
            return None
        rendered = "[" + ", ".join(repr(n) for n in names) + "]" if names else "none registered"
        return f"expected one of {rendered}"

    return _validate


class ConfigLoader:
    '''
    Resolves DevQ configuration for one DevQ instance.

    Construct once per DevQ object, after its registry has been seeded
    with built-ins and any user plugins, and before build() reads any
    configuration.
    '''

    def __init__(self, registry):
        self._registry = registry
        self._core     = _core_specs(registry)

    # -- Spec access ----------------------------------------------------------

    def specs(self):
        '''
        Every known key -> KeySpec: core keys plus plugin contributions.

        Rebuilt on each call rather than cached, because a plugin
        registered after this loader was constructed must be visible.
        Core keys win on collision, but the registry's mandatory
        namespacing means a plugin cannot declare one.
        '''
        merged = dict(self._registry.schema())
        merged.update(self._core)
        return merged

    def groups(self):
        '''Every normalise group: core plus plugin contributions.'''
        merged = dict(self._registry.groups())
        merged.update(_CORE_GROUPS)
        return merged

    def labels(self):
        '''
        Display labels for qconfig, as {kind: {name: label}}.

        Nested by component kind so that a new kind (Phase 6 frontend
        adapters) needs no new argument threaded through to the shell.
        Passed to QShell as data at construction: the shell renders
        configuration, it does not resolve it, and handing it the loader
        would let it re-read config or reach the registry.
        '''
        out = {}
        for kind in self._registry.kinds():
            out[kind] = {
                name: self._describe(kind, name)
                for name in self._registry.names(kind)
            }
        return out

    def _describe(self, kind, name):
        '''Human label for a registered component, falling back to its name.'''
        component = self._registry.get(kind, name)
        cls       = component if isinstance(component, type) else type(component)
        return getattr(cls, "LABEL", None) or cls.__name__

    # -- Public entry points --------------------------------------------------

    def load_global(self, config_path=None):
        '''
        Resolve the GLOBAL config scope: core defaults <- global user file.

        Args:
            config_path: optional path to the global user JSON config file

        Returns:
            (config, provenance) for GLOBAL and COMMON keys
        '''
        specs = self.specs()
        keys  = {k: s for k, s in specs.items()
                 if s.scope in ("global", "common")}

        config     = {k: s.default for k, s in keys.items()}
        provenance = {k: "DevQ Core" for k in keys}

        if config_path:
            user_cfg = self._load_user_config(config_path)
            for key, val in user_cfg.items():
                spec = specs.get(key)
                # Device-scope keys in the same file are handled by
                # load_device(); silently skipped rather than warned about,
                # since one file may legitimately carry both scopes.
                if spec is not None and spec.scope == "device":
                    continue
                if self._validate_key_value(specs, key, val, "User (global)"):
                    config[key]     = val
                    provenance[key] = "User (global)"

        self._normalise_groups(specs, config, provenance)
        return config, provenance

    def load_device(self, provider, index,
                    global_config_path=None, device_config_path=None):
        '''
        Resolve the DEVICE config scope for one attached device:
        core defaults <- provider preferred_config() <- global user file
        <- per-device user file.

        Args:
            provider:           BaseProvider instance backing this device
            index:              device index (for provenance labels)
            global_config_path: the DevQ-level user config file, if any
            device_config_path: this device's user config file, if any

        Returns:
            (config, provenance) for DEVICE and COMMON keys
        '''
        specs = self.specs()
        keys  = {k: s for k, s in specs.items()
                 if s.scope in ("device", "common")}

        config     = {k: s.default for k, s in keys.items()}
        provenance = {k: "DevQ Core" for k in keys}

        # Level 2 -- provider preferred config
        provider_prefs = self._load_provider_config(provider)
        provider_label = type(provider).__name__

        device_key_names = sorted(k for k, s in specs.items()
                                  if s.scope == "device")

        for key, val in provider_prefs.items():
            spec = specs.get(key)
            if spec is not None and spec.scope == "global":
                print(f"[Config] Warning: provider {provider_label} attempted to "
                      f"set global key '{key}' — providers may only set device "
                      f"keys ({', '.join(device_key_names)}). Ignoring.")
                continue
            if self._validate_key_value(specs, key, val, provider_label):
                config[key]     = val
                provenance[key] = provider_label

        # Levels 3 & 4 -- global user file, then per-device user file
        for path, label in ((global_config_path, "User (global)"),
                            (device_config_path, f"User (d{index})")):
            if not path:
                continue
            user_cfg = self._load_user_config(path)
            for key, val in user_cfg.items():
                spec = specs.get(key)
                # Global-scope keys in the same file are handled by
                # load_global().
                if spec is not None and spec.scope == "global":
                    continue
                if self._validate_key_value(specs, key, val, label):
                    config[key]     = val
                    provenance[key] = label

        self._normalise_groups(specs, config, provenance)
        return config, provenance

    # -- Normalisation --------------------------------------------------------

    def _normalise_groups(self, specs, config, provenance):
        '''
        Scale every normalise group present in this scope to sum to 1.

        Members whose keys are not in this scope's resolved config are
        simply absent -- router_blend has global-scope members, so it
        does not appear in a device resolve, and noise_cost has
        common-scope members, so it appears in both. That falls out of
        the scoping rather than needing a per-scope call list.
        '''
        for name, group in self.groups().items():
            members = [m for m in group.members if m in config]
            if len(members) < 2:
                continue
            self._normalise(name, members, specs, config, provenance)

    def _normalise(self, name, members, specs, config, provenance):
        '''
        Scale one group's members to sum to 1.

        Only the RATIO between members affects the decision the consuming
        policy makes, so normalising keeps behaviour identical while
        putting cost scores on one comparable scale everywhere. Each key
        cascades independently, THEN the group is normalised; qconfig
        shows the effective normalised values.

        A group whose members all resolve to 0 would make every candidate
        score 0, silently degrading the consuming policy to "first
        candidate found" -- rejected here with a fall back to core
        defaults.
        '''
        values = [float(config[m]) for m in members]
        total  = sum(values)

        if total <= 0.0:
            rendered = "/".join(f"{specs[m].default}" for m in members)
            # "both" reads correctly for the two-member core groups and
            # would be wrong for a larger plugin group.
            quantifier = "both" if len(members) == 2 else "all"
            listed     = (" and ".join(members) if len(members) == 2
                          else ", ".join(members))
            print(f"[Config] Warning: {listed} "
                  f"are {quantifier} 0. Falling back to core defaults "
                  f"({rendered}).")
            for member in members:
                config[member]     = specs[member].default
                provenance[member] = "DevQ Core"
            return

        for member, value in zip(members, values):
            config[member] = value / total

    # -- Private helpers ------------------------------------------------------

    def _load_provider_config(self, provider) -> dict:
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

    def _load_user_config(self, config_path) -> dict:
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

    def _validate_key_value(self, specs, key, val, source) -> bool:
        '''
        Validate one key/value pair; warn and reject unknown or invalid.

        The key's own validator supplies the message describing what was
        expected, so a key that can fail for several distinct reasons
        reports the right one, and a key whose legal set is dynamic
        renders the set as it stands now.
        '''
        spec = specs.get(key)

        if spec is None:
            print(f"[Config] Warning: unknown config key '{key}' from {source} — ignoring.")
            return False

        try:
            message = spec.validate(val)
        except Exception as e:
            print(f"[Config] Warning: validator for '{key}' raised "
                  f"{type(e).__name__} on value '{val}' from {source} "
                  f"({e}) — ignoring.")
            return False

        if message is not None:
            print(f"[Config] Warning: invalid value '{val}' for '{key}' from "
                  f"{source} — {message}. Ignoring.")
            return False

        return True