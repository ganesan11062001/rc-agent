---
# An example playbook: the format, with a made-up procedure. RC's own
# playbooks are internal and are kept outside the repository; set
# PLAYBOOK_DIR to their directory (cluster/env.sh does, on Explorer).
name: example-procedure
title: Example -- access to a restricted software group
# Every `match` line must match the researcher's words; no `unless` line may.
match: (?i)\bexample-software\b
match: (?i)\b(access|add(ed)?|join|group)\b
unless: (?i)\binstall(ation)?\b
# The agent this kind of ticket belongs to (rc-agent): incident | request.
route: request
# Values pulled out of the whole cleaned ticket (group 1, or the whole match).
field.username: (?<![\w@./-])([a-z][a-z0-9-]*\.[a-z][a-z0-9-]*)(?![\w@-]|\.[a-z0-9])
field.ticket: (?m)^Ticket:\s*(\S+)
# What RC needs before it can act; any one field satisfies it; the text is what the reply asks for.
require.username: the cluster username of each person to add
---
Access to the example-software group is granted by RC after the software
owner approves. (The runbook body is free text: stages, how to tell from the
RC staff entries which stage the ticket is at, the commands for the RC
engineer, and the reply for each stage.)

Stages -- read the RC staff entries to see which one the ticket is at:

1. NEW REQUEST. Steps for the RC engineer: check the account
   (`id <username>`), ask the owner for approval.
   Reply: RC has asked the software owner for approval and will confirm.

2. APPROVED (an RC staff entry says so). Steps: add the user to the group.
   Reply: say they were added only once an RC staff entry or a lookup shows it.
