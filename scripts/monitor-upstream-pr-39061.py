#!/usr/bin/env python3
"""
Daily monitor of upstream go-gitea/gitea PR #39061 and issue #39060.

Workflow:
1. Fetch PR + issue state via gh CLI.
2. Locate or create a "log issue" in chudnyi/gitea with label `monitor-pr-39061-log`
   that carries the last-known state in a `<details>` JSON block.
3. Diff state vs. last-known.
4. On meaningful change → append a comment with a recommended plan.
5. Always update the body so the next run can compare.
6. On MERGED / CLOSED: append a "stop the monitor" hint (do NOT auto-disable
   the workflow — owner decision).

What it does NOT do:
- Push to upstream.
- Modify the upstream PR / issue.
- Force-push to the fork.
- Disable the workflow itself.
- Edit INFRA-360 in Huly (no Huly API access from CI).

State file (in the log issue body):
<details><summary>state</summary>
```json
{ ... }
```
</details>
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from typing import Any

UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "go-gitea/gitea")
UPSTREAM_PR_NUMBER = int(os.environ.get("UPSTREAM_PR_NUMBER", "39061"))
UPSTREAM_ISSUE_NUMBER = int(os.environ.get("UPSTREAM_ISSUE_NUMBER", "39060"))
LOG_ISSUE_LABEL = os.environ.get("LOG_ISSUE_LABEL", "monitor-pr-39061-log")
LOG_ISSUE_TITLE = "[monitor] upstream PR go-gitea/gitea#39061 (npm tarball PathEscape)"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def gh(*args: str, check: bool = True) -> str:
    """Run gh CLI in the upstream repo and return stdout."""
    result = subprocess.run(
        ["gh", *args, "--repo", UPSTREAM_REPO],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed: {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout


def gh_api(endpoint: str) -> Any:
    """gh api <endpoint> → returns parsed JSON. Endpoint must include full path (repos/owner/repo/...)."""
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {endpoint} failed: {result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def gh_issue_create(title: str, body: str, labels: list[str]) -> int:
    """Create issue in the upstream repo (actually fork, see run())."""
    cmd = [
        "gh", "issue", "create",
        "--title", title,
        "--body", body,
    ]
    for label in labels:
        cmd += ["--label", label]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue create failed: {result.stderr}")
    # stdout: https://github.com/owner/repo/issues/123
    return int(result.stdout.strip().split("/")[-1])


def gh_issue_edit(number: int, body: str) -> None:
    if DRY_RUN:
        print(f"[dry-run] would edit issue #{number}")
        return
    subprocess.run(
        ["gh", "issue", "edit", str(number), "--body", body],
        check=True,
        capture_output=True,
        text=True,
    )


def gh_issue_comment(number: int, body: str) -> None:
    if DRY_RUN:
        print(f"[dry-run] would comment on issue #{number}")
        return
    subprocess.run(
        ["gh", "issue", "comment", str(number), "--body", body],
        check=True,
        capture_output=True,
        text=True,
    )


def fetch_state() -> dict[str, Any]:
    """Collect current state from upstream."""
    pr = json.loads(gh(
        "pr", "view", str(UPSTREAM_PR_NUMBER),
        "--json",
        "state,isDraft,mergeable,reviewDecision,additions,deletions,changedFiles,maintainerCanModify,headRefName,baseRefName,url",
    ))
    issue = json.loads(gh(
        "issue", "view", str(UPSTREAM_ISSUE_NUMBER),
        "--json", "state,stateReason,labels",
    ))
    pr_comments = gh_api(f"repos/{UPSTREAM_REPO}/issues/{UPSTREAM_PR_NUMBER}/comments")
    issue_comments = gh_api(f"repos/{UPSTREAM_REPO}/issues/{UPSTREAM_ISSUE_NUMBER}/comments")
    reviews = gh_api(f"repos/{UPSTREAM_REPO}/pulls/{UPSTREAM_PR_NUMBER}/reviews")
    review_comments = gh_api(f"repos/{UPSTREAM_REPO}/pulls/{UPSTREAM_PR_NUMBER}/comments")

    # statusCheckRollup is fragile (nested unions); capture minimal info.
    checks_raw = gh_api(f"repos/{UPSTREAM_REPO}/commits/{pr['headRefName']}/check-runs?per_page=100")
    checks = []
    failed_jobs = []
    for run in checks_raw.get("check_runs", []):
        checks.append({
            "name": run.get("name"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "workflow": (run.get("check_suite") or {}).get("app", {}).get("name") if run.get("check_suite") else None,
        })
        if run.get("conclusion") in ("failure", "cancelled", "timed_out"):
            failed_jobs.append(run.get("name"))

    state = {
        "pr": {
            "state": pr.get("state"),
            "isDraft": pr.get("isDraft"),
            "mergeable": pr.get("mergeable"),
            "reviewDecision": pr.get("reviewDecision") or "",
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "changedFiles": pr.get("changedFiles"),
            "url": pr.get("url"),
            "headRefName": pr.get("headRefName"),
            "baseRefName": pr.get("baseRefName"),
        },
        "issue": {
            "state": issue.get("state"),
            "stateReason": issue.get("stateReason"),
            "labels": [l["name"] for l in (issue.get("labels") or [])],
        },
        "counts": {
            "pr_comments": len(pr_comments),
            "issue_comments": len(issue_comments),
            "reviews": len(reviews),
            "review_comments": len(review_comments),
            "checks_total": len(checks),
            "checks_failed": len(failed_jobs),
            "checks_failed_names": failed_jobs,
        },
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    state["hash"] = hashlib.sha256(
        json.dumps(state, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return state


def find_log_issue() -> int | None:
    """Find existing log issue by label."""
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--label", LOG_ISSUE_LABEL,
            "--state", "open",
            "--json", "number,title",
            "--limit", "1",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh issue list failed: {result.stderr}")
    issues = json.loads(result.stdout or "[]")
    if issues and issues[0]["title"] == LOG_ISSUE_TITLE:
        return issues[0]["number"]
    return None


def create_log_issue(initial_state: dict[str, Any]) -> int:
    """Create the log issue in the current repo (fork)."""
    body = render_issue_body(initial_state, plan="Bootstrap. First run.")
    return gh_issue_create(LOG_ISSUE_TITLE, body, [LOG_ISSUE_LABEL])


def extract_state_from_body(body: str) -> dict[str, Any] | None:
    """Parse last-known state from issue body."""
    if "<details><summary>state</summary>" not in body:
        return None
    try:
        block = body.split("<details><summary>state</summary>", 1)[1]
        json_part = block.split("```json", 1)[1].split("```", 1)[0]
        return json.loads(json_part)
    except Exception as e:
        print(f"warning: could not parse state block: {e}", file=sys.stderr)
        return None


def render_issue_body(state: dict[str, Any], plan: str) -> str:
    body = textwrap.dedent(f"""\
        # {LOG_ISSUE_TITLE}

        Автоматический ежедневный мониторинг upstream PR [#{UPSTREAM_PR_NUMBER}](https://github.com/{UPSTREAM_REPO}/pull/{UPSTREAM_PR_NUMBER})
        и issue [#{UPSTREAM_ISSUE_NUMBER}](https://github.com/{UPSTREAM_REPO}/issues/{UPSTREAM_ISSUE_NUMBER}).

        Workflow: `.github/workflows/monitor-upstream-pr-39061.yml`
        Cron: `0 7 * * 1-5` UTC (10:00 Europe/Moscow, Mon–Fri).
        Huly-задача-зеркало: [INFRA-360](https://task.it3.su/workbench/it3/tracker/INFRA-360).

        ## Текущий план / последнее наблюдение

        {plan}

        <details><summary>state</summary>

        ```json
        {json.dumps(state, indent=2, ensure_ascii=False, default=str)}
        ```

        </details>
    """)
    return body


def plan_for_changes(old: dict[str, Any] | None, new: dict[str, Any]) -> str | None:
    """Build a plan string if anything meaningful changed. None otherwise."""
    if old is None:
        return None

    changes: list[str] = []

    pr_old, pr_new = old["pr"], new["pr"]
    issue_old, issue_new = old["issue"], new["issue"]
    counts_old, counts_new = old["counts"], new["counts"]

    # PR lifecycle
    if pr_old["state"] != pr_new["state"]:
        if pr_new["state"] == "MERGED":
            changes.append("✅ **PR смержен в upstream main.** Код-фикс появится в следующем релизе Gitea.")
            changes.append("- [ ] Проверить релиз: `gh release list --repo go-gitea/gitea --limit 5`")
            changes.append("- [ ] Наш локальный патч RTGCH-127 можно удалить после выхода соответствующей версии")
            changes.append("- [ ] Обновить `Taskfile.yml` (`VERSION:`) и `README.md` (упоминания 1.27.x → новой)")
            changes.append("- [ ] Рекомендуется остановить workflow: `gh workflow disable monitor-upstream-pr-39061`")
        elif pr_new["state"] == "CLOSED":
            reason = issue_new.get("stateReason") or ""
            changes.append(f"❌ **PR закрыт без merge** ({reason}).")
            changes.append("- [ ] Решить: открыть новый PR с пересмотренным фиксом или оставить локальный патч")
            changes.append("- [ ] Рекомендуется остановить workflow")
    else:
        if pr_old["mergeable"] == "MERGEABLE" and pr_new["mergeable"] == "CONFLICTING":
            changes.append("⚠️ **Появились конфликты с upstream main.**")
            changes.append("План:")
            changes.append("```")
            changes.append("cd .temp/gitea-1.27.2")
            changes.append("git fetch upstream main")
            changes.append("git rebase upstream/main")
            changes.append("go test ./routers/api/packages/npm/")
            changes.append("git push origin fix/npm-tarball-path-escape --force-with-lease")
            changes.append("```")
        elif pr_old["mergeable"] == "CONFLICTING" and pr_new["mergeable"] == "MERGEABLE":
            changes.append("✅ **Конфликт разрешён** (вероятно, после rebase upstream).")

        if pr_old["reviewDecision"] != pr_new["reviewDecision"]:
            if pr_new["reviewDecision"] == "APPROVED":
                changes.append("👍 **Получен approval.** Ждём merge от maintainer'ов.")
            elif pr_new["reviewDecision"] == "CHANGES_REQUESTED":
                changes.append("🔧 **Запрошены изменения.** План: прочитать review-комментарии, подготовить правки.")
                changes.append("- [ ] Прочитать: `gh api /repos/go-gitea/gitea/pulls/39061/comments`")

    # Issue lifecycle
    if issue_old["state"] != issue_new["state"]:
        changes.append(f"ℹ️ **Issue сменил статус**: `{issue_old['state']}` → `{issue_new['state']}`")

    # CI failures
    failed_new = set(counts_new["checks_failed_names"])
    failed_old = set(counts_old["checks_failed_names"])
    new_failures = failed_new - failed_old
    recovered = failed_old - failed_new
    if new_failures:
        changes.append(f"❌ **Новые упавшие CI jobs**: {', '.join(sorted(new_failures))}")
        changes.append("- [ ] Детали: `gh pr checks 39061 --repo go-gitea/gitea`")
        changes.append("- [ ] Возможные причины: flake (перезапустить), реальный fail (исправить)")
    if recovered:
        changes.append(f"✅ **Восстановились CI jobs**: {', '.join(sorted(recovered))}")

    # Comments / reviews
    if counts_new["pr_comments"] > counts_old["pr_comments"]:
        changes.append(f"💬 Новые issue/PR комментарии на PR: {counts_old['pr_comments']} → {counts_new['pr_comments']}")
    if counts_new["review_comments"] > counts_old["review_comments"]:
        changes.append(f"📝 Новые inline review comments: {counts_old['review_comments']} → {counts_new['review_comments']}")
        changes.append("- [ ] Прочитать: `gh api /repos/go-gitea/gitea/pulls/39061/comments`")
    if counts_new["issue_comments"] > counts_old["issue_comments"]:
        changes.append(f"💬 Новые комментарии в issue: {counts_old['issue_comments']} → {counts_new['issue_comments']}")
    if counts_new["reviews"] > counts_old["reviews"]:
        changes.append(f"🔍 Новые reviews: {counts_old['reviews']} → {counts_new['reviews']}")

    # Labels
    if set(issue_old["labels"]) != set(issue_new["labels"]):
        added = set(issue_new["labels"]) - set(issue_old["labels"])
        removed = set(issue_old["labels"]) - set(issue_new["labels"])
        if added:
            changes.append(f"🏷️ Добавлены labels в issue: {', '.join(sorted(added))}")
        if removed:
            changes.append(f"🏷️ Удалены labels из issue: {', '.join(sorted(removed))}")

    if not changes:
        return None

    return "\n".join(changes)


def main() -> int:
    print(f"=== monitor-upstream-pr-{UPSTREAM_PR_NUMBER} ===")
    print(f"upstream: {UPSTREAM_REPO}")
    print(f"dry-run: {DRY_RUN}")

    new_state = fetch_state()
    print(f"PR state: {new_state['pr']['state']}, mergeable: {new_state['pr']['mergeable']}")
    print(f"Issue state: {new_state['issue']['state']}")
    print(f"State hash: {new_state['hash']}")

    log_issue = find_log_issue()
    old_state = None
    if log_issue is not None:
        body = subprocess.run(
            ["gh", "issue", "view", str(log_issue), "--json", "body", "--jq", ".body"],
            capture_output=True, text=True, check=True,
        ).stdout
        old_state = extract_state_from_body(body)
        print(f"log issue: #{log_issue}")
        print(f"old state hash: {old_state.get('hash') if old_state else 'n/a'}")
    else:
        print("log issue not found, will create")

    plan = plan_for_changes(old_state, new_state)

    if log_issue is None:
        log_issue = create_log_issue(new_state)
        print(f"created log issue #{log_issue}")
        if plan:
            gh_issue_comment(log_issue, render_comment(plan, new_state))
            print("posted initial plan comment")
    else:
        # Always update body with current state.
        gh_issue_edit(log_issue, render_issue_body(new_state, plan or "_Ничего не изменилось с прошлой проверки._"))
        if plan:
            gh_issue_comment(log_issue, render_comment(plan, new_state))
            print(f"posted plan comment with {len(plan.splitlines())} lines")
        else:
            print("no meaningful changes; silent skip")

    return 0


def render_comment(plan: str, state: dict[str, Any]) -> str:
    return textwrap.dedent(f"""\
        ### 📊 Изменения в upstream ({state["fetched_at"]})

        PR state: `{state['pr']['state']}` · mergeable: `{state['pr']['mergeable']}` · review: `{state['pr']['reviewDecision'] or '—'}`

        План действий:

        {plan}

        ---
        _state hash: `{state['hash']}` · _no auto-actions: push/force-push/amend отключены_
    """)


if __name__ == "__main__":
    sys.exit(main())
