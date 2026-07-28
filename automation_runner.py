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
    print("mailboxes " + " ".join(f"{key}={value}" for key, value in mailbox_result.items() if key != "by_user"))
    connector_result = server.poll_connected_tools()
    print("connectors " + " ".join(f"{key}={value}" for key, value in connector_result.items() if key != "by_user"))
    notify_result = server.notify_new_message_arrivals(
        mailbox_result.get("by_user", {}), connector_result.get("by_user", {}))
    print("new-message emails " + " ".join(f"{key}={value}" for key, value in notify_result.items()))
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

    # Renewal notices. Safe to run on the same 5-minute cadence as everything
    # else: each company is stamped with the renewal date it was reminded
    # about, so this only actually emails once per billing cycle.
    for row in server.db.due_subscription_reminders():
        try:
            result = server.send_subscription_reminder(row)
            print(f"renewal reminder company={row['company_id']}: {result}")
        except Exception as exc:
            print(f"renewal reminder company={row['company_id']} failed unexpectedly: {exc}")

    # Client maintenance check-ins (e.g. a solar installer's 6-month/
    # quarterly/yearly follow-up). Same idempotent shape as the renewal
    # reminders above: each client is stamped with the due date it was
    # reminded about, so this only emails once per cycle.
    for row in server.db.due_maintenance_reminders():
        try:
            result = server.send_maintenance_reminder(row)
            print(f"maintenance reminder client={row['id']}: {result}")
        except Exception as exc:
            print(f"maintenance reminder client={row['id']} failed unexpectedly: {exc}")


if __name__ == "__main__":
    main()
