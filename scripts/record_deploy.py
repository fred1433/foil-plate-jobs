"""Record a merge-triggered deployment from its GitHub Actions run.

    PROJECT=<gcp project> python scripts/record_deploy.py <run id> <pr number or -> [evidence name]
"""
import json
import re
import subprocess
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
import lab  # noqa: E402

REPO = "fred1433/foil-plate-jobs"
run_id, pr = sys.argv[1], sys.argv[2]
name = sys.argv[3] if len(sys.argv) > 3 else "merge-deploy"
gh = lambda *a: subprocess.run(["gh", *a], capture_output=True, text=True, check=True).stdout  # noqa: E731
run = json.loads(gh("run", "view", run_id, "-R", REPO, "--json",
                    "url,event,headSha,headBranch,createdAt,updatedAt,conclusion,jobs"))
prj = json.loads(gh("pr", "view", pr, "-R", REPO, "--json", "url,mergedAt,mergeCommit,title")) if pr != "-" else None
log = gh("run", "view", run_id, "-R", REPO, "--log")
serving = [dict(zip(("service", "revision", "image"), m))
           for m in re.findall(r"SERVING service=(\S+) revision=(\S+) image=(\S+)", log)
           if not m[1].startswith("$")]
tests = re.findall(r"(\d+ passed[^\n]*)", log)
lab.save(name, {
    "scenario": name, "recorded_at": lab.now(),
    "pull_request": prj, "workflow_run": {k: run[k] for k in ("url", "event", "headSha", "headBranch",
                                                               "createdAt", "updatedAt", "conclusion")},
    "steps": [{"name": s["name"], "conclusion": s["conclusion"]} for j in run["jobs"] for s in j["steps"]],
    "tests_in_image": tests[:1], "serving_after_deploy": serving,
    "context_now": lab.deployment_context(__import__("os").environ["PROJECT"]),
})
