# ABOUTME: Read-only discrepancy report between Jamf Pro and Jamf Protect computer inventories.
# ABOUTME: Fetches both inventories with the sync's own credentials and reports drift; never deletes, never prints credentials.

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import boto3
import requests

SECRET_NAME = os.environ.get("SECRET_NAME", "JamfSyncCredentials")
PAGE_SIZE = 100

LIST_COMPUTERS_QUERY = """
query listComputers($page_size: Int, $next: String) {
  listComputers(input: { pageSize: $page_size, next: $next }) {
    items {
      uuid
      hostName
      serial
      checkin
    }
    pageInfo {
      next
    }
  }
}
"""


def normalize_serial(serial):
    return (serial or "").strip().upper()


def parse_checkin(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_report(pro_computers, protect_computers, now_iso, stale_days=30):
    """Compare inventories and return a dict of discrepancy lists."""
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    stale_cutoff = now - timedelta(days=stale_days)

    pro_serials = {normalize_serial(c.get("serial_number")) for c in pro_computers}
    pro_serials.discard("")

    protect_by_serial = {}
    protect_missing_serial = []
    protect_duplicate_serials = {}
    for computer in protect_computers:
        serial = normalize_serial(computer.get("serial"))
        if not serial:
            protect_missing_serial.append(computer)
            continue
        if serial in protect_by_serial:
            protect_duplicate_serials.setdefault(
                serial, [protect_by_serial[serial]]
            ).append(computer)
        else:
            protect_by_serial[serial] = computer

    protect_not_in_pro = [
        c for serial, c in sorted(protect_by_serial.items()) if serial not in pro_serials
    ]
    pro_not_in_protect = [
        c
        for c in pro_computers
        if normalize_serial(c.get("serial_number"))
        and normalize_serial(c.get("serial_number")) not in protect_by_serial
    ]
    protect_stale_checkin = []
    for serial, computer in sorted(protect_by_serial.items()):
        if serial not in pro_serials:
            continue
        checkin = parse_checkin(computer.get("checkin"))
        if checkin is not None and checkin < stale_cutoff:
            protect_stale_checkin.append(computer)

    pro_unmanaged = [c for c in pro_computers if not c.get("managed", True)]

    return {
        "pro_total": len(pro_computers),
        "protect_total": len(protect_computers),
        "protect_not_in_pro": protect_not_in_pro,
        "pro_not_in_protect": pro_not_in_protect,
        "protect_missing_serial": protect_missing_serial,
        "protect_duplicate_serials": protect_duplicate_serials,
        "protect_stale_checkin": protect_stale_checkin,
        "pro_unmanaged": pro_unmanaged,
    }


def get_credentials():
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=SECRET_NAME)
    return json.loads(response["SecretString"])


def get_jamf_pro_computers(base_url, client_id, client_secret):
    token_resp = requests.post(
        f"{base_url}/api/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token_resp.raise_for_status()
    token = token_resp.json()["access_token"]

    resp = requests.get(
        f"{base_url}/JSSResource/computers/subset/basic",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json().get("computers", [])


def get_protect_computers(instance, client_id, client_secret):
    token_resp = requests.post(
        f"https://{instance}.protect.jamfcloud.com/token",
        json={"client_id": client_id, "password": client_secret},
    )
    token_resp.raise_for_status()
    token = token_resp.json()["access_token"]

    computers = []
    next_token = None
    while True:
        resp = requests.post(
            f"https://{instance}.protect.jamfcloud.com/graphql",
            json={
                "query": LIST_COMPUTERS_QUERY,
                "variables": {"page_size": PAGE_SIZE, "next": next_token},
            },
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Protect GraphQL errors: {body['errors']}")
        listing = body["data"]["listComputers"]
        computers.extend(listing["items"])
        next_token = listing["pageInfo"]["next"]
        if next_token is None:
            return computers


def summarize(items, keys, limit):
    lines = []
    for item in items[:limit]:
        lines.append("  - " + ", ".join(f"{k}={item.get(k)}" for k in keys))
    if len(items) > limit:
        lines.append(f"  ... and {len(items) - limit} more")
    return "\n".join(lines) if lines else "  (none)"


def main():
    parser = argparse.ArgumentParser(description="Read-only Jamf Pro vs Protect drift report")
    parser.add_argument("--stale-days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=10, help="max examples per section")
    args = parser.parse_args()

    creds = get_credentials()
    print("Fetching Jamf Pro inventory...", file=sys.stderr)
    pro_computers = get_jamf_pro_computers(
        creds["jamf_pro_url"], creds["jamf_pro_client_id"], creds["jamf_pro_client_secret"]
    )
    print("Fetching Jamf Protect inventory...", file=sys.stderr)
    protect_computers = get_protect_computers(
        creds["jamf_protect_instance"],
        creds["jamf_protect_client_id"],
        creds["jamf_protect_client_secret"],
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    report = build_report(pro_computers, protect_computers, now_iso, args.stale_days)

    print(f"Jamf Pro computers:              {report['pro_total']}")
    print(f"Jamf Protect computers:          {report['protect_total']}")
    print()
    print(f"In Protect but NOT in Pro ({len(report['protect_not_in_pro'])}) - sync will delete these at next run:")
    print(summarize(report["protect_not_in_pro"], ["serial", "hostName", "checkin"], args.limit))
    print()
    print(f"In Pro but NOT in Protect ({len(report['pro_not_in_protect'])}) - Macs with no Protect agent record:")
    print(summarize(report["pro_not_in_protect"], ["serial_number", "name", "model"], args.limit))
    print()
    print(f"Protect records with NO serial ({len(report['protect_missing_serial'])}) - invisible to the sync forever:")
    print(summarize(report["protect_missing_serial"], ["uuid", "hostName", "checkin"], args.limit))
    print()
    print(f"Duplicate serials in Protect ({len(report['protect_duplicate_serials'])}):")
    for serial, records in list(report["protect_duplicate_serials"].items())[: args.limit]:
        print(f"  - {serial}: {len(records)} records")
    if not report["protect_duplicate_serials"]:
        print("  (none)")
    print()
    print(f"Protect agents stale > {args.stale_days} days but still in Pro ({len(report['protect_stale_checkin'])}):")
    print(summarize(report["protect_stale_checkin"], ["serial", "hostName", "checkin"], args.limit))
    print()
    print(f"Unmanaged computers in Pro ({len(report['pro_unmanaged'])}) - kept in Protect by the sync:")
    print(summarize(report["pro_unmanaged"], ["serial_number", "name"], args.limit))


if __name__ == "__main__":
    main()
