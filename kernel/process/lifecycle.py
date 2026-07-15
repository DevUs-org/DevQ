'''
Tags: Main

Enum for Job Statuses
'''

from enum import Enum

class JobStates(Enum):
    READY    = "READY"
    RUNNING  = "RUNNING"
    WAITING  = "WAITING"     # blocked on resources — transient, retried
    FINISHED = "FINISHED"
    FAILED   = "FAILED"      # dispatched, execution errored — terminal
    REJECTED = "REJECTED"    # never dispatched: no valid allocation can
                             # exist on this device under the job's
                             # thresholds — terminal