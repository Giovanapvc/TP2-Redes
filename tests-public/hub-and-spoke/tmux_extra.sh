#!/bin/sh
exe="../../router.py"

tmux split-pane -v $exe 127.0.1.10 4 hub_extra.txt &
tmux split-pane -v $exe 127.0.1.3  4 dst.txt &
tmux split-pane -v $exe 127.0.1.11 4 spoke-B.txt &
tmux split-pane -v $exe 127.0.1.1  4 spoke-A.txt &
tmux select-layout even-vertical
