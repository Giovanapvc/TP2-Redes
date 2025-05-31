#!/bin/sh
set -eu

exe="../../router.py"

tmux split-pane -v $exe 127.0.1.10 4 hub.txt &

for i in $(seq 1 5) ; do
    tmux split-pane -v $exe "127.0.1.$i" 4 spoke.txt &
    tmux select-layout even-vertical
done
