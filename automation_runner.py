"""Run due read-only mailbox and connector automations from cPanel Cron Jobs.

Recommended cadence: every 5 minutes. The database advances each recipe's
next_run_at before network work begins, which keeps an ordinary single cPanel
cron entry from selecting the same job again while it is running.
"""

from __future__ import annotations

import server


def main() -> None:
    server.db.init_db()
    mailbox_result = server.poll_connected_mailboxes()
    print("mailboxes " + " ".join(f"{key}={value}" for key, value in mailbox_result.items()))
    connector_result = server.poll_connected_tools()
    print("connectors " + " ".join(f"{key}={value}" for key, value in connector_result.items()))
    due = server.db.due_automations()
    for job in due:
        key = job["recipe_key"]
        run_id = server.db.start_automation_run(
            job["user_id"], job["company_id"], key,
            server.next_automation_run(key),
        )
        try:
            result = server.run_automation(job["user_id"], key)
            server.db.finish_automation_run(run_id, result=result)
            print(f"complete user={job['user_id']} recipe={key}")
        except Exception as exc:
            server.db.finish_automation_run(run_id, error=str(exc))
            print(f"failed user={job['user_id']} recipe={key}: {exc}")

    for job in server.db.due_reminders():
        try:
            result = server.send_due_reminder(job)
            print(f"reminder id={job['id']} user={job['created_by']}: {result}")
        except Exception as exc:
            print(f"reminder id={job['id']} failed unexpectedly: {exc}")


if __name__ == "__main__":
    main()
