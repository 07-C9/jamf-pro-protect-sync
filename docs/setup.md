# Setup

Everything here uses the AWS CLI. Console works too; the CLI version is exact and copy-pasteable. Replace the ALL_CAPS placeholders as you go.

Prerequisites: an AWS account, the AWS CLI configured, admin access to Jamf Pro and Jamf Protect.

## 1. Jamf Pro API client

Jamf Pro > Settings > System > API roles and clients.

1. Create an API role named `protect-sync` with exactly one privilege: **Read Computers**.
2. Create an API client named `protect-sync`, assign it that role, enable it.
3. Record the client ID and client secret. The secret is shown once.

The sync reads the computer list and nothing else. If a credential with only Read Computers leaks, the blast radius is your inventory list, not your MDM.

## 2. Jamf Protect API client

Jamf Protect > Administrative > API Clients.

1. Create an API client named `protect-sync`.
2. Record the client ID and password.

The sync needs to list computers and delete computers. Protect API client permissions are coarser than Jamf Pro's; scope the role down as far as your tenant allows. After setup, run `verify_privileges.py` to see what the client can actually reach.

## 3. Secrets Manager secret

```bash
aws secretsmanager create-secret \
  --name JamfSyncCredentials \
  --description "Jamf Pro and Jamf Protect API credentials for the Pro-to-Protect computer sync Lambda" \
  --secret-string '{
    "jamf_pro_url": "https://YOURORG.jamfcloud.com",
    "jamf_pro_client_id": "JAMF_PRO_CLIENT_ID",
    "jamf_pro_client_secret": "JAMF_PRO_CLIENT_SECRET",
    "jamf_protect_instance": "YOURINSTANCE",
    "jamf_protect_client_id": "PROTECT_CLIENT_ID",
    "jamf_protect_client_secret": "PROTECT_CLIENT_PASSWORD"
  }'
```

`jamf_pro_url` has no trailing slash. `jamf_protect_instance` is just the subdomain: if your Protect console is `acme.protect.jamfcloud.com`, the value is `acme`.

## 4. IAM role

Uses the two policy files in [iam/](../iam/): a trust policy that lets Lambda assume the role, and a permissions policy that grants CloudWatch Logs writes plus `GetSecretValue` on this one secret.

```bash
aws iam create-role \
  --role-name JamfSyncLambdaRole \
  --assume-role-policy-document file://iam/trust-policy.json \
  --description "Execution role for the Jamf Pro to Jamf Protect sync Lambda: log writes and read access to the JamfSyncCredentials secret only"

aws iam put-role-policy \
  --role-name JamfSyncLambdaRole \
  --policy-name JamfSyncPermissions \
  --policy-document file://iam/lambda-permissions.json
```

## 5. Build and create the Lambda

Build the deployment zip (bundles `requests`; `boto3` ships with the Lambda runtime):

```bash
mkdir package
pip3 install --target package requests
cp lambda_function.py package/
cd package && zip -r ../jamf-sync-lambda.zip . && cd ..
```

Create the function. Note `MAX_DELETIONS_ALLOWED=0`: your first run should be a dry run.

```bash
aws lambda create-function \
  --function-name JamfProProtectSync \
  --runtime python3.13 \
  --role arn:aws:iam::YOUR_ACCOUNT_ID:role/JamfSyncLambdaRole \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://jamf-sync-lambda.zip \
  --timeout 900 \
  --memory-size 256 \
  --environment "Variables={SECRET_NAME=JamfSyncCredentials,MAX_DELETIONS_ALLOWED=0,DIAGNOSTIC_MODE=false}" \
  --description "Syncs Jamf Pro inventory to Jamf Protect by deleting Protect computer records whose serial no longer exists in Jamf Pro. Daily via EventBridge. Credentials in Secrets Manager (JamfSyncCredentials)."
```

## 6. Dry run

```bash
aws lambda invoke --function-name JamfProProtectSync --payload '{}' response.json
cat response.json
aws logs tail /aws/lambda/JamfProProtectSync
```

Read the log. It shows both inventory counts, how many records would be deleted, and a preview of the first ten. With `MAX_DELETIONS_ALLOWED=0`, any pending deletion trips the safety failsafe and nothing is deleted.

If the counts look wrong, set `DIAGNOSTIC_MODE=true` and invoke again; it dumps the first raw record from each API so you can see field names and serial formats.

Sanity-check the preview against reality. Machines listed there should be ones you actually removed from Jamf Pro. If your first cleanup is large (years of accumulated ghosts), spot-check a handful in the Protect console before letting the deletion run.

## 7. First real run

Set the failsafe to your real cap and let it clean up the backlog. For the initial cleanup you may need a large cap once, then drop it to something close to your normal daily churn:

```bash
# initial cleanup (size to your backlog)
aws lambda update-function-configuration --function-name JamfProProtectSync \
  --environment "Variables={SECRET_NAME=JamfSyncCredentials,MAX_DELETIONS_ALLOWED=500,DIAGNOSTIC_MODE=false}"
aws lambda invoke --function-name JamfProProtectSync --payload '{}' response.json

# then back down for daily operation
aws lambda update-function-configuration --function-name JamfProProtectSync \
  --environment "Variables={SECRET_NAME=JamfSyncCredentials,MAX_DELETIONS_ALLOWED=50,DIAGNOSTIC_MODE=false}"
```

## 8. Schedule

Daily at 10:00 UTC (adjust the cron to taste):

```bash
aws events put-rule \
  --name JamfPro-Protect-Sync-Daily \
  --schedule-expression "cron(0 10 * * ? *)" \
  --description "Triggers the Jamf Pro to Jamf Protect computer sync Lambda once a day"

aws lambda add-permission \
  --function-name JamfProProtectSync \
  --statement-id AllowEventBridgeInvoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:YOUR_REGION:YOUR_ACCOUNT_ID:rule/JamfPro-Protect-Sync-Daily

aws events put-targets \
  --rule JamfPro-Protect-Sync-Daily \
  --targets "Id"="1","Arn"="arn:aws:lambda:YOUR_REGION:YOUR_ACCOUNT_ID:function:JamfProProtectSync"
```

Then verify the wiring end to end: wait for the next scheduled time and confirm a fresh log stream appears in `/aws/lambda/JamfProProtectSync`. Do not skip this. A rule that exists but is not attached to the function fails silently, and nothing tells you. Ours sat unattached for 20 days after initial deployment.

## 9. Log retention

```bash
aws logs put-retention-policy \
  --log-group-name /aws/lambda/JamfProProtectSync \
  --retention-in-days 180
```

The logs contain hostnames and serials. Default retention is forever.

## 10. Alerting

Do not run this without it. See [alerting.md](alerting.md).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_NAME` | `JamfSyncCredentials` | Secrets Manager secret holding both credential sets |
| `MAX_DELETIONS_ALLOWED` | `50` | Failsafe cap per run; `0` = dry run |
| `DIAGNOSTIC_MODE` | `false` | Dump first raw record from each API, log skipped serials |
