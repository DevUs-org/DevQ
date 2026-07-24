'''
Tags: Main

Example entry point — launches a multi-device DevQ session:

    d0            — DevQ simulated provider, random 7-qubit topology
    nairobi (d1)  — IBM FakeNairobiV2 (real Nairobi calibration data)
    lagos   (d2)  — IBM FakeLagosV2   (real Lagos calibration data)

Devices may be given an optional name. A name is an alias, never a
replacement: d1 and nairobi refer to the same device everywhere, in
--exec/--no-exec and in device-scoped commands alike. d0 below is left
unnamed to show the mixed case. Names must be unique, cannot look like
an index (d0, d7, ...), and cannot shadow a shell keyword (q, e, b).

The NoiseRouter (default) binds each job to the best feasible device
by noise quality and queue pressure; pin jobs with --exec/--no-exec.
Edit the providers, backends, or config paths here; everything else
is handled by the DevQ core.

Pass --seed to make a session reproducible:

    python example.py                # unseeded (default)
    python example.py --seed 42      # identical device + counts every launch

The seed goes to the providers at construction; d0's generated topology
and error maps become fixed, and IBM execution counts replay job-for-job
across identical sessions. See "Reproducibility & Seeding" in the README.
'''

import argparse

from devq import DevQ
from providers.devq.devq_simulated_provider import DevQSimulatedProvider
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launch an example DevQ session.")
    parser.add_argument(
        "--seed",
        type    = int,
        default = None,
        help    = "seed the providers for a reproducible session "
                  "(omit for unseeded, non-deterministic behaviour)"
    )
    args = parser.parse_args()

    ibm = IBMSimulatedProvider(seed=args.seed)

    # IBM is not a DevQ built-in, and add_device() refuses a device whose
    # provider class was never registered. Register the CLASS; the
    # instance above is constructed here, with a seed DevQ never sees.
    DevQ(config_path='./config/config_examples/router_only.config.json') \
        .register_provider("ibm.simulated", IBMSimulatedProvider) \
        .add_device(DevQSimulatedProvider(seed=args.seed).get_device("random", 7)) \
        .add_devices([
            (ibm.get_device("FakeNairobiV2"), "nairobi"),
            (ibm.get_device("FakeLagosV2"),   "lagos"),
        ]) \
        .start()