#!/bin/sh

test_description='Test flux job info service security'

. $(dirname $0)/sharness.sh

test_under_flux 4 job

# We have to fake a job submission by a guest into the KVS.
# This method of editing the eventlog preserves newline separators.

# Usage: submit_job [userid]
# To ensure robustness of tests despite future job manager changes,
# cancel the job, and wait for clean event.  Optionally, edit the
# userid
submit_job() {
        userid=$1
        jobid=$(flux job submit test.json)
        flux job cancel $jobid
        flux job wait-event $jobid clean >/dev/null
        if test -n "$userid"; then
            kvsdir=$(flux job id --to=kvs $jobid)
            flux kvs get --raw ${kvsdir}.eventlog \
                | sed -e 's/\("userid":\)[0-9]*/\1'${userid}/ \
                | flux kvs put --raw ${kvsdir}.eventlog=-
        fi
        echo $jobid
}

# Usage: bad_first_event
# Wait for job eventlog to include 'depend' event, then mangle submit event name
bad_first_event() {
        jobid=$(flux job submit test.json)
        flux job wait-event $jobid depend >/dev/null
        kvsdir=$(flux job id --to=kvs $jobid)
        flux kvs get --raw ${kvsdir}.eventlog \
            | sed -e s/submit/foobar/ \
            | flux kvs put --raw ${kvsdir}.eventlog=-
        echo $jobid
}

set_userid() {
        export FLUX_HANDLE_USERID=$1
        export FLUX_HANDLE_ROLEMASK=0x2
}

unset_userid() {
        unset FLUX_HANDLE_USERID
        unset FLUX_HANDLE_ROLEMASK
}

test_expect_success 'job-info: generate jobspec for simple test job' '
        flux jobspec --format json srun -N1 hostname > test.json
'

#
# job eventlog
#

test_expect_success 'flux job eventlog works (owner)' '
        jobid=$(submit_job) &&
        flux job eventlog $jobid
'

test_expect_success 'flux job eventlog works (user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9000 &&
        flux job eventlog $jobid &&
        unset_userid
'

test_expect_success 'flux job eventlog fails (wrong user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9999 &&
        ! flux job eventlog $jobid &&
        unset_userid
'

test_expect_success 'flux job eventlog fails on bad first event (user)' '
        jobid=$(bad_first_event 9000) &&
        set_userid 9999 &&
        ! flux job eventlog $jobid &&
        unset_userid
'

#
# job wait-event
#

test_expect_success 'flux job wait-event works (owner)' '
        jobid=$(submit_job) &&
        flux job wait-event $jobid submit
'

test_expect_success 'flux job wait-event works (user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9000 &&
        flux job wait-event $jobid submit &&
        unset_userid
'

test_expect_success 'flux job wait-event fails (wrong user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9999 &&
        ! flux job wait-event $jobid submit &&
        unset_userid
'

#
# job info
#

test_expect_success 'flux job info eventlog works (owner)' '
        jobid=$(submit_job) &&
        flux job info $jobid eventlog
'

test_expect_success 'flux job info eventlog works (user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9000 &&
        flux job info $jobid eventlog &&
        unset_userid
'

test_expect_success 'flux job info eventlog fails (wrong user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9999 &&
        ! flux job info $jobid eventlog &&
        unset_userid
'

test_expect_success 'flux job info jobspec works (owner)' '
        jobid=$(submit_job) &&
        flux job info $jobid jobspec
'

test_expect_success 'flux job info jobspec works (user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9000 &&
        flux job info $jobid jobspec &&
        unset_userid
'

test_expect_success 'flux job info jobspec fails (wrong user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9999 &&
        ! flux job info $jobid jobspec &&
        unset_userid
'

test_expect_success 'flux job info multiple keys works (owner, include eventlog)' '
        jobid=$(submit_job) &&
        flux job info $jobid eventlog jobspec J
'

test_expect_success 'flux job info multiple keys works (user, include eventlog)' '
        jobid=$(submit_job 9000) &&
        set_userid 9000 &&
        flux job info $jobid eventlog jobspec J &&
        unset_userid
'

test_expect_success 'flux job info multiple keys fails (wrong user, include eventlog)' '
        jobid=$(submit_job 9000) &&
        set_userid 9999 &&
        ! flux job info $jobid eventlog jobspec J &&
        unset_userid
'

test_expect_success 'flux job info multiple keys works (owner, no eventlog)' '
        jobid=$(submit_job) &&
        flux job info $jobid jobspec J
'

test_expect_success 'flux job info multiple keys works (user, no eventlog)' '
        jobid=$(submit_job 9000) &&
        set_userid 9000 &&
        flux job info $jobid jobspec J &&
        unset_userid
'

test_expect_success 'flux job info multiple keys fails (wrong user, no eventlog)' '
        jobid=$(submit_job 9000) &&
        set_userid 9999 &&
        ! flux job info $jobid jobspec J &&
        unset_userid
'

test_expect_success 'flux job info foobar fails (owner)' '
        jobid=$(submit_job) &&
        ! flux job info $jobid foobar
'

test_expect_success 'flux job info foobar fails (user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9000 &&
        ! flux job info $jobid foobar &&
        unset_userid
'

test_expect_success 'flux job info foobar fails (wrong user)' '
        jobid=$(submit_job 9000) &&
        set_userid 9999 &&
        ! flux job info $jobid foobar &&
        unset_userid
'

test_done
