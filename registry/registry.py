'''
Tags: Main

Registry — DevQ's name -> component resolution and extensibility surface.

Every pluggable part of DevQ (scheduler, allocator, router, provider) is
referred to elsewhere in the system by a short string: in config files
("scheduler": "packing"), in benchmark workload specs ("provider":
"ibm"), and in qconfig output. The Registry is the single place that
maps those strings to classes, and the single gate every component —
built-in or third-party — passes through.

    devq = DevQ()
    devq.register_scheduler("qos", QOSScheduler)
    devq.register_provider("ionq", IonQProvider(api_key=...))
    devq.start()

INSTANCE-SCOPED, NOT GLOBAL. Each DevQ object owns its own Registry.
Two DevQ objects in one process do not share registrations, and a test
that registers a mock component cannot leak into the next test. This
matters concretely: the test suite builds ~30 shells in a single
process.

BUILT-INS REGISTER THROUGH THE SAME PATH. DevQ seeds its own schedulers,
allocators, routers and providers by calling the public register()
method, from devq.py, using the same validation every plugin faces. If
the extension path breaks, every built-in breaks immediately and loudly
rather than the plugin path quietly rotting while the shipped system
keeps working.

ONE REGISTRY, SEVERAL KINDS. The differences between kinds are data,
not code — see _KINDS below. Adding a new kind (Phase 6 frontend
adapters) is a row in that table, not a new module.

VALIDATION AT REGISTRATION TIME. A component that does not satisfy its
contract is rejected when it is registered, not when it is eventually
constructed several layers down. This is deliberate: DevQ has already
been bitten twice by contract violations that surfaced far from their
cause — a router whose __init__ took no arguments while the builder
passed four (every round-robin combination died), and a pool-like object
missing one method of the three its consumer called (the failure was
swallowed by a bare except and the shell hung forever). Both would have
been caught here. Registration performs:

    1. TYPE     — the component is a subclass (or instance) of the ABC
                  its kind requires.
    2. BIND     — its __init__ accepts exactly what DevQ will pass it,
                  checked with inspect.signature().bind() rather than
                  by construction, so no side effects run.
    3. METHODS  — the methods DevQ calls exist and accept the arguments
                  DevQ passes. For classes this is redundant with the
                  ABC; for registered INSTANCES, where abstractmethod
                  enforcement has already happened at construction, it
                  still guards against signature drift.
    4. SCHEMA   — any CONFIG_SCHEMA the component declares is namespaced,
                  uses scopes legal for its kind, and its defaults pass
                  its own validators.
    5. GROUPS   — any CONFIG_GROUPS it declares reference keys that
                  exist, have at least two members, and agree with the
                  normalise_group recorded on each member KeySpec.

INSTANCES vs CLASSES. Routers and providers may be registered as either
a class (DevQ constructs it) or a ready-made instance (the user
constructed it, perhaps with credentials or a seed DevQ knows nothing
about). Schedulers and allocators are CLASS-ONLY, and this is a
correctness constraint rather than a stylistic one: DevQ constructs one
scheduler and one allocator PER DEVICE, each bound to that device's own
MemoryManager and its own queue. A shared instance would silently merge
the per-device queues that the multi-device federation exists to keep
separate — a system that appears to work and is quietly wrong.

FREEZING. Once DevQ.build() has consumed the registry, it is frozen and
further registration raises. Registering after the maps have been read
would have no effect, and silently ignoring it is worse than refusing.
'''

import inspect
from dataclasses import dataclass
from typing import Sequence

from registry.keyspec import KeySpec, NormaliseGroup, SCOPES


class RegistryError(Exception):
    '''
    Raised for any violation of the registration contract.

    devq.py re-raises this as DevQError so that callers see one
    exception type from the DevQ facade; the distinct type here keeps
    registry-internal failures identifiable when the registry is used
    directly (e.g. in tests).
    '''
    pass


@dataclass(frozen=True)
class ComponentKind:
    '''
    Everything that differs between one pluggable kind and another.

    Attributes:
        base:             the ABC a component of this kind must subclass.
        init_params:      the parameter names DevQ passes to __init__.
                          Used for the bind check — NOT for construction,
                          which is done by the caller that owns the
                          runtime objects (devq.py).
        scopes:           config scopes a component of this kind may
                          declare keys in. A router knob is meaningless
                          per-device; a scheduler knob is meaningless
                          system-wide.
        accepts_instance: whether a ready-made instance may be
                          registered, or only a class. See the module
                          docstring — this is a correctness constraint
                          for per-device components.
        methods:          methods DevQ calls on the component, mapped to
                          the parameter names DevQ passes. Checked for
                          existence and signature compatibility.
        label:            human name for the kind, used in messages.
    '''
    base:             type
    init_params:      Sequence[str]
    scopes:           frozenset
    accepts_instance: bool
    methods:          dict
    label:            str


def _build_kinds():
    '''
    Construct the _KINDS table.

    Deferred into a function so that the imports of DevQ's own ABCs
    happen at call time. registry/ sits above kernel/ and providers/ in
    the dependency order, and importing them at module scope would make
    `from registry.keyspec import KeySpec` — the plugin-facing import —
    drag in the whole kernel.
    '''
    from kernel.scheduler.base_scheduler import BaseScheduler
    from kernel.memory.allocators.base_allocator import BaseAllocator
    from kernel.router.base_router import BaseRouter
    from providers.base_provider import BaseProvider

    return {
        "scheduler": ComponentKind(
            base             = BaseScheduler,
            init_params      = ("memory_manager", "process_table"),
            scopes           = frozenset({"device", "common"}),
            accepts_instance = False,
            methods          = {"schedule": ()},
            label            = "scheduler",
        ),
        "allocator": ComponentKind(
            base             = BaseAllocator,
            init_params      = ("qubit_error_weight", "edge_error_weight"),
            scopes           = frozenset({"device", "common"}),
            accepts_instance = False,
            methods          = {"allocate": ("circuit", "device", "pool",
                                             "max_qubit_error", "max_edge_error")},
            label            = "allocator",
        ),
        "router": ComponentKind(
            base             = BaseRouter,
            init_params      = ("router_queue_weight", "router_noise_weight",
                                "qubit_error_weight", "edge_error_weight"),
            scopes           = frozenset({"global", "common"}),
            accepts_instance = True,
            methods          = {"route": ("qcb", "contexts")},
            label            = "router",
        ),
        "provider": ComponentKind(
            base             = BaseProvider,
            init_params      = ("seed",),
            scopes           = frozenset({"global", "common"}),
            accepts_instance = True,
            methods          = {
                "get_device_from_spec": ("spec",),
                "execute":              ("circuit", "v2p_map", "shots", "device"),
                "preferred_config":     (),
            },
            label            = "provider",
        ),
    }


class Registry:
    '''
    Name -> component resolution for one DevQ instance.

    Holds registrations for every kind in _KINDS, the merged
    configuration schema contributed by registered components, and the
    normalisation groups they declare. The ConfigLoader consults this
    object so that the set of legal config values is derived from what
    is actually registered rather than from a hand-maintained duplicate
    list — registering a scheduler makes its name a legal value of the
    "scheduler" config key immediately, with no second edit.
    '''

    def __init__(self):
        self._kinds     = _build_kinds()
        self._entries   = {kind: {} for kind in self._kinds}
        self._schema    = {}      # key -> KeySpec, contributed by plugins
        self._groups    = {}      # group name -> NormaliseGroup
        self._key_owner = {}      # key -> "kind:name", for error messages
        self._frozen    = False

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, kind, name, component):
        '''
        Register one component under a name, after validating its contract.

        Args:
            kind:      one of the keys of _KINDS ("scheduler", ...)
            name:      the string used to refer to it in config files and
                       workload specs
            component: a class, or (for kinds whose accepts_instance is
                       True) a ready-made instance

        Raises:
            RegistryError: on any contract violation, or if the registry
                           has been frozen by DevQ.build()
        '''
        if self._frozen:
            raise RegistryError(
                f"cannot register {kind} '{name}' — the registry was frozen "
                "when build() ran. Register all components before calling "
                "build() or start()."
            )

        if kind not in self._kinds:
            raise RegistryError(
                f"unknown component kind '{kind}' — expected one of "
                f"{', '.join(sorted(self._kinds))}."
            )

        spec = self._kinds[kind]

        if not isinstance(name, str) or not name.strip():
            raise RegistryError(
                f"{spec.label} name must be a non-empty string, got {name!r}."
            )

        if name in self._entries[kind]:
            raise RegistryError(
                f"{spec.label} '{name}' is already registered. Choose another "
                "name — re-registering would silently change the meaning of "
                "existing config files."
            )

        cls = self._validate_component(kind, spec, name, component)
        self._validate_schema(kind, spec, name, cls)

        self._entries[kind][name] = component
        self._merge_schema(kind, name, cls)

    # ── Validation ───────────────────────────────────────────────────────────

    def _validate_component(self, kind, spec, name, component):
        '''
        Levels 1-3: type, constructor bind, and method signatures.

        Returns the CLASS to read declarations from — the component
        itself when a class was registered, type(component) when an
        instance was.
        '''
        is_instance = not inspect.isclass(component)

        if is_instance:
            if not spec.accepts_instance:
                raise RegistryError(
                    f"{spec.label} '{name}' was registered as an instance, but "
                    f"{spec.label}s must be registered as a CLASS. DevQ "
                    f"constructs one {spec.label} per attached device, each "
                    "bound to that device's own memory manager and queue; a "
                    "shared instance would merge state across devices. Pass "
                    f"{type(component).__name__} itself, not "
                    f"{type(component).__name__}(...)."
                )
            if not isinstance(component, spec.base):
                raise RegistryError(
                    f"{spec.label} '{name}' must be an instance of "
                    f"{spec.base.__name__}, got {type(component).__name__}."
                )
            cls = type(component)
        else:
            if not issubclass(component, spec.base):
                raise RegistryError(
                    f"{spec.label} '{name}' must subclass "
                    f"{spec.base.__name__}, got {component.__name__}."
                )
            cls = component
            self._check_bind(spec, name, cls)

        self._check_methods(spec, name, cls)
        return cls

    def _check_bind(self, spec, name, cls):
        '''
        Level 2 — does __init__ accept exactly what DevQ will pass?

        Checked with signature().bind() rather than by constructing the
        object: binding runs no user code and has no side effects, and
        it reports the mismatch in terms of parameter names rather than
        as a TypeError raised deep inside build().

        Skipped for a registered instance, which is already constructed.
        '''
        try:
            signature = inspect.signature(cls.__init__)
        except (TypeError, ValueError):
            return   # builtins and C-implemented types are not introspectable

        kwargs = {param: None for param in spec.init_params}

        try:
            signature.bind(None, **kwargs)
        except TypeError as e:
            passed = ", ".join(spec.init_params) or "no arguments"
            raise RegistryError(
                f"{spec.label} '{name}' ({cls.__name__}) cannot be constructed "
                f"by DevQ: __init__ must accept {passed}, but binding them "
                f"failed ({e}). Its signature is "
                f"{cls.__name__}{signature}."
            ) from None

    def _check_methods(self, spec, name, cls):
        '''
        Level 3 — do the methods DevQ calls exist, and accept what DevQ
        passes?

        The ABC guarantees abstract methods are implemented, but it does
        not check their signatures, and it says nothing about a class
        that satisfies the interface by duck typing. A near-miss here
        (a method present but taking the wrong arguments) otherwise
        surfaces as an AttributeError or TypeError at execution time,
        potentially swallowed by a broad except.
        '''
        for method_name, params in spec.methods.items():
            method = getattr(cls, method_name, None)

            if method is None or not callable(method):
                raise RegistryError(
                    f"{spec.label} '{name}' ({cls.__name__}) is missing the "
                    f"method {method_name}(), which DevQ calls."
                )

            try:
                signature = inspect.signature(method)
            except (TypeError, ValueError):
                continue

            try:
                signature.bind(None, **{p: None for p in params})
            except TypeError as e:
                rendered = ", ".join(params) or "no arguments"
                raise RegistryError(
                    f"{spec.label} '{name}' ({cls.__name__}): "
                    f"{method_name}() must accept {rendered}, but binding "
                    f"them failed ({e}). Its signature is "
                    f"{method_name}{signature}."
                ) from None

    def _validate_schema(self, kind, spec, name, cls):
        '''
        Levels 4-5 — the component's declared config keys and groups.

        A component contributes configuration by declaring CONFIG_SCHEMA
        (and optionally CONFIG_GROUPS) as class attributes. Both are
        validated here so that a malformed declaration fails at
        registration rather than producing a key that quietly never
        resolves.
        '''
        schema = getattr(cls, "CONFIG_SCHEMA", None) or {}
        groups = getattr(cls, "CONFIG_GROUPS", None) or {}

        if not isinstance(schema, dict):
            raise RegistryError(
                f"{spec.label} '{name}' ({cls.__name__}): CONFIG_SCHEMA must "
                f"be a dict of key -> KeySpec, got {type(schema).__name__}."
            )
        if not isinstance(groups, dict):
            raise RegistryError(
                f"{spec.label} '{name}' ({cls.__name__}): CONFIG_GROUPS must "
                f"be a dict of name -> NormaliseGroup, got "
                f"{type(groups).__name__}."
            )

        for key, key_spec in schema.items():
            self._validate_key(kind, spec, name, cls, key, key_spec)

        for group_name, group in groups.items():
            self._validate_group(spec, name, cls, group_name, group, schema)

        # Every member key naming a group must have that group declared,
        # either here or already present from an earlier registration.
        for key, key_spec in schema.items():
            group_name = key_spec.normalise_group
            if group_name is None:
                continue
            if group_name not in groups and group_name not in self._groups:
                raise RegistryError(
                    f"{spec.label} '{name}' ({cls.__name__}): key '{key}' "
                    f"names normalise group '{group_name}', but no such group "
                    "is declared in CONFIG_GROUPS."
                )

    def _validate_key(self, kind, spec, name, cls, key, key_spec):
        '''Validate one declared KeySpec.'''
        origin = f"{spec.label} '{name}' ({cls.__name__})"

        if not isinstance(key_spec, KeySpec):
            raise RegistryError(
                f"{origin}: CONFIG_SCHEMA['{key}'] must be a KeySpec, got "
                f"{type(key_spec).__name__}."
            )

        # Namespacing. Without it, two independent plugins declaring
        # "window" would collide, and qconfig could not show which knob
        # belongs to which component.
        if "." not in key or key.startswith(".") or key.endswith("."):
            raise RegistryError(
                f"{origin}: config key '{key}' must be namespaced as "
                f"'<prefix>.<key>' (for example '{name}.{key}'). Un-namespaced "
                "keys are reserved for DevQ core."
            )

        if key in self._schema:
            raise RegistryError(
                f"{origin}: config key '{key}' is already declared by "
                f"{self._key_owner[key]}."
            )

        if key_spec.scope not in SCOPES:
            raise RegistryError(
                f"{origin}: key '{key}' has unknown scope "
                f"'{key_spec.scope}' — expected one of "
                f"{', '.join(sorted(SCOPES))}."
            )

        if key_spec.scope not in spec.scopes:
            raise RegistryError(
                f"{origin}: key '{key}' declares scope '{key_spec.scope}', "
                f"which is not legal for a {spec.label}. A {spec.label} may "
                f"declare keys in: {', '.join(sorted(spec.scopes))}."
            )

        if not callable(key_spec.validate):
            raise RegistryError(
                f"{origin}: key '{key}' has a non-callable validator "
                f"({type(key_spec.validate).__name__})."
            )

        # The default must satisfy the key's own validator. This also
        # catches a validator that forgets to return None on the happy
        # path — such a validator would otherwise reject every value a
        # user ever supplied, while the default silently stood in.
        try:
            message = key_spec.validate(key_spec.default)
        except Exception as e:
            raise RegistryError(
                f"{origin}: validator for key '{key}' raised "
                f"{type(e).__name__} on its own default "
                f"{key_spec.default!r} ({e})."
            ) from None

        if message is not None:
            raise RegistryError(
                f"{origin}: default {key_spec.default!r} for key '{key}' is "
                f"rejected by that key's own validator ({message}). Either "
                "the default or the validator is wrong."
            )

    def _validate_group(self, spec, name, cls, group_name, group, schema):
        '''Validate one declared NormaliseGroup.'''
        origin = f"{spec.label} '{name}' ({cls.__name__})"

        if not isinstance(group, NormaliseGroup):
            raise RegistryError(
                f"{origin}: CONFIG_GROUPS['{group_name}'] must be a "
                f"NormaliseGroup, got {type(group).__name__}."
            )

        if group_name in self._groups:
            raise RegistryError(
                f"{origin}: normalise group '{group_name}' is already declared."
            )

        members = list(group.members)

        # A one-member group is almost always a typo — someone tagged one
        # key and misspelled the group name on its partner. Normalising a
        # single key would set it to 1.0 regardless of what the user
        # wrote, and the only symptom would be a wrong benchmark number.
        if len(members) < 2:
            raise RegistryError(
                f"{origin}: normalise group '{group_name}' has "
                f"{len(members)} member(s); a group needs at least two. "
                "Normalising one key alone would force it to 1.0 whatever "
                "the user configured."
            )

        if len(set(members)) != len(members):
            raise RegistryError(
                f"{origin}: normalise group '{group_name}' lists a member "
                "more than once."
            )

        scopes = set()
        for member in members:
            member_spec = schema.get(member) or self._schema.get(member)

            if member_spec is None:
                raise RegistryError(
                    f"{origin}: normalise group '{group_name}' names member "
                    f"'{member}', which is not declared in any CONFIG_SCHEMA."
                )

            if member_spec.normalise_group != group_name:
                declared = member_spec.normalise_group
                raise RegistryError(
                    f"{origin}: normalise group '{group_name}' claims member "
                    f"'{member}', but that key's KeySpec records "
                    f"normalise_group={declared!r}. The two declarations must "
                    "agree."
                )

            scopes.add(member_spec.scope)

        # Members are normalised together within one scope's cascade. If
        # they lived in different scopes they would be resolved by
        # different passes and could never be scaled against each other.
        if len(scopes) > 1:
            raise RegistryError(
                f"{origin}: normalise group '{group_name}' spans scopes "
                f"{', '.join(sorted(scopes))}. All members of a group must "
                "share one scope."
            )

    def _merge_schema(self, kind, name, cls):
        '''Record a validated component's config declarations.'''
        schema = getattr(cls, "CONFIG_SCHEMA", None) or {}
        groups = getattr(cls, "CONFIG_GROUPS", None) or {}
        owner  = f"{self._kinds[kind].label} '{name}'"

        for key, key_spec in schema.items():
            self._schema[key]    = key_spec
            self._key_owner[key] = owner

        for group_name, group in groups.items():
            self._groups[group_name] = group

    # ── Lookup ───────────────────────────────────────────────────────────────

    def get(self, kind, name):
        '''
        Resolve a name to its registered component.

        Returns the class or instance exactly as registered; the caller
        decides whether to construct it, since only the caller holds the
        per-device runtime objects a scheduler or allocator needs.
        '''
        if kind not in self._entries:
            raise RegistryError(f"unknown component kind '{kind}'.")

        try:
            return self._entries[kind][name]
        except KeyError:
            known = ", ".join(sorted(self._entries[kind])) or "none"
            raise RegistryError(
                f"no {self._kinds[kind].label} registered under '{name}'. "
                f"Registered: {known}."
            ) from None

    def names(self, kind):
        '''
        Registered names for a kind, in registration order.

        The ConfigLoader validates config values against this, so a
        newly registered component is a legal config value immediately.
        '''
        if kind not in self._entries:
            raise RegistryError(f"unknown component kind '{kind}'.")
        return list(self._entries[kind])

    def kinds(self):
        '''All component kinds this registry knows about.'''
        return list(self._kinds)

    def schema(self):
        '''Merged plugin-contributed config keys: key -> KeySpec.'''
        return dict(self._schema)

    def groups(self):
        '''Merged plugin-contributed normalise groups: name -> NormaliseGroup.'''
        return dict(self._groups)

    def owner_of(self, key):
        '''Which component declared a config key, for messages. None if core.'''
        return self._key_owner.get(key)

    # ── Freezing ─────────────────────────────────────────────────────────────

    def freeze(self):
        '''
        Close the registry to further registration.

        Called by DevQ.build() once the maps have been read. Registering
        afterwards could not affect the built system, so refusing is
        better than accepting and ignoring.
        '''
        self._frozen = True

    @property
    def frozen(self):
        return self._frozen