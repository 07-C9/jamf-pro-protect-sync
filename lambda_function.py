"""
Jamf Pro to Jamf Protect Computer Sync - Lambda Function
Deletes computers from Protect that no longer exist in Pro
"""

import json
import os
import boto3
import requests
from typing import List, Set, Dict, Tuple
from botocore.exceptions import ClientError

# Configuration from environment variables
SECRET_NAME = os.environ.get('SECRET_NAME', 'JamfSyncCredentials')
MAX_DELETIONS_ALLOWED = int(os.environ.get('MAX_DELETIONS_ALLOWED', '50'))
DIAGNOSTIC_MODE = os.environ.get('DIAGNOSTIC_MODE', 'false').lower() == 'true'

# Timeout for every HTTP call so a hung connection fails fast
# instead of stalling until the Lambda timeout kills the run
REQUEST_TIMEOUT = 30

# ============================================================================
# AWS SECRETS MANAGER
# ============================================================================

def get_credentials() -> Dict:
    """Retrieve credentials from AWS Secrets Manager"""
    try:
        secrets_client = boto3.client('secretsmanager')
        response = secrets_client.get_secret_value(SecretId=SECRET_NAME)
        secret_string = response['SecretString']
        credentials = json.loads(secret_string)
        print(f"[SECRETS] Successfully retrieved credentials from {SECRET_NAME}")
        return credentials
    except ClientError as e:
        print(f"[ERROR] Failed to retrieve secrets: {e}")
        raise


# ============================================================================
# JAMF PRO API FUNCTIONS
# ============================================================================

def get_jamf_pro_token(base_url: str, client_id: str, client_secret: str) -> str:
    """Get bearer token for Jamf Pro API using Client Credentials"""
    token_url = f"{base_url}/api/oauth/token"

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials"
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        response = requests.post(token_url, data=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        token_data = response.json()
        print(f"[JAMF PRO] Access token obtained (expires in {token_data.get('expires_in', 'unknown')}s)")
        return token_data["access_token"]
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Jamf Pro: Failed to get token - {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"[ERROR] Response: {e.response.text}")
        raise


def get_all_jamf_pro_computers(base_url: str, token: str) -> List[Dict]:
    """Get all computers from Jamf Pro using Classic API"""
    computers_url = f"{base_url}/JSSResource/computers/subset/basic"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    try:
        response = requests.get(computers_url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        computers = data.get("computers", [])
        print(f"[JAMF PRO] Retrieved {len(computers)} computers")
        return computers

    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Jamf Pro: Failed to retrieve computers - {e}")
        raise


def extract_serial_numbers_from_pro(computers: List[Dict]) -> Set[str]:
    """Extract and normalize serial numbers from Jamf Pro computer list"""
    serials = set()

    if DIAGNOSTIC_MODE and len(computers) > 0:
        print("\n[DIAGNOSTIC] First computer structure from Jamf Pro:")
        print(json.dumps(computers[0], indent=2))
        print()

    for computer in computers:
        serial = (
            computer.get("serial_number") or
            computer.get("serialNumber") or
            computer.get("serial") or
            ""
        ).strip().upper()

        if serial:
            serials.add(serial)
        elif DIAGNOSTIC_MODE:
            print(f"[WARNING] Computer missing serial: {computer.get('name', 'Unknown')}")

    print(f"[JAMF PRO] Extracted {len(serials)} valid serial numbers")

    if DIAGNOSTIC_MODE and len(serials) > 0:
        sample = list(serials)[:5]
        print(f"[JAMF PRO] Sample serials: {', '.join(sample)}")

    return serials


# ============================================================================
# JAMF PROTECT API FUNCTIONS
# ============================================================================

def get_protect_access_token(protect_instance: str, client_id: str, client_secret: str) -> str:
    """Gets access token for Jamf Protect API"""
    token_url = f"https://{protect_instance}.protect.jamfcloud.com/token"
    payload = {"client_id": client_id, "password": client_secret}

    try:
        resp = requests.post(token_url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp_data = resp.json()
        print(f"[JAMF PROTECT] Access token granted (valid for {int(resp_data['expires_in'] // 60)} minutes)")
        return resp_data["access_token"]
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Jamf Protect: Failed to get token - {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"[ERROR] Response: {e.response.text}")
        raise


def make_protect_api_call(protect_instance: str, access_token: str, query: str, variables: Dict = None) -> Dict:
    """Sends a GraphQL query to Jamf Protect API"""
    if variables is None:
        variables = {}

    api_url = f"https://{protect_instance}.protect.jamfcloud.com/graphql"
    payload = {"query": query, "variables": variables}
    headers = {
        "Authorization": access_token,
        "Content-Type": "application/json"
    }

    response = requests.post(api_url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


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

DELETE_COMPUTER_MUTATION = """
mutation deleteComputer($uuid: ID!) {
  deleteComputer(uuid: $uuid) {
    hostName
  }
}
"""


def get_all_protect_computers(protect_instance: str, access_token: str) -> List[Dict]:
    """Get all computers from Jamf Protect with pagination"""
    next_token = None
    computers = []
    page_count = 1

    print("[JAMF PROTECT] Retrieving computers (paginated):")

    while True:
        print(f"[JAMF PROTECT] Retrieving page {page_count}...")

        vars = {"page_size": 100, "next": next_token}
        resp = make_protect_api_call(protect_instance, access_token, LIST_COMPUTERS_QUERY, vars)

        next_token = resp["data"]["listComputers"]["pageInfo"]["next"]
        computers.extend(resp["data"]["listComputers"]["items"])

        if next_token is None:
            break

        page_count += 1

    print(f"[JAMF PROTECT] Retrieved {len(computers)} computers total")
    return computers


def normalize_protect_computers(computers: List[Dict]) -> Dict[str, Dict]:
    """Normalize Protect computer data, keyed by serial number"""
    normalized = {}

    if DIAGNOSTIC_MODE and len(computers) > 0:
        print("\n[DIAGNOSTIC] First computer structure from Jamf Protect:")
        print(json.dumps(computers[0], indent=2))
        print()

    for computer in computers:
        serial = computer.get("serial", "").strip().upper()
        if serial:
            normalized[serial] = computer

    print(f"[JAMF PROTECT] Normalized {len(normalized)} computers with valid serials")

    if DIAGNOSTIC_MODE and len(normalized) > 0:
        sample = list(normalized.keys())[:5]
        print(f"[JAMF PROTECT] Sample serials: {', '.join(sample)}")

    return normalized


# ============================================================================
# SYNC LOGIC
# ============================================================================

def find_computers_to_delete(pro_serials: Set[str], protect_computers: Dict[str, Dict]) -> List[Dict]:
    """Find computers that exist in Protect but not in Pro"""
    to_delete = []

    for serial, computer in protect_computers.items():
        if serial not in pro_serials:
            to_delete.append(computer)

    return to_delete


def delete_computers_from_protect(
    protect_instance: str,
    access_token: str,
    computers_to_delete: List[Dict],
    api_call=make_protect_api_call
) -> Tuple[int, int]:
    """
    Delete computers from Jamf Protect, logging every serial (never hostnames) for auditing
    Returns tuple of (success_count, failure_count)
    """
    success_count = 0
    failure_count = 0

    print(f"\n[DELETE] Beginning deletion of {len(computers_to_delete)} computers...")

    for idx, computer in enumerate(computers_to_delete, 1):
        try:
            variables = {"uuid": computer["uuid"]}
            resp = api_call(
                protect_instance,
                access_token,
                DELETE_COMPUTER_MUTATION,
                variables
            )
            # A refused delete returns HTTP 200 with errors and no deleteComputer result
            resp['data']['deleteComputer']['hostName']
            print(f"[DELETE] {idx}/{len(computers_to_delete)} - Deleted serial {computer['serial']}")
            success_count += 1

        except Exception as e:
            print(f"[ERROR] Failed to delete serial {computer.get('serial', 'Unknown')}: {e}")
            failure_count += 1

    return success_count, failure_count


# ============================================================================
# LAMBDA HANDLER
# ============================================================================

def lambda_handler(event, context):
    """
    Main Lambda handler function
    Triggered by EventBridge on schedule
    """
    print("=" * 80)
    print("Jamf Pro to Jamf Protect Computer Sync - Lambda Execution")
    print("=" * 80)
    print()

    try:
        # Get credentials from Secrets Manager
        print("[STEP 0] Retrieving credentials from AWS Secrets Manager")
        print("-" * 80)
        creds = get_credentials()
        print()

        # Step 1: Get all computers from Jamf Pro
        print("[STEP 1] Fetching computers from Jamf Pro")
        print("-" * 80)
        pro_token = get_jamf_pro_token(
            creds['jamf_pro_url'],
            creds['jamf_pro_client_id'],
            creds['jamf_pro_client_secret']
        )
        pro_computers = get_all_jamf_pro_computers(creds['jamf_pro_url'], pro_token)
        pro_serials = extract_serial_numbers_from_pro(pro_computers)
        print()

        # Step 2: Get all computers from Jamf Protect
        print("[STEP 2] Fetching computers from Jamf Protect")
        print("-" * 80)
        protect_token = get_protect_access_token(
            creds['jamf_protect_instance'],
            creds['jamf_protect_client_id'],
            creds['jamf_protect_client_secret']
        )
        protect_computers_list = get_all_protect_computers(creds['jamf_protect_instance'], protect_token)
        protect_computers = normalize_protect_computers(protect_computers_list)
        print()

        # Step 3: Compare and find orphaned computers
        print("[STEP 3] Comparing inventories")
        print("-" * 80)
        computers_to_delete = find_computers_to_delete(pro_serials, protect_computers)

        print(f"Jamf Pro computers: {len(pro_serials)}")
        print(f"Jamf Protect computers: {len(protect_computers)}")
        print(f"Computers to delete: {len(computers_to_delete)}")
        print()

        # Step 4: Check if sync needed
        if len(computers_to_delete) == 0:
            print("[SUCCESS] No orphaned computers found. Inventories are in sync!")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Sync complete - no deletions needed',
                    'pro_count': len(pro_serials),
                    'protect_count': len(protect_computers),
                    'deleted': 0
                })
            }

        # Step 5: Safety check
        if len(computers_to_delete) > MAX_DELETIONS_ALLOWED:
            error_msg = f"Safety failsafe triggered: Attempting to delete {len(computers_to_delete)} computers (max: {MAX_DELETIONS_ALLOWED})"
            print(f"[SAFETY FAILSAFE] {error_msg}")
            print(f"[SAFETY FAILSAFE] This sync will NOT proceed.")
            print(f"[SAFETY FAILSAFE] Please review the data or increase MAX_DELETIONS_ALLOWED.")

            return {
                'statusCode': 400,
                'body': json.dumps({
                    'message': 'Safety failsafe triggered',
                    'error': error_msg,
                    'computers_to_delete': len(computers_to_delete),
                    'max_allowed': MAX_DELETIONS_ALLOWED
                })
            }

        # Step 6: Show preview of deletions
        print("[STEP 4] Preview of computers to be deleted")
        print("-" * 80)
        for idx, computer in enumerate(computers_to_delete[:10], 1):
            print(f"  {idx}. {computer['hostName']} (Serial: {computer['serial']})")

        if len(computers_to_delete) > 10:
            print(f"  ... and {len(computers_to_delete) - 10} more")
        print()

        # Step 7: Execute deletions
        success_count, failure_count = delete_computers_from_protect(
            creds['jamf_protect_instance'],
            protect_token,
            computers_to_delete
        )

        print()
        print("=" * 80)
        print("[COMPLETE] Sync finished")
        print(f"[COMPLETE] Successfully deleted: {success_count}/{len(computers_to_delete)} computers")
        if failure_count > 0:
            print(f"[COMPLETE] Failed deletions: {failure_count}")
        print("=" * 80)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Sync complete',
                'pro_count': len(pro_serials),
                'protect_count': len(protect_computers),
                'to_delete': len(computers_to_delete),
                'deleted_success': success_count,
                'deleted_failed': failure_count
            })
        }

    except Exception as e:
        error_msg = f"Fatal error during sync: {str(e)}"
        print(f"\n[FATAL] {error_msg}")

        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Sync failed',
                'error': error_msg
            })
        }
