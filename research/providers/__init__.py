'''
Tags: Main

research.providers — provider implementations that USE DevQ's provider
contract but are not part of DevQ core. Kept under research/ because they
are opt-in, account-gated, or cost real resources (e.g. real quantum
hardware), none of which belongs in the shipped provider set or the core
test suite. They implement BaseProvider exactly as a core provider does,
so the kernel, router, allocator and metrics treat them identically.
'''