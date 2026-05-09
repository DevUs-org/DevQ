'''
Tags: Main

Enum for Job Statuses
'''

from enum import Enum

class JobStates(Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING  = "WAITING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"