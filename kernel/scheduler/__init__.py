'''
Tags: Main

scheduler — The scheduling contract and its implementations.

BaseScheduler (Main) defines enqueue/schedule and the shared
allocation-classification step. Implementations: PackingScheduler
(Default), FCFSScheduler and ShortestDepthScheduler (Alt) —
interchangeable via config with no other changes.
'''