'''
Tags: Main

JobStates — The six-state job lifecycle.

READY    submitted, not yet seen by the scheduler
WAITING  allocation failed on resources — transient, retried
REJECTED unsatisfiable: no valid allocation can ever exist on this
         device under the job's thresholds — terminal, never dispatched
RUNNING  dispatched to the device provider
FINISHED execution completed successfully — terminal
FAILED   execution returned an error — terminal
'''

from enum import Enum

class JobStates(Enum):
    READY    = "READY"
    RUNNING  = "RUNNING"
    WAITING  = "WAITING"
    FINISHED = "FINISHED"
    FAILED   = "FAILED"
    REJECTED = "REJECTED"