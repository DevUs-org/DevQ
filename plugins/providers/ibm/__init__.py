'''
Tags: Plugin

ibm provider — IBM/Qiskit-family providers.

IBMProvider is the shared base: it reads DevQ's calibration surface from a
Qiskit BackendV2 Target and builds the full-device-width layout. Two
providers subclass it. IBMSimulatedProvider wraps Qiskit V2 fake backends
(real IBM calibration via the Target API; native 2Q gate auto-discovered per
backend) with AerSimulator noise-model execution — the shipped, tested
provider. IBMRealProvider executes on live hardware via QiskitRuntimeService;
it is an opt-in, account-gated research instrument (Tags: Research), not part
of the default provider set.
'''