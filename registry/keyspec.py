'''
Tags: Main

KeySpec — Declarative description of a single DevQ configuration key.

This module is the PLUGIN-FACING half of the registry: a third-party
scheduler, allocator, router or provider that wants its own tunables to
participate in DevQ's configuration cascade declares them here and
nowhere else.

    from registry.keyspec import KeySpec, NormaliseGroup, positive_int

    class QOSScheduler(BaseScheduler):
        CONFIG_SCHEMA = {
            "qos.batch_window": KeySpec(
                scope    = "device",
                default  = 5,
                validate = positive_int,
                label    = "QOS batch window"
            ),
        }

One declaration buys the key four things, with no edits anywhere in
DevQ core:
    - a place in the configuration cascade (via `scope` + `default`)
    - validation of user-supplied values (via `validate`)
    - a human-readable line in `qconfig` (via `label`)
    - optional normalisation against sibling keys (via NormaliseGroup)

NAMESPACING — every plugin key MUST be namespaced with a prefix and a
dot ("qos.batch_window", not "batch_window"). The registry rejects
un-namespaced plugin keys. Namespacing keeps `qconfig` readable, makes
the plugin boundary visible in published benchmark artifacts, and stops
two independent plugins from colliding on a name like "window".

SCOPES — where in the cascade a key is resolved:
    "device"  resolved independently for every attached device, through
              the full four-level cascade (core -> provider -> global
              user file -> per-device user file)
    "global"  resolved once for the whole system (core -> global user
              file). Providers may NEVER set a global key, including one
              they declared themselves — a provider expressing
              system-level policy is a layer violation.
    "common"  resolved in BOTH scopes independently. Used for a concept
              with two consumers at different levels, e.g. the noise
              cost weights, whose global copy steers the router's
              scoring yardstick while each device's copy steers that
              device's allocator.

Which scopes a plugin may declare depends on what kind of component it
is; the registry enforces this at registration time:
    scheduler / allocator  ->  "device" or "common"
    router    / provider   ->  "global" or "common"

VALIDATORS — a validator is a plain callable taking the user-supplied
value and returning EITHER None (the value is acceptable) OR a string
describing what was expected. The string is spliced into the loader's
warning, so it should complete the sentence "... for 'key' from
<source> — <message>. Ignoring.":

    def positive_int(value):
        if isinstance(value, bool) or not isinstance(value, int):
            return "expected an integer"
        if value <= 0:
            return "expected a positive integer"
        return None

Message-on-failure rather than a bare False so that a validator which
can fail for several distinct reasons reports the RIGHT one, and so
that a validator whose legal set is dynamic (e.g. "one of the currently
registered scheduler names") can render that set at the moment of
failure. A validator that forgets to return None on the happy path
would silently reject everything; the registry guards against this by
checking each key's own default against its validator at registration.
'''

from dataclasses import dataclass
from typing import Any, Callable, Sequence


# Legal values for KeySpec.scope. Kept here rather than in the registry
# because a plugin author reading this file needs them.
SCOPES = frozenset({"device", "global", "common"})


@dataclass(frozen=True)
class KeySpec:
    '''
    Everything DevQ needs to know about one configuration key.

    Attributes:
        scope:           "device" | "global" | "common" — see module
                         docstring. Determines which cascade(s) resolve
                         this key.
        default:         the DevQ Core value, used when no provider or
                         user file supplies one. Must itself satisfy
                         `validate` (checked at registration).
        validate:        callable(value) -> None if acceptable, else a
                         string describing what was expected.
        label:           human-readable name shown by `qconfig`.
        normalise_group: optional name of a NormaliseGroup this key
                         belongs to. Keys sharing a group are scaled to
                         sum to 1 after the cascade completes. The group
                         itself must be declared separately (see
                         NormaliseGroup); this field only names it.
    '''
    scope:           str
    default:         Any
    validate:        Callable[[Any], str | None]
    label:           str
    normalise_group: str | None = None


@dataclass(frozen=True)
class NormaliseGroup:
    '''
    A set of configuration keys that are scaled to sum to 1 together.

    Only the RATIO between members carries meaning, so a user may write
    the group on any scale — 1/9, 0.1/0.9 and 2/18 are equivalent. Each
    member cascades independently; normalisation happens once afterwards,
    per scope. `qconfig` shows the effective normalised values.

    Declared alongside the schema on the plugin class:

        CONFIG_GROUPS = {
            "qos.blend": NormaliseGroup(["qos.wait_weight", "qos.fid_weight"]),
        }

    Every member must also appear in CONFIG_SCHEMA with a matching
    `normalise_group`, and a group must have at least two members — both
    checked at registration, because a dangling or lonely member is
    almost always a typo whose only symptom would be a quietly wrong
    number in a benchmark.

    When every member of a group resolves to 0 the ratio is undefined,
    and leaving it would make all candidate scores identical — silently
    degrading the consuming policy to "first candidate found". The
    loader warns and reverts the whole group to its core defaults.

    Attributes:
        members: the keys in this group. Order is irrelevant.
    '''
    members: Sequence[str]


# ── Stock validators ─────────────────────────────────────────────────────────
#
# Provided so that the common cases need no plugin-authored code. Each
# follows the contract in the module docstring: None means acceptable,
# a string means rejected and says what was expected.
#
# Note the explicit `isinstance(value, bool)` guards. In Python, bool is
# a subclass of int, so True would otherwise pass as a positive integer
# and silently become shots=1.


def positive_int(value):
    '''Accept integers strictly greater than zero.'''
    if isinstance(value, bool) or not isinstance(value, int):
        return "expected an integer"
    if value <= 0:
        return "expected a positive integer"
    return None


def non_negative(value):
    '''Accept any number >= 0. Used for cost weights before normalisation.'''
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "expected a number"
    if float(value) < 0.0:
        return "expected a non-negative number"
    return None


def unit_interval(value):
    '''Accept any number in the closed interval [0, 1].'''
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "expected a number"
    if not 0.0 <= float(value) <= 1.0:
        return "expected a number in [0, 1]"
    return None


def one_of(*permitted):
    '''
    Build a validator accepting only the given values.

    For a FIXED set of literals. A key whose legal values depend on what
    is currently registered (scheduler, allocator, router names) should
    NOT use this — the loader supplies a registry-backed validator for
    those, so that registering a plugin makes its name legal immediately
    rather than requiring a second edit here.
    '''
    permitted = tuple(permitted)
    rendered  = ", ".join(repr(p) for p in permitted)

    def _validate(value):
        if value in permitted:
            return None
        return f"expected one of {rendered}"

    return _validate


def non_empty_string(value):
    '''Accept a string with at least one non-whitespace character.'''
    if not isinstance(value, str):
        return "expected a string"
    if not value.strip():
        return "expected a non-empty string"
    return None