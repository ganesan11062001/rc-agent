# RC Copilot for RC team members

RC Copilot drafts replies to ServiceNow tickets with a private LLM in your
own Slurm job. Drafts are grounded in rc.northeastern.edu and rc-docs, with
links to the exact sections they rely on. You review every draft before it
is sent; nothing is submitted automatically.

## Install (once, ~1 minute)

```bash
/projects/rc/projects/ticketmate/rc-copilot/install.sh
```

This links `~/ondemand/dev/rc-copilot` to the shared app here. There is no
copy to maintain, and nothing in `/scratch` for the monthly purge to remove.
Updates reach you automatically on your next launch. If you already had an
`rc-copilot` sandbox app, it is moved aside to `rc-copilot.backup-<date>`,
not deleted.

The script then explains how to launch the app and install the browser
extension (`rc-copilot-extension.zip`, in this directory).

## Use

1. **Open OnDemand → Develop → My Sandbox Apps → RC Copilot → Launch.**
   The default, Qwen2.5-7B (4-bit) on any free GPU, starts in a few minutes;
   pick GLM-4.7-Flash with an 80 GB GPU in the form for stronger drafts.
2. Open **My Interactive Sessions**. The extension picks up the session
   automatically.
3. Open a ticket in ServiceNow and click **Draft reply**. The draft lands in
   work notes, unsaved. Check it, especially anything under "Check before
   sending", then edit and save it yourself.

## What is private, and to whom

| | where it lives | who can read it |
|---|---|---|
| ticket text, prompts, drafts | your Slurm job; logs in `/scratch/$USER/rc_copilot_runs` (mode 700) | you |
| session access token | the OOD session card (owner only) | you |
| model caches | `/scratch/$USER/rc_copilot_cache` | you |
| the app, models, knowledge graph | `/projects/rc/projects/...` | RC team, read-only |

Nothing leaves the cluster: the model and the knowledge-graph search both run
inside your job.

## Version and problems

- `cat /projects/rc/projects/ticketmate/rc-copilot/current/VERSION` shows the
  version you are running.
- The session's `output.log` (open it from the session card) shows startup.
  `rc_kg.log` in the same directory shows the knowledge graph.
- To stop using the shared app, run `install.sh --uninstall`.
