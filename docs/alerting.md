# Alerting

The sync fails in ways the default Lambda metrics cannot see. The handler catches every exception and returns an error body instead of raising, so a run that failed completely still counts as a clean invocation. `Errors` stays at zero forever. And a schedule that quietly stops firing produces no metric at all.

So the monitoring has three layers:

1. **Log-pattern alarms** for in-code failures (the handler logs `[FATAL]`, `SAFETY FAILSAFE`, or `Failed deletions:` on every failure path)
2. **Lambda `Errors` alarm** for what the handler cannot catch: timeouts, out-of-memory, init crashes
3. **Dead-man alarm** that fires when the function has not run for a day

All commands below assume the function name `JamfProProtectSync`. Replace `YOUR_REGION`, `YOUR_ACCOUNT_ID`, and the email address.

## SNS topic

```bash
aws sns create-topic --name jamf-protect-sync-alerts \
  --attributes DisplayName="Jamf Protect Sync Alerts"

aws sns subscribe \
  --topic-arn arn:aws:sns:YOUR_REGION:YOUR_ACCOUNT_ID:jamf-protect-sync-alerts \
  --protocol email \
  --notification-endpoint alerts@example.com
```

The address gets a confirmation email. Nothing is delivered until someone clicks the link, and AWS does not warn you about unconfirmed subscriptions. Confirm it now, then verify:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:YOUR_REGION:YOUR_ACCOUNT_ID:jamf-protect-sync-alerts
```

`"SubscriptionArn": "PendingConfirmation"` means not confirmed. A real ARN means done. Route to a distribution list or group, not one person's inbox.

## Metric filters

These turn the failure log lines into metrics:

```bash
LG="/aws/lambda/JamfProProtectSync"

aws logs put-metric-filter --log-group-name "$LG" \
  --filter-name jamf-protect-sync-fatal --filter-pattern '"[FATAL]"' \
  --metric-transformations metricName=FatalErrors,metricNamespace=JamfProtectSync,metricValue=1

aws logs put-metric-filter --log-group-name "$LG" \
  --filter-name jamf-protect-sync-failsafe --filter-pattern '"SAFETY FAILSAFE"' \
  --metric-transformations metricName=FailsafeTriggers,metricNamespace=JamfProtectSync,metricValue=1

aws logs put-metric-filter --log-group-name "$LG" \
  --filter-name jamf-protect-sync-failed-deletions --filter-pattern '"Failed deletions:"' \
  --metric-transformations metricName=FailedDeletions,metricNamespace=JamfProtectSync,metricValue=1
```

Prove the patterns match before trusting them:

```bash
aws logs test-metric-filter --filter-pattern '"[FATAL]"' --log-event-messages \
  "[FATAL] Fatal error during sync: connection timeout" \
  "[COMPLETE] Sync finished"
```

Expect exactly one match. Repeat for the other two patterns.

## Alarms

```bash
TOPIC="arn:aws:sns:YOUR_REGION:YOUR_ACCOUNT_ID:jamf-protect-sync-alerts"
FN="JamfProProtectSync"

# in-code fatal error
aws cloudwatch put-metric-alarm --alarm-name jamf-protect-sync-fatal-errors \
  --alarm-description "Jamf Pro to Protect sync hit a fatal error. The handler swallows exceptions, so this log-pattern alarm is the only signal for in-code failures. Check /aws/lambda/$FN logs." \
  --namespace JamfProtectSync --metric-name FatalErrors --statistic Sum --period 300 \
  --evaluation-periods 1 --threshold 0 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --alarm-actions "$TOPIC"

# failsafe tripped
aws cloudwatch put-metric-alarm --alarm-name jamf-protect-sync-failsafe-triggered \
  --alarm-description "Sync wanted to delete more than MAX_DELETIONS_ALLOWED computers and refused to run. Usually a truncated or empty Jamf Pro inventory response. Review logs before raising the cap." \
  --namespace JamfProtectSync --metric-name FailsafeTriggers --statistic Sum --period 300 \
  --evaluation-periods 1 --threshold 0 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --alarm-actions "$TOPIC"

# some deletions failed
aws cloudwatch put-metric-alarm --alarm-name jamf-protect-sync-failed-deletions \
  --alarm-description "Sync completed but one or more Protect delete mutations failed; orphaned records remain. Check logs for the failing serials." \
  --namespace JamfProtectSync --metric-name FailedDeletions --statistic Sum --period 300 \
  --evaluation-periods 1 --threshold 0 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --alarm-actions "$TOPIC"

# lambda-level errors the handler cannot catch
aws cloudwatch put-metric-alarm --alarm-name jamf-protect-sync-errors \
  --alarm-description "Lambda-level errors (timeout, OOM, init crash) for the Jamf Pro to Protect sync. In-code exceptions are covered by jamf-protect-sync-fatal-errors instead." \
  --namespace AWS/Lambda --metric-name Errors --dimensions Name=FunctionName,Value=$FN \
  --statistic Sum --period 300 --evaluation-periods 1 --threshold 0 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --alarm-actions "$TOPIC"

# dead-man: no run in a day
aws cloudwatch put-metric-alarm --alarm-name jamf-protect-sync-no-invocations \
  --alarm-description "Dead-man switch: the sync Lambda did not run in a full day. Check that the EventBridge rule exists, is enabled, and is attached to the function." \
  --namespace AWS/Lambda --metric-name Invocations --dimensions Name=FunctionName,Value=$FN \
  --statistic Sum --period 86400 --evaluation-periods 1 --threshold 0 \
  --comparison-operator LessThanOrEqualToThreshold \
  --treat-missing-data breaching --alarm-actions "$TOPIC"

# runtime creeping toward the timeout
aws cloudwatch put-metric-alarm --alarm-name jamf-protect-sync-duration \
  --alarm-description "Sync runtime exceeded 10 of its 15 minutes. Normal runs take seconds; long runs mean API slowness or a huge deletion batch." \
  --namespace AWS/Lambda --metric-name Duration --dimensions Name=FunctionName,Value=$FN \
  --statistic Maximum --period 300 --evaluation-periods 1 --threshold 600000 \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --alarm-actions "$TOPIC"
```

The dead-man alarm is the one that matters most. A failed run at least leaves a log; a schedule that never fires leaves nothing. Ours went silent for 20 days once (rule existed, was never attached to the function) and we only caught it because someone went looking. `treat-missing-data breaching` is what makes the alarm fire on total silence.

## End-to-end test

After the subscription is confirmed, force an alarm through the pipeline and confirm the email lands:

```bash
aws cloudwatch set-alarm-state --alarm-name jamf-protect-sync-fatal-errors \
  --state-value ALARM --state-reason "end-to-end notification test"
```

The alarm resets itself at the next evaluation. If no email arrives, the subscription is not confirmed or the alarm has no action attached; `aws cloudwatch describe-alarms --alarm-name-prefix jamf-protect-sync` shows both.

New alarms sit in `INSUFFICIENT_DATA` until their metric produces a datapoint. That is normal. The `notBreaching` alarms go `OK` as evaluation periods pass; the dead-man alarm goes `OK` after the next scheduled run.
