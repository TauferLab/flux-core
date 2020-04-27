#!/usr/bin/env python

from __future__ import print_function
import argparse
import os
import re
import uuid
import csv
import math
import json
import logging
import heapq
import random
from abc import ABCMeta, abstractmethod
from datetime import datetime, timedelta
from collections import Sequence, namedtuple, OrderedDict

import six
import flux
import flux.job
import flux.util
import flux.kvs
import flux.constants


def create_resource(res_type, count, with_child=[]):
    assert isinstance(with_child, Sequence), "child resource must be a sequence"
    assert not isinstance(with_child, str), "child resource must not be a string"
    assert count > 0, "resource count must be > 0"

    res = {"type": res_type, "count": count}

    if len(with_child) > 0:
        res["with"] = with_child
    return res


def create_slot(label, count, with_child):
    slot = create_resource("slot", count, with_child)
    slot["label"] = label
    return slot


class Job(object):
    def __init__(self, nnodes, ncpus, submit_time, elapsed_time, timelimit, io_sens, resubmit=False, rerun=False, IO=0, original_start=False, good_put=None, exitcode=0):
        self.nnodes = nnodes
        self.ncpus = ncpus
        self.submit_time = submit_time
        self.elapsed_time = elapsed_time
        self.timelimit = timelimit
        self.exitcode = exitcode
        self.io_sens = io_sens
        self.IO = IO
        self.resubmit = resubmit
        self.rerun = rerun
        self.original_start = original_start
        self.good_put = good_put
        self.start_time = None
        self.state_transitions = {}
        self._jobid = None
        self._jobspec = None
        self._submit_future = None
        self._start_msg = None

    @property
    def jobspec(self):
        if self._jobspec is not None:
            return self._jobspec

        assert self.ncpus % self.nnodes == 0
        core = create_resource("core", self.ncpus / self.nnodes)
        slot = create_slot("task", 1, [core])
        if self.nnodes > 0:
            resource_section = create_resource("node", self.nnodes, [slot])
        else:
            resource_section = slot

        jobspec = {
            "version": 1,
            "resources": [resource_section],
            "tasks": [
                {
                    "command": ["sleep", "0"],
                    "slot": "task",
                    "count": {"per_slot": 1},
                }
            ],
            "attributes": {"system": {"duration": self.timelimit}},
        }

        self._jobspec = jobspec
        return self._jobspec

    def submit(self, flux_handle):
        jobspec_json = json.dumps(self.jobspec)
        logger.log(9, jobspec_json)
        flags = 0
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Submitting job with FLUX_JOB_DEBUG enabled")
            flags = flux.constants.FLUX_JOB_DEBUG
        self._submit_future = flux.job.submit_async(flux_handle, jobspec_json, flags=flags)

    @property
    def jobid(self):
        if self._jobid is None:
            if self._submit_future is None:
                raise ValueError("Job was not submitted yet. No ID assigned.")
            logger.log(9, "Waiting on jobid")
            self._jobid = flux.job.submit_get_id(self._submit_future)
            self._submit_future = None
            logger.log(9, "Received jobid: {}".format(self._jobid))
        return self._jobid

    @property
    def complete_time(self):
        if self.start_time is None:
            raise ValueError("Job has not started yet")
        return self.start_time + self.elapsed_time

    def start(self, flux_handle, start_msg, start_time):
        self.start_time = start_time
        self._start_msg = start_msg.copy()
        flux_handle.respond(
            self._start_msg, payload={"id": self.jobid, "type": "start", "data": {}}
        )

    def complete(self, flux_handle):
        # TODO: emit "finish" event
        flux_handle.respond(
            self._start_msg,
            payload={"id": self.jobid, "type": "finish", "data": {"status" : 0}}
        )
        # TODO: emit "done" event
        flux_handle.respond(
            self._start_msg,
            payload={"id": self.jobid, "type": "release", "data": {"ranks" : "all", "final": True}}
        )

    def cancel(self, flux_handle):
        flux.job.RAW.cancel(flux_handle, self.jobid, "Canceled by simulator")

    def insert_apriori_events(self, simulation):
        # TODO: add priority to `add_event` so that all submits for a given time
        # can happen consecutively, followed by the waits for the jobids
        simulation.add_event(self.submit_time, 'submit', simulation.submit_job, self)

    def record_state_transition(self, state, time):
        self.state_transitions[state] = time


class Contention(object):
    def __init__(self, start_time, end_time, severity):
        self.start_time = start_time
        self.end_time = end_time
        self.severity = severity
        self.id = uuid.uuid4()

    def modify_job(self, simulation, job):
        if not job.io_sens:
            return job
        contention_overlap = min(self.end_time, job.complete_time) - simulation.current_time
        job.elapsed_time += int(contention_overlap * random.uniform(*self.severity))
        job.elapsed_time = min(job.timelimit, job.elapsed_time)
        return job

    def start(self, simulation, event_list):
        new_event_list = EventList()
        for time, events in event_list.time_heap:
            for event_type, callback, data in events:
                if event_type == 'complete':
                    data = self.modify_job(simulation, data)
                    time = data.complete_time
                new_event_list.add_event(time, event_type, callback, data)

        return new_event_list

    def insert_apriori_events(self, simulation):
        simulation.add_event(self.start_time, 'contention', simulation.start_contention_event, self)


class EventList(six.Iterator):
    def __init__(self):
        self.time_heap = []
        self.time_map = {}
        self._current_time = None

    def add_event(self, time, event_type, callback, data):
        if self._current_time is not None and time <= self._current_time:
            logger.warn(
                "Adding a new event at a time ({}) <= the current time ({})".format(
                    time, self._current_time
                )
            )

        if time in self.time_map:
            self.time_map[time].append((event_type, callback, data))
        else:
            new_event_list = [(event_type, callback, data)]
            self.time_map[time] = new_event_list
            heapq.heappush(self.time_heap, (time, new_event_list))

    def __len__(self):
        return len(self.time_heap)

    def __iter__(self):
        return self

    def min(self):
        if self.time_heap:
            return self.time_heap[0]
        else:
            return None

    def max(self):
        if self.time_heap:
            time = max(self.time_map.keys())
            return self.time_map[time]
        else:
            return None

    def __next__(self):
        try:
            time, event_list = heapq.heappop(self.time_heap)
            self.time_map.pop(time)
            self._current_time = time  # used for warning messages in `add_event`
            return time, event_list
        except (IndexError, KeyError):
            raise StopIteration()


class Simulation(object):
    def __init__(
            self,
            flux_handle,\
            event_list,\
            job_map,\
            submit_job_hook=None,\
            start_job_hook=None,\
            complete_job_hook=None,\
            cancel_job_hook=None,\
            start_contention_hook=None,\
            end_contention_hook=None,\
            oracle=None,\
            resub_chance=0.5,\
            allow_contention=True
    ):
        self.event_list = event_list
        self.job_map = job_map
        self.current_time = 0
        self.flux_handle = flux_handle
        self.pending_inactivations = set()
        self.job_manager_quiescent = True
        self.submit_job_hook = submit_job_hook
        self.start_job_hook = start_job_hook
        self.complete_job_hook = complete_job_hook
        self.start_contention_hook = start_contention_hook
        self.end_contention_hook = end_contention_hook
        self.oracle=oracle
        self.contention_event = False
        self.sys_contention = False
        self.resub_chance = resub_chance
        self.allow_contention = allow_contention
        self.IO_usage = 0 # Current mean IO usage
        self.IO_limit = 300 # max IO usage before contention occurs
        self.queued_jobs = 0

    def add_event(self, time, event_type, callback, data):
        self.event_list.add_event(time, event_type, callback, data)

    def start_sys_contention(self, contention):
        if self.oracle:
            contention = self.oracle.predict_contention(contention)
        if self.start_contention_hook:
            self.start_contention_hook(self, contention)
        self.event_list = contention.start(self, self.event_list)
        self.sys_contention = contention
        self.add_event(contention.end_time, 'contention', self.end_sys_contention, contention)

    def end_sys_contention(self, contention):
        if self.end_contention_hook:
            self.end_contention_hook(self, contention)
        self.sys_contention = False
        if self.IO_usage > self.IO_limit:
            C = Contention(start_time=self.current_time+1,\
                           end_time=self.current_time+600,\
                           severity=(.2,.6))

            self.add_event(C.start_time, 'contention', self.start_sys_contention, C)

    def start_contention_event(self, contention):
        if self.oracle:
            contention = self.oracle.predict_contention(contention)
        if self.start_contention_hook:
            self.start_contention_hook(self, contention)
        self.event_list = contention.start(self, self.event_list)
        self.contention_event = contention
        self.add_event(contention.end_time, 'contention', self.end_contention_event, contention)

    def end_contention_event(self, contention):
        if self.end_contention_hook:
            self.end_contention_hook(self, contention)
        self.contention_event = False

    def submit_job(self, job):
        logger.debug("Submitting a new job")
        job.submit(self.flux_handle)
        self.job_map[job.jobid] = job
        logger.info("Submitted job {}".format(job.jobid))
        self.queued_jobs += 1
        if self.submit_job_hook:
            self.submit_job_hook(self, job)

    def false_submit_job(self, job):
        job.submit(self.flux_handle)
        self.job_map[job.jobid] = job

    def false_complete_job(self, job):
        job.complete(self.flux_handle)
        self.pending_inactivations.add(job)

    def resubmit_job(self, job, submit_time, start_msg):
        # Run the job for 1 second (gets around bug in job.cancel)
        # TODO: address bug in job.cancel
        job.start(self.flux_handle, start_msg, self.current_time)
        self.add_event(self.current_time+1, 'complete', self.false_complete_job, job)
        # Then make a new job and resubmit
        if job.original_start:
            original_start = job.original_start
        else:
            original_start = self.current_time
        job = Job(job.nnodes, job.ncpus, submit_time, job.elapsed_time, job.timelimit, job.io_sens, resubmit=True, IO=job.IO, original_start=original_start)
        self.add_event(job.submit_time, 'submit', self.false_submit_job, job)

    def start_job(self, jobid, start_msg):
        job = self.job_map[jobid]
        if self.oracle:
            job = self.oracle.predict_job(job)
            if self.queued_jobs == 1:
                pass

            elif (self.IO_usage + job.predicted_IO) > self.IO_limit:
                self.resubmit_job(job, self.current_time+600, start_msg)
                return None

            elif self.sys_contention:
                # Do we predict contention and IO-sens job? CanarIO Action
                if self.sys_contention.predicted and job.predicted_sens:
                    # TODO need to add logic in here so that if there are no other
                    # jobs to run, the IO sens job can still run!
                    # First cancel the job
                    self.resubmit_job(job, self.sys_contention.end_time+1, start_msg)
                    return None

            elif self.contention_event:
                # Do we predict contention and IO-sens job? CanarIO Action
                if self.contention_event.predicted and job.predicted_sens:
                    # TODO need to add logic in here so that if there are no other
                    # jobs to run, the IO sens job can still run!
                    # First cancel the job
                    self.resubmit_job(job, self.contention_event.end_time+1, start_msg)
                    return None

        if self.start_job_hook:
            self.start_job_hook(self, job)
        job.start(self.flux_handle, start_msg, self.current_time)
        logger.info("Started job {}".format(job.jobid))

        if self.sys_contention:
            job = self.sys_contention.modify_job(self, job)
        if self.contention_event:
            job = self.contention_event.modify_job(self, job)

        self.IO_usage += job.IO
        self.queued_jobs -= 1
        self.add_event(job.complete_time, 'complete', self.complete_job, job)
        logger.debug("Registered job {} to complete at {}".format(job.jobid, job.complete_time))
        # Check if IO_limit has exceeded and start contention if necessary
        if (self.IO_usage > self.IO_limit) and not self.sys_contention and self.allow_contention:
            C = Contention(start_time=self.current_time+1,\
                           end_time=self.current_time+600,\
                           severity=(.2,.6))

            self.add_event(C.start_time, 'contention', self.start_sys_contention, C)


        # Put updated job into job_map
        self.job_map[jobid] = job

    def complete_job(self, job):
        good_put = True
        if job.timelimit == job.elapsed_time:
            if self.resub_chance > random.uniform(0,1):
                good_put = False
        job.good_put = good_put
        if self.complete_job_hook:
            self.complete_job_hook(self, job)
        job.complete(self.flux_handle)
        logger.info("Completed job {}".format(job.jobid))
        self.pending_inactivations.add(job)
        self.IO_usage -= job.IO
        if not good_put:
                job = Job(job.nnodes, job.ncpus, self.current_time+1, job.elapsed_time, job.timelimit+3600, job.io_sens, rerun=True, IO=job.IO)
                self.add_event(job.submit_time, 'submit', self.submit_job, job)

    def record_job_state_transition(self, jobid, state):
        try:
            job = self.job_map[jobid]
        except KeyError:
            return
        job.record_state_transition(state, self.current_time)
        if state == 'INACTIVE' and job in self.pending_inactivations:
            self.pending_inactivations.remove(job)
            if self.is_quiescent():
                self.advance()

    def advance(self):
        try:
            self.current_time, events_at_time = next(self.event_list)
        except StopIteration:
            logger.info("No more events in event list, running post-sim analysis")
            self.post_verification()
            logger.info("Ending simulation")
            self.flux_handle.reactor_stop(self.flux_handle.get_reactor())
            return
        logger.info("Fast-forwarding time to {}".format(self.current_time))
        for _, event, data in events_at_time:
            event(data)
        logger.debug("Sending quiescent request for time {}".format(self.current_time))
        self.flux_handle.rpc("job-manager.quiescent", {"time": self.current_time}).then(
            lambda fut, arg: arg.quiescent_cb(), arg=self
        )
        self.job_manager_quiescent = False

    def is_quiescent(self):
        return self.job_manager_quiescent and len(self.pending_inactivations) == 0

    def quiescent_cb(self):
        logger.debug("Received a response indicating the system is quiescent")
        self.job_manager_quiescent = True
        if self.is_quiescent():
            self.advance()

    def post_verification(self):
        for jobid, job in six.iteritems(self.job_map):
            if 'INACTIVE' not in job.state_transitions:
                job_kvs_dir = flux.job.convert_id(jobid, "dec", "kvs")
                logger.warn("Job {} had not reached the inactive state by simulation termination time.".format(jobid))
                logger.debug("Job {}'s eventlog:".format(jobid))
                eventlog = flux.kvs.get_key_raw(self.flux_handle, job_kvs_dir + ".eventlog")
                for line in eventlog.splitlines():
                    json_event = json.loads(line)
                    logger.debug(json_event)

def datetime_to_epoch(dt):
    return int((dt - datetime(1970, 1, 1)).total_seconds())


re_dhms = re.compile(r"^\s*(\d+)[:-](\d+):(\d+):(\d+)\s*$")
re_hms = re.compile(r"^\s*(\d+):(\d+):(\d+)\s*$")


def walltime_str_to_timedelta(walltime_str):
    (days, hours, mins, secs) = (0, 0, 0, 0)
    match = re_dhms.search(walltime_str)
    if match:
        days = int(match.group(1))
        hours = int(match.group(2))
        mins = int(match.group(3))
        secs = int(match.group(4))
    else:
        match = re_hms.search(walltime_str)
        if match:
            hours = int(match.group(1))
            mins = int(match.group(2))
            secs = int(match.group(3))
    return timedelta(days=days, hours=hours, minutes=mins, seconds=secs)


@six.add_metaclass(ABCMeta)
class JobTraceReader(object):
    def __init__(self, tracefile):
        self.tracefile = tracefile

    @abstractmethod
    def validate_trace(self):
        pass

    @abstractmethod
    def read_trace(self):
        pass


def job_from_slurm_row(row):
    kwargs = {}
    if "ExitCode" in row:
        kwargs["exitcode"] = "ExitCode"
    if "IOSens" in row:
        kwargs["io_sens"] = bool(int(row["IOSens"]))
    if "IO" in row:
        kwargs["IO"] = int(row["IO"])

    submit_time = datetime_to_epoch(
        datetime.strptime(row["Submit"], "%Y-%m-%dT%H:%M:%S")
    )
    elapsed = walltime_str_to_timedelta(row["Elapsed"]).total_seconds()
    if elapsed <= 0:
        logger.warn("Elapsed time ({}) <= 0".format(elapsed))
    timelimit = walltime_str_to_timedelta(row["Timelimit"]).total_seconds()
    if elapsed > timelimit:
        logger.warn(
            "Elapsed time ({}) greater than Timelimit ({})".format(elapsed, timelimit)
        )
    nnodes = int(row["NNodes"])
    ncpus = int(row["NCPUS"])
    if nnodes > ncpus:
        logger.warn(
            "Number of Nodes ({}) greater than Number of CPUs ({}), setting NCPUS = NNodes".format(
                nnodes, ncpus
            )
        )
        ncpus = nnodes
    elif ncpus % nnodes != 0:
        old_ncpus = ncpus
        ncpus = math.ceil(ncpus / nnodes) * nnodes
        logger.warn(
            "Number of Nodes ({}) does not evenly divide the Number of CPUs ({}), setting NCPUS to an integer multiple of the number of nodes ({})".format(
                nnodes, old_ncpus, ncpus
            )
        )

    return Job(nnodes, ncpus, submit_time, elapsed, timelimit, **kwargs)


class SacctReader(JobTraceReader):
    required_fields = ["Elapsed", "Timelimit", "Submit", "NNodes", "NCPUS", "IOSens"]

    def __init__(self, tracefile):
        super(SacctReader, self).__init__(tracefile)
        self.determine_delimiter()

    def determine_delimiter(self):
        """
        sacct outputs data with '|' as the delimiter by default, but ',' is a more
        common delimiter in general.  This is a simple heuristic to figure out if
        the job trace is straight from sacct or has had some post-processing
        done that converts the delimiter to a comma.
        """
        with open(self.tracefile) as infile:
            first_line = infile.readline()
        self.delim = '|' if '|' in first_line else ','

    def validate_trace(self):
        with open(self.tracefile) as infile:
            reader = csv.reader(infile, delimiter=self.delim)
            header_fields = set(next(reader))
        for req_field in SacctReader.required_fields:
            if req_field not in header_fields:
                raise ValueError("Job file is missing '{}'".format(req_field))

    def read_trace(self):
        """
        You can obtain the necessary information from the sacct command using the -o flag.
        For example: sacct -o nnodes,ncpus,timelimit,state,submit,elapsed,exitcode
        """
        with open(self.tracefile) as infile:
            lines = [line for line in infile.readlines() if not line.startswith('#')]
            reader = csv.DictReader(lines, delimiter=self.delim)
            jobs = [job_from_slurm_row(row) for row in reader]
        return jobs


def insert_resource_data(flux_handle, num_ranks, cores_per_rank):
    """
    Populate the KVS with the resource data of the simulated system
    An example of the data format: {"0": {"Package": 7, "Core": 7, "PU": 7, "cpuset": "0-6"}}
    """
    if num_ranks <= 0:
        raise ValueError("Requires at least one rank")

    kvs_key = "resource.hwloc.by_rank"
    resource_dict = {}
    for rank in range(num_ranks):
        resource_dict[rank] = {}
        for key in ["Package", "Core", "PU"]:
            resource_dict[rank][key] = cores_per_rank
        resource_dict[rank]["cpuset"] = (
            "0-{}".format(cores_per_rank - 1) if cores_per_rank > 1 else "0"
        )
    put_rc = flux.kvs.put(flux_handle, kvs_key, resource_dict)
    if put_rc < 0:
        raise ValueError("Error inserting resource data into KVS, rc={}".format(put_rc))
    flux.kvs.commit(flux_handle)


def job_state_cb(flux_handle, watcher, msg, simulation):
    '''
    example payload: {u'transitions': [[63652757504, u'CLEANUP'], [63652757504, u'INACTIVE']]}
    '''
    logger.log(9, "Received a job state cb. msg payload: {}".format(msg.payload))
    for jobid, state, _ in msg.payload['transitions']:
        simulation.record_job_state_transition(jobid, state)

def get_loaded_modules(flux_handle):
    modules = flux_handle.rpc("cmb.lsmod").get()
    return modules["mods"]


def load_missing_modules(flux_handle):
    # TODO: check that necessary modules are loaded
    # if not, load them
    # return an updated list of loaded modules
    loaded_modules = get_loaded_modules(flux_handle)
    pass


def reload_scheduler(flux_handle):
    sched_module = "sched-simple"
    # Check if there is a module already loaded providing 'sched' service,
    # if so, reload that module
    for module in get_loaded_modules(flux_handle):
        if "sched" in module["services"]:
            sched_module = module["name"]

    logger.debug("Reloading the '{}' module".format(sched_module))
    flux_handle.rpc("cmb.rmmod", payload={"name": "sched-simple"}).get()
    path = flux.util.modfind("sched-simple")
    flux_handle.rpc("cmb.insmod", payload=json.dumps({"path": path, "args": []})).get()


def job_exception_cb(flux_handle, watcher, msg, cb_args):
    logger.warn("Detected a job exception, but not handling it")


def sim_exec_start_cb(flux_handle, watcher, msg, simulation):
    payload = msg.payload
    logger.log(9, "Received sim-exec.start request. Payload: {}".format(payload))
    jobid = payload["id"]
    simulation.start_job(jobid, msg)


def exec_hello(flux_handle):
    logger.debug("Registering sim-exec with job-manager")
    flux_handle.rpc("job-manager.exec-hello", payload={"service": "sim-exec"}).get()


def service_add(f, name):
    future = f.service_register(name)
    return f.future_get(future, None)


def service_remove(f, name):
    future = f.service_unregister(name)
    return f.future_get(future, None)


def setup_watchers(flux_handle, simulation):
    watchers = []
    services = set()
    for type_mask, topic, cb, args in [
        (flux.constants.FLUX_MSGTYPE_EVENT, "job-state", job_state_cb, simulation),
        (
            flux.constants.FLUX_MSGTYPE_REQUEST,
            "sim-exec.start",
            sim_exec_start_cb,
            simulation,
        ),
    ]:
        if type_mask == flux.constants.FLUX_MSGTYPE_EVENT:
            flux_handle.event_subscribe(topic)
        watcher = flux_handle.msg_watcher_create(
            cb, type_mask=type_mask, topic_glob=topic, args=args
        )
        watcher.start()
        watchers.append(watcher)
        if type_mask == flux.constants.FLUX_MSGTYPE_REQUEST:
            service_name = topic.split(".")[0]
            if service_name not in services:
                service_add(flux_handle, service_name)
                services.add(service_name)
    return watchers, services


def teardown_watchers(flux_handle, watchers, services):
    for watcher in watchers:
        watcher.stop()
    for service_name in services:
        service_remove(flux_handle, service_name)


Makespan = namedtuple('Makespan', ['beginning', 'end'])

class Oracle(object):
    def __init__(self, PRIONN_accuracy=1, CanarIO_job_accuracy=1, CanarIO_contention_accuracy=1):
        self.PRIONN_accuracy = PRIONN_accuracy
        self.CanarIO_job_accuracy = CanarIO_job_accuracy
        self.CanarIO_contention_accuracy = CanarIO_contention_accuracy

    def predict_job(self, job):
        job.predicted_sens = self.predict_io_sens(job)
        job.predicted_IO = self.predict_io(job)
        return job

    def predict_contention(self, contention):
        if self.CanarIO_contention_accuracy > random.uniform(0,1):
            contention.predicted = True
        else:
            contention.predicted = False
        return contention

    def predict_io_sens(self, job):
        if self.CanarIO_job_accuracy > random.uniform(0,1):
            return job.io_sens
        else:
            return not job.io_sens

    def predict_io(self, job):
        if self.PRIONN_accuracy == 1:
            return job.IO

        pred_acc = random.normalvariate(self.PRIONN_accuracy, 0.1)
        pred_acc = min(pred_acc, 1)
        pred = job.IO*pred_acc + (.5>random.uniform(0,1))*2*(1-pred_acc)*job.IO
        return pred


class SimpleExec(object):
    def __init__(self, num_nodes, cores_per_node, output):
        self.num_nodes = num_nodes
        self.cores_per_node = cores_per_node
        self.num_free_nodes = num_nodes
        self.output = output
        self.used_core_hours = 0
        self.makespan = Makespan(
            beginning=float('inf'),
            end=-1,
        )

    # Function that defines the format of the output csv
    def define_output(self, event_type, simulation, job):
        output_data = OrderedDict([('event_type', event_type),\
                                   ('time', simulation.current_time),\
                                   ('jobid', job.jobid),\
                                   ('nnodes', job.nnodes),\
                                   ('timelimit', job.timelimit),\
                                   ('elapsed', job.elapsed_time),\
                                   ('original_start', job.original_start),\
                                   ('IO', job.IO),\
                                   ('io_sens', job.io_sens),\
                                   ('goodput', job.good_put),\
                                   ('resubmit', job.resubmit),\
                                   ('rerun', job.rerun),\
                                   ('contention_event', bool(simulation.contention_event)),\
                                   ('sys_contention', bool(simulation.sys_contention))])
        return output_data

    # Simple function for writing simulation data to output
    def write_output(self, event_type, simulation, job):
        if self.output is None:
            return
        data = self.define_output(event_type, simulation, job)
        msg = ','.join([str(x) for x in data.values()])
        if not os.path.exists(self.output):
            header = ','.join([str(x) for x in data.keys()])
            msg = '\n'.join([header, msg])
        with open(self.output, "a") as f:
            f.write(msg + '\n')

    def update_makespan(self, current_time):
        if current_time < self.makespan.beginning:
            self.makespan = self.makespan._replace(beginning=current_time)
        if current_time > self.makespan.end:
            self.makespan = self.makespan._replace(end=current_time)

    def start_contention(self, simulation, contention):
        return None

    def end_contention(self, simulation, contention):
        return None

    def submit_job(self, simulation, job):
        self.update_makespan(simulation.current_time)
        self.write_output("submit", simulation, job)

    def start_job(self, simulation, job):
        self.num_free_nodes -= job.nnodes
        if self.num_free_nodes < 0:
            logger.error("Scheduler over-subscribed nodes")
        if (job.ncpus / job.nnodes) > self.cores_per_node:
            logger.error("Scheduler over-subscribed cores on the node")
        self.write_output("start", simulation, job)

    def complete_job(self, simulation, job):
        self.num_free_nodes += job.nnodes
        self.used_core_hours += (job.ncpus * job.elapsed_time) / 3600
        self.update_makespan(simulation.current_time)
        self.write_output("complete", simulation, job)

    def post_analysis(self, simulation):
        if self.makespan.beginning > self.makespan.end:
            logger.warn("Makespan beginning ({}) greater than end ({})".format(
                self.makespan.beginning,
                self.makespan.end,
            ))

        total_num_cores = self.num_nodes * self.cores_per_node
        print("Makespan (hours): {:.1f}".format((self.makespan.end - self.makespan.beginning) / 3600))
        total_core_hours = (total_num_cores * (self.makespan.end - self.makespan.beginning)) / 3600
        print("Total Core-Hours: {:,.1f}".format(total_core_hours))
        print("Used Core-Hours: {:,.1f}".format(self.used_core_hours))
        print("Average Core-Utilization: {:.2f}%".format((self.used_core_hours / total_core_hours) * 100))


logger = logging.getLogger("flux-simulator")


@flux.util.CLIMain(logger)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file")
    parser.add_argument("num_ranks", type=int)
    parser.add_argument("cores_per_rank", type=int)
    parser.add_argument("--output", "-o", type=str)
    parser.add_argument("--log-level", type=int)
    parser.add_argument("--oracle", action='store_true')
    parser.add_argument("--resub_chance", type=float, default=1.0)
    parser.add_argument("--canario_job_acc", type=float, default=1.0)
    parser.add_argument("--canario_con_acc", type=float, default=1.0)
    parser.add_argument("--prionn_job_acc", type=float, default=1.0)
    parser.add_argument("--contention", action='store_true', default=False)
    args = parser.parse_args()

    if args.log_level:
        logger.setLevel(args.log_level)

    flux_handle = flux.Flux()

    exec_validator = SimpleExec(args.num_ranks, args.cores_per_rank, args.output)
    if args.oracle:
        oracle = Oracle(PRIONN_accuracy=args.prionn_job_acc,\
                        CanarIO_job_accuracy=args.canario_job_acc,\
                        CanarIO_contention_accuracy=args.canario_con_acc)
    else:
        oracle = None

    simulation = Simulation(
        flux_handle,\
        EventList(),\
        {},\
        submit_job_hook=exec_validator.submit_job,\
        start_job_hook=exec_validator.start_job,\
        complete_job_hook=exec_validator.complete_job,\
        start_contention_hook=exec_validator.start_contention,\
        end_contention_hook=exec_validator.end_contention,\
        oracle=oracle,\
        resub_chance=args.resub_chance,\
        allow_contention=args.contention
    )
    reader = SacctReader(args.job_file)
    reader.validate_trace()
    jobs = list(reader.read_trace())
    for job in jobs:
        job.insert_apriori_events(simulation)

    C = [Contention(start_time=int((datetime(2020,1,1,1)-datetime(1970,1,1)).total_seconds()),\
                    end_time=int((datetime(2020,1,1,3)-datetime(1970,1,1)).total_seconds()),\
                    severity=(.8,1))]
    if args.contention:
        for c in C:
            c.insert_apriori_events(simulation)

    load_missing_modules(flux_handle)
    insert_resource_data(flux_handle, args.num_ranks, args.cores_per_rank)
    reload_scheduler(flux_handle)

    watchers, services = setup_watchers(flux_handle, simulation)
    exec_hello(flux_handle)
    simulation.advance()
    flux_handle.reactor_run(flux_handle.get_reactor(), 0)
    teardown_watchers(flux_handle, watchers, services)
    exec_validator.post_analysis(simulation)

if __name__ == "__main__":
    main()
