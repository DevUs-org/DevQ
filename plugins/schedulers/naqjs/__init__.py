'''
Tags: Plugin

naqjs — the NAQJS scheduler baseline (Wu et al., ICCAD 2024). A scored,
sweepable queue-rearranging scheduler: each cycle it sorts the queue by a
weighted sum of per-job width/shots/sequence features and packs jobs until
cumulative width would exceed a device-fraction bound. Registered by hand
as a scheduler.
'''
