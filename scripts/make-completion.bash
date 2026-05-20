#!/usr/bin/env bash
# §17.204 — bash completion for `make <TAB>` in the scaffold-engine
# repo. Reads the Makefile's target list dynamically so a new target
# (or a renamed one) is picked up on the next shell session without
# editing this file.
#
# Install (one-time, per dev host):
#
#   # Repo-local (recommended for developers who only want it in this
#   # repo's directory tree — sourced via direnv / per-dir hook):
#   source scripts/make-completion.bash
#
#   # User-global (~/.bashrc):
#   echo "source $(realpath scripts/make-completion.bash)" >> ~/.bashrc
#
# After install, `make st<TAB>` shows `status`, `status-raw`, etc.
# `make h<TAB>` shows `help`, `health`. See `make help` for the full
# annotated list (this completion lists targets only).
#
# Implementation note: the target list is computed from the Makefile in
# the directory the user is invoking `make` from — not from a frozen
# snapshot baked into this file. That keeps the completion in lockstep
# with the Makefile even as targets churn over time.

_scaffold_make_completion() {
    local cur targets
    cur="${COMP_WORDS[COMP_CWORD]}"

    # Only complete the target argument; pass through to default
    # completion for other forms (variables, -f Makefile, etc.).
    if [[ "$cur" == -* || "$cur" == *=* ]]; then
        return 0
    fi

    # Extract target names from the Makefile in the current directory.
    # Pattern: lines starting with a lowercase letter followed by
    # [a-zA-Z0-9_-]* and a colon (excludes ".PHONY:", indented recipes,
    # and rule-prereq lines that contain "=").
    if [[ -r Makefile ]]; then
        targets=$(grep -E '^[a-z][a-zA-Z0-9_-]*:' Makefile | sed 's/:.*//')
    else
        targets=""
    fi

    COMPREPLY=( $(compgen -W "$targets" -- "$cur") )
    return 0
}

# Bind to the `make` command. Operators with their own make-completion
# (eg from bash-completion-extras) should source THIS file last so it
# wins.
complete -F _scaffold_make_completion make
