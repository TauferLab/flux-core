#!/bin/bash
#SBATCH --job-name=Flux-Sim-AI
#SBATCH --output=./data-collection.log
#SBATCH --nodes=1
#SBATCH --time=00:10:00
#SBATCH --partition=pbatch

MAX_PP=16

for RESUB in 0 0.5 1
do
    ./src/cmd/flux start flux simulator\
                   ~/repos/Src_IO_Sched_Sim/data/test_jobs.csv\
                   16 16 --resub_chance $RESUB\
                   -o ~/repos/Src_IO_Sched_Sim/data/sim-resub$RESUB.log
done

for RESUB in 0 0.5 1
do
    for PRIONN_ACC in 20 60 100
    do
        for CANARIO_JOB in 20 60 100
        do
            for CANARIO_CON in 20 60 100
            do
                echo "RUNNING $PRIONN_ACC $CANARIO_JOB $CANARIO_CON"
                while [ $(ps -a | grep flux | wc -l) -ge $MAX_PP ]
                do
                    sleep 300s
                done
                ./src/cmd/flux start flux simulator\
                               ~/repos/Src_IO_Sched_Sim/data/test_jobs.csv\
                               16 16 --oracle --resub_chance $RESUB\
                               --prionn_job_acc $PRIONN_ACC\
                               --canario_job_acc $CANARIO_JOB\
                               --canario_con_acc $CANARIO_CON\
                               -o ~/repos/Src_IO_Sched_Sim/data/sim-oracle-resub$RESUB-prionn$PRIONN_ACC-canariojob$CANARIO_JOB-canariocon$CANARIO_CON.log &
            done
        done
    done
done

