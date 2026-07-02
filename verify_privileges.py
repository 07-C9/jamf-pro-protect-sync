# ABOUTME: Read-only privilege check for the sync's Jamf Pro and Protect API clients.
# ABOUTME: Confirms least privilege by probing endpoints the sync should NOT be able to reach.

import json
import os

import boto3
import requests

SECRET_NAME = os.environ.get("SECRET_NAME", "JamfSyncCredentials")

creds = json.loads(
    boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_NAME)["SecretString"]
)

base = creds["jamf_pro_url"]
tok = requests.post(
    f"{base}/api/oauth/token",
    data={
        "client_id": creds["jamf_pro_client_id"],
        "client_secret": creds["jamf_pro_client_secret"],
        "grant_type": "client_credentials",
    },
    timeout=30,
).json()["access_token"]
h = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}

print("=== Jamf Pro probes ===")
print("Expect 200 on the first two (the sync needs them) and 401/403 on everything else.")
pro_probes = [
    ("computers subset/basic (required)", "/JSSResource/computers/subset/basic"),
    ("computers-inventory (same privilege)", "/api/v1/computers-inventory?page-size=1"),
    ("accounts", "/JSSResource/accounts"),
    ("policies", "/JSSResource/policies"),
    ("computer groups", "/JSSResource/computergroups"),
    ("scripts", "/api/v1/scripts?page-size=1"),
    ("api integrations", "/api/v1/api-integrations?page-size=1"),
    ("mobile devices", "/JSSResource/mobiledevices"),
]
for label, path in pro_probes:
    r = requests.get(f"{base}{path}", headers=h, timeout=30)
    print(f"  {r.status_code}  {label}")

print("=== Jamf Protect probes ===")
print("Expect ALLOWED on listComputers. ALLOWED on the others means the client has")
print("more read access than the sync needs; scope its role down if your tenant allows.")
inst = creds["jamf_protect_instance"]
ptok = requests.post(
    f"https://{inst}.protect.jamfcloud.com/token",
    json={"client_id": creds["jamf_protect_client_id"], "password": creds["jamf_protect_client_secret"]},
    timeout=30,
).json()["access_token"]

protect_probes = [
    ("listComputers (required)", "query { listComputers(input:{pageSize:1}) { items { uuid } } }"),
    ("listAlerts", "query { listAlerts(input:{pageSize:1}) { items { uuid } } }"),
    ("listPlans", "query { listPlans(input:{pageSize:1}) { items { id } } }"),
    ("listUsers", "query { listUsers { items { email } } }"),
]
for label, q in protect_probes:
    r = requests.post(
        f"https://{inst}.protect.jamfcloud.com/graphql",
        json={"query": q},
        headers={"Authorization": ptok, "Content-Type": "application/json"},
        timeout=30,
    )
    body = r.json()
    if body.get("errors"):
        etype = body["errors"][0].get("errorType", "") or body["errors"][0].get("message", "")[:60]
        print(f"  DENIED   {label}  ({etype})")
    else:
        print(f"  ALLOWED  {label}")
