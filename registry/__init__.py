'''
Tags: Main

registry — DevQ's extensibility surface.

Third-party schedulers, allocators, routers and providers are attached
to a DevQ instance through this package, without editing DevQ core:

    registry.keyspec   plugin-facing declarations (KeySpec,
                       NormaliseGroup, stock validators)
    registry.registry  the Registry itself — name -> class resolution,
                       contract validation at registration time

Deliberately empty of logic: import from the submodules directly, e.g.
`from registry.keyspec import KeySpec`.
'''