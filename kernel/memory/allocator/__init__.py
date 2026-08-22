'''
Tags: Main

allocators — The allocation contract and its implementations.

BaseAllocator (Main) defines allocate() and feasible(); filtering
(Main) holds the shared hard-constraint helpers. Implementations:
NoiseGraphAllocator (Default), GraphAllocator and StaticAllocator
(Alt) — interchangeable via config with no other changes.
'''