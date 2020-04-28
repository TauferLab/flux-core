#!/bin/bash
#SBATCH --job-name=Flux-Sim-AI
#SBATCH --output=./data-collection.log
#SBATCH --nodes=1
#SBATCH --time=00:10:00
#SBATCH --partition=pdebug

cd ~/repos/flux-core
source /etc/profile
MAX_PP=16


OUTPUT=~/repos/Src_IO_Sched_Sim/data/sim-control.log
./src/cmd/flux start flux simulator\
               ~/repos/Src_IO_Sched_Sim/data/test_jobs.csv\
               16 16 --resub_chance 0\
               -o $OUTPUT &


for RESUB in 0 0.5 1
do
    OUTPUT=~/repos/Src_IO_Sched_Sim/data/sim-resub$RESUB.log
    if [ -f "$OUTPUT" ]
    then
        continue
    else
        ./src/cmd/flux start flux simulator\
                       ~/repos/Src_IO_Sched_Sim/data/test_jobs.csv\
                       16 16 --resub_chance $RESUB\
                       --contention\
                       -o $OUTPUT &
    fi
done

for RESUB in 0 0.5 1
do
    for PRIONN_ACC in 20 60 100
    do
        OUTPUT=~/repos/Src_IO_Sched_Sim/data/sim-oracle-resub$RESUB-prionn$PRIONN_ACC-canariojob0-canariocon0.log
        if [ -f "$OUTPUT" ]
        then
            continue
        else
            echo "RUNNING $PRIONN_ACC $CANARIO_JOB $CANARIO_CON"
            ./src/cmd/flux start flux simulator\
                       ~/repos/Src_IO_Sched_Sim/data/test_jobs.csv\
                       16 16 --oracle --resub_chance $RESUB\
                       --prionn --prionn_job_acc $PRIONN_ACC\
                       --contention\
                       -o $OUTPUT &
        fi
    done
done

for RESUB in 0 0.5 1
do
    for CANARIO_JOB in 20 60 100
    do
        for CANARIO_CON in 20 60 100
        do
            while [ $(ps -a | grep flux | wc -l) -ge $MAX_PP ]
            do
                sleep 10s
            done

            OUTPUT=~/repos/Src_IO_Sched_Sim/data/sim-oracle-resub$RESUB-prionn0-canariojob$CANARIO_JOB-canariocon$CANARIO_CON.log
            if [ -f "$OUTPUT" ]
            then
                continue
            else
                echo "RUNNING $PRIONN_ACC $CANARIO_JOB $CANARIO_CON"
                ./src/cmd/flux start flux simulator\
                           ~/repos/Src_IO_Sched_Sim/data/test_jobs.csv\
                           16 16 --oracle --resub_chance $RESUB\
                           --contention --canario\
                           --canario_job_acc $CANARIO_JOB\
                           --canario_con_acc $CANARIO_CON\
                           -o $OUTPUT &
            fi
        done
    done
done
for RESUB in 0 0.5 1
do
    for PRIONN_ACC in 20 60 100
    do
        for CANARIO_JOB in 20 60 100
        do
            for CANARIO_CON in 20 60 100
            do
                while [ $(ps -a | grep flux | wc -l) -ge $MAX_PP ]
                do
                    sleep 10s
                done

                OUTPUT=~/repos/Src_IO_Sched_Sim/data/sim-oracle-resub$RESUB-prionn$PRIONN_ACC-canariojob$CANARIO_JOB-canariocon$CANARIO_CON.log
                if [ -f "$OUTPUT" ]
                then
                    continue
                else
                    echo "RUNNING $PRIONN_ACC $CANARIO_JOB $CANARIO_CON"
                    ./src/cmd/flux start flux simulator\
                               ~/repos/Src_IO_Sched_Sim/data/test_jobs.csv\
                               16 16 --oracle --resub_chance $RESUB\
                               --contention --prionn --canario\
                               --prionn_job_acc $PRIONN_ACC\
                               --canario_job_acc $CANARIO_JOB\
                               --canario_con_acc $CANARIO_CON\
                               -o $OUTPUT &
                fi
            done
        done
    done
done

