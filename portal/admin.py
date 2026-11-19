"""Admin tasks on Azure (a Container Apps job runs `python -m portal.admin <task>`); on AWS the same tasks are
Lambda invocations (see lambda_handler.py). Only these read-only or schema tasks are offered here:
  migrate     apply pending schema migrations as platform_migrator, then (re)grant platform_app its rights
  status      applied and pending migrations
  list_ndas   NDAs on record
Exits non-zero if the task reports an error, so the job run shows as failed."""
import json, sys
from portal import lambda_handler as h

TASKS = {"migrate": h.migrate, "status": h.status, "list_ndas": lambda: h.handler({"admin_task": "list_ndas"}, None)}

if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    if task not in TASKS:
        print(f"Usage: python -m portal.admin {'|'.join(TASKS)}", file=sys.stderr); sys.exit(2)
    out = TASKS[task]()
    print(json.dumps(out, default=str))
    sys.exit(1 if isinstance(out, dict) and out.get("error") else 0)
