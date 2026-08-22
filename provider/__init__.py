'''
Tags: Main

providers — The provider contract and its implementations.

BaseProvider (Main) mandates exactly two methods — get_device() and
execute() — which is the entire integration surface for new hardware.
Implementation subpackages (devq, ibm, …) are tagged Provider.
'''