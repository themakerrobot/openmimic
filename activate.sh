#!/usr/bin/env bash
source "$HOME/miniforge3/etc/profile.d/conda.sh"
conda activate openmimic
export HF_HOME="$HOME/project/openmimic/data/hf"
export TORCH_HOME="$HOME/project/openmimic/data/torch"
cd "$HOME/project/openmimic"
