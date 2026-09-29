#!/usr/bin/env python3
"""Import T001-T099 from tasks.md into Paperclip as child issues of AUD-113."""

import os
import re
import json
import time
import urllib.request
import urllib.error

API_URL = os.environ["PAPERCLIP_API_URL"].rstrip("/").rstrip("/api")
API_KEY = os.environ["PAPERCLIP_API_KEY"]
COMPANY_ID = os.environ["PAPERCLIP_COMPANY_ID"]
PARENT_ID = os.environ["PAPERCLIP_TASK_ID"]  # AUD-113
PROJECT_ID = "d96ee0b3-e80e-4a82-89dc-674ce711f192"
RUN_ID = os.environ.get("PAPERCLIP_RUN_ID", "")

PHASES = {
    range(1, 9): ("Phase 1: Setup (Shared Infrastructure)", None),
    range(9, 21): ("Phase 2: Foundational (Blocking Prerequisites)", None),
    range(21, 47): ("Phase 3: User Story 1 — Wallet Holdings", "US1"),
    range(47, 64): ("Phase 4: User Story 2 — Value & Allocation", "US2"),
    range(64, 76): ("Phase 5: User Story 3 — Portfolio History", "US3"),
    range(76, 94): ("Phase 6: User Story 4 — Control & Recovery", "US4"),
    range(94, 100): ("Phase 7: Polish & Cross-Cutting Verification", None),
}

def get_phase(num):
    for r, info in PHASES.items():
        if num in r:
            return info
    return ("Unknown", None)

TASKS_MD = "/home/codex/git/audr/specs/001-ethereum-portfolio/tasks.md"

def parse_tasks(path):
    with open(path) as f:
        content = f.read()

    # Match task lines: - [ ] T### [optional tags] description
    pattern = re.compile(
        r'- \[ \] (T\d{3})((?:\s+\[P\])*(?:\s+\[US\d\])*)\s+(.*?)$',
        re.MULTILINE
    )
    tasks = []
    for m in pattern.finditer(content):
        task_id = m.group(1)
        tags = m.group(2).strip()
        desc = m.group(3).strip()
        num = int(task_id[1:])
        phase_name, story = get_phase(num)
        parallel = "[P]" in tags
        tasks.append({
            "id": task_id,
            "num": num,
            "description": desc,
            "phase": phase_name,
            "story": story,
            "parallel": parallel,
        })
    return tasks

def make_request(method, path, data=None):
    url = f"{API_URL}/api{path}"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if RUN_ID:
        headers["X-Paperclip-Run-Id"] = RUN_ID

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {error_body}")

def create_issue(task):
    parallel_note = " [P]" if task["parallel"] else ""
    story_note = f" [{task['story']}]" if task["story"] else ""
    title = f"{task['id']}{story_note}{parallel_note}: {task['description'][:120]}"

    description = f"**Phase**: {task['phase']}\n\n"
    if task["story"]:
        description += f"**User Story**: {task['story']}\n\n"
    if task["parallel"]:
        description += "**Parallel**: Can run in parallel with adjacent tasks after prerequisites.\n\n"
    description += f"**Full task description**:\n{task['description']}"

    payload = {
        "title": title,
        "description": description,
        "parentId": PARENT_ID,
        "projectId": PROJECT_ID,
        "status": "backlog",
        "priority": "medium",
    }
    return make_request("POST", f"/companies/{COMPANY_ID}/issues", payload)

def main():
    tasks = parse_tasks(TASKS_MD)
    print(f"Parsed {len(tasks)} tasks from tasks.md")

    results = []
    for i, task in enumerate(tasks):
        try:
            issue = create_issue(task)
            ident = issue.get("identifier", "?")
            print(f"[{i+1}/{len(tasks)}] Created {ident} for {task['id']}: {task['description'][:60]}")
            results.append({"task": task["id"], "identifier": ident, "id": issue["id"]})
        except Exception as e:
            print(f"[{i+1}/{len(tasks)}] ERROR for {task['id']}: {e}")
            results.append({"task": task["id"], "error": str(e)})
        # Small delay to avoid overwhelming the API
        time.sleep(0.1)

    # Summary
    succeeded = [r for r in results if "identifier" in r]
    failed = [r for r in results if "error" in r]
    print(f"\n=== SUMMARY ===")
    print(f"Created: {len(succeeded)}/{len(tasks)}")
    if failed:
        print(f"Failed: {len(failed)}")
        for f in failed:
            print(f"  {f['task']}: {f['error']}")

    return results

if __name__ == "__main__":
    main()
