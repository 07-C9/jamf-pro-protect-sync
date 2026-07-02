# jamf-pro-protect-sync

AWS Lambda that deletes computer records from Jamf Protect when the computer no longer exists in Jamf Pro. Runs on a daily schedule. Jamf Pro is the source of truth.

Runs in production in a K-12 district, daily since November 2025.

## Why

Deleting a computer from Jamf Pro does not delete it from Jamf Protect. The Protect record sticks around until someone removes it in the console, one computer at a time. Retire a few hundred Macs and your Protect inventory is full of ghosts: the device counts never match, and you can't tell real coverage gaps from leftovers.

People have been asking for this sync in the MacAdmins #jamf-protect channel since 2020. Jamf folks said in mid-2025 that a native delete workflow is something they're exploring, but as of July 2026 nothing native exists. The community tools that do exist (linked below) are interactive utilities you run by hand. This is the automated version: schedule it and it keeps the two inventories matched without you thinking about it.

## How it works

Once a day, EventBridge invokes the Lambda. It:

1. Pulls every computer from Jamf Pro (Classic API, one request)
2. Pulls every computer from Jamf Protect (GraphQL, paginated)
3. Normalizes serial numbers on both sides (trim + uppercase)
4. Diffs them: any serial in Protect but not in Pro is an orphan
5. Deletes each orphan from Protect via the `deleteComputer` mutation

```
EventBridge rule (daily, cron)
  |
  v
Lambda (Python 3.13, ~430 lines)
  |
  +--- AWS Secrets Manager (Jamf Pro + Protect credentials)
  +--- POST {jamf-pro}/api/oauth/token (client credentials)
  +--- GET  {jamf-pro}/JSSResource/computers/subset/basic
  +--- POST {instance}.protect.jamfcloud.com/token
  +--- POST {instance}.protect.jamfcloud.com/graphql (listComputers, paginated)
  +--- For each orphan: graphql deleteComputer mutation
```

There is no state store. Every run re-derives the diff from both live inventories, so a failed or repeated run self-corrects on the next pass.

## Safety failsafe

`MAX_DELETIONS_ALLOWED` caps how many deletions a single run may perform. If the diff exceeds the cap, the run deletes nothing, logs `[SAFETY FAILSAFE]`, and exits. This is the guard against a bad day where Jamf Pro returns a truncated or empty computer list and the diff says "delete everything."

Two things to get right:

- Size it to your real churn, not your fleet. If you retire 5 machines on a normal day, a cap of 50 gives you headroom without letting a bad API response take out half your tenant. For planned mass offboarding (end of school year, big refresh), raise it temporarily, run, and put it back.
- `MAX_DELETIONS_ALLOWED=0` is a dry run. Any pending deletion trips the failsafe before anything is deleted, and the logs show the counts and a preview. Use this for your first run.

## What it does not do

- It does not uninstall the Protect agent. If a Mac is deleted from Jamf Pro but still alive and running Protect, it re-registers itself in Protect at its next check-in, gets deleted again at the next sync, and loops forever. We found machines that had been deleted dozens of times this way. The fix is process, not code: uninstall Protect (or wipe the Mac) as part of offboarding, before the Jamf Pro record goes away. The repeat offenders are worth chasing down anyway: each one is a device running outside MDM control.
- It does not touch Protect records that have no serial number. They can never match a Jamf Pro record, so the sync skips them. `discrepancy_report.py` lists them; delete them by hand.
- It does not install Protect on Macs that are missing it.
- Computers only. No mobile devices.

## Monitoring

The handler catches every exception and returns an error body instead of raising, which means the Lambda's built-in `Errors` metric stays at zero even when a sync fails. If you only alarm on `Errors`, you will never hear about a failure.

The alerting setup in [docs/alerting.md](docs/alerting.md) covers this with CloudWatch Logs metric filters on the log patterns (`[FATAL]`, `SAFETY FAILSAFE`, `Failed deletions:`) plus a dead-man alarm that fires if the function does not run for a day. The dead-man alarm earns its keep: our schedule silently didn't fire for the first 20 days after deployment, and nobody noticed for three weeks.

## Companion tools

- `discrepancy_report.py` - read-only drift report: pending orphans, Macs in Pro with no Protect record, serial-less Protect records, duplicate serials, stale Protect check-ins. Run it any time; it deletes nothing.
- `verify_privileges.py` - read-only check that your API clients hold least privilege. It probes endpoints the sync should NOT be able to reach and shows what your credentials can actually do.

Both use the same Secrets Manager secret as the Lambda.

## Security

- Credentials live in AWS Secrets Manager, fetched at runtime. Nothing sensitive in code, environment variables, or logs.
- The Jamf Pro API client needs one privilege: Read Computers. Nothing else.
- The Jamf Protect API client needs to list and delete computers. Scope its role down as far as your tenant allows, and run `verify_privileges.py` to see what it can really do.
- The IAM role grants CloudWatch Logs writes plus `GetSecretValue` on this one secret (see [iam/](iam/)).
- The Lambda accepts invocations only from its EventBridge rule.
- Deletion uses a GraphQL variable for the uuid, not string interpolation.
- Set a retention policy on the Lambda's log group. The logs include hostnames and serial numbers, and hostnames are often people's names.

## Setup

Full walkthrough in [docs/setup.md](docs/setup.md): API clients on both sides, the secret, IAM, the Lambda, the schedule, a dry run, and alerting.

## Costs

One short Lambda invocation a day (about 15 seconds at 256 MB) plus a Secrets Manager secret. Well inside the Lambda free tier; the secret costs $0.40/month. CloudWatch alarms are $0.10/month each.

## Related tools

- [Jamf-Protect-Batch-Delete](https://github.com/red5coder/Jamf-Protect-Batch-Delete) - GUI for bulk-deleting Protect records, run manually
- [Annie](https://apps.apple.com/gb/app/annie/id6753867307?mt=12) - Mac app that compares Pro and Protect enrollments and cleans up on click
- [jamf/jamfprotect](https://github.com/jamf/jamfprotect) - Jamf's own repo of Protect API script examples

## License

MIT
