'''
Tags: Main

Router package — device routing layer for distributed DevQ.

The router is the cluster-scheduler analogue sitting above the
per-device DeviceContexts: it decides WHICH device a job is bound to;
each context's local scheduler then decides WHEN it runs there.
'''