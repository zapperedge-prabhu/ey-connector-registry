# Microsoft 365 API Connector — Client Setup Guide

**Version:** 1.0.0
**Date:** August 07, 2026
**Connector:** Microsoft 365 API Connector
**Purpose:** Step-by-step guide for generating the credentials required to connect EY SAM Studio to your Microsoft 365 system

> The setup steps and configuration values below are sourced directly from the
> SAM Studio vendor knowledge corpus, keeping this guide in lock-step with the
> connector reference documentation.

---

## Overview

This connector extracts data from your **Microsoft 365** system via its REST API and loads it into the SAM platform for analysis. To enable the connector, you need to generate the appropriate credentials in your Microsoft 365 environment and provide them securely to your EY team.

No data is modified by this connector — all API access is **read-only**.

---

## Prerequisites

Before you begin, ensure you have:

- [ ] **Global Administrator** (or Privileged Role Administrator) in Microsoft Entra ID (Azure AD)
- [ ] Access to the Azure portal to register an application
- [ ] Permission to grant admin consent for API permissions
- [ ] Network connectivity to the API base URL and token endpoint

---

## APIs Accessed by This Connector

The connector will call the following API endpoints. All calls are read-only (GET requests):

| Method | Endpoint | Bridge Table | Fields Extracted |
|--------|----------|--------------|------------------|
| `GET` | `/subscribedSkus` | subscribedskus | grouptype, displayname |

**Base URL:** `https://graph.microsoft.com/v1.0`

---

## Authentication Method

- **Type:** OAuth 2.0 Client Credentials Flow (Service Principal)
- **Token Endpoint:** `https://login.microsoftonline.com/{MSGRAPH_TENANT_ID}/oauth2/v2.0/token`
- **Scope:** `https://graph.microsoft.com/.default`
- **Token Lifetime:** 60 minutes (automatically refreshed by the connector)

---

## Client Setup Guide — How to Create the App Registration

**Step 1:** Sign in to [https://portal.azure.com](https://portal.azure.com) as a Global Administrator → **Entra ID** → **App registrations** → **New registration**.
- Name: `EY-SAM-M365-Connector` | Account types: Single tenant | Redirect URI: blank → **Register**
- Copy **Application (client) ID** → your Client ID and **Directory (tenant) ID** → your Tenant ID.

**Step 2:** In the app registration → **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**. Add:

| Permission | Purpose |
|-----------|---------|
| `User.Read.All` | Read all user accounts and attributes |
| `Organization.Read.All` | Read tenant organisation / subscription details |
| `Directory.Read.All` | Read directory objects (groups, roles) |
| `LicenseAssignment.Read.All` | Read per-user Microsoft 365 licence assignments |
| `Reports.Read.All` | Read Office 365 activity and usage reports |

Click **Grant admin consent for [your organisation]** → **Yes**. All permissions must show green ✓ Granted.

**Step 3:** **Certificates & secrets** → **New client secret** → set description and expiry (12 or 24 months) → **Add** → copy **Value** immediately.

**Step 4 — Verify (two-step curl):**
```bash
# Get token
curl -s -X POST "https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token" \
  -d "grant_type=client_credentials&client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}&scope=https://graph.microsoft.com/.default"

# Call Graph API
curl -s -H "Authorization: Bearer {ACCESS_TOKEN}" \
  "https://graph.microsoft.com/v1.0/subscribedSkus"
```

**Troubleshooting:**

| Error | Cause | Fix |
|-------|-------|-----|
| `AADSTS700016` | Wrong Tenant ID or Client ID | Re-copy from App registration Overview |
| `AADSTS7000215` | Secret expired or copied incorrectly | Rotate secret; copy Value (not Secret ID) |
| `AADSTS65001` | Admin consent not granted | Click "Grant admin consent" on API permissions page |
| `403 Forbidden` | Missing Graph application permissions | Ensure all permissions added and admin consent granted |

---

## Configuration Values to Provide

**These are obtained by registering an application in Azure Active Directory (Entra ID) — not by generating a Personal Access Token. Microsoft 365 / Graph API uses OAuth 2.0 client credentials flow.**

| Parameter | Description | Where to Find | Example Format |
|-----------|-------------|---------------|----------------|
| **Tenant ID** | Your Azure AD directory identifier | Azure Portal → Entra ID → Overview → "Directory (tenant) ID" | `12345678-1234-1234-1234-123456789abc` |
| **Client ID** | The app registration's Application ID | App registrations → your app → Overview → "Application (client) ID" | `87654321-4321-4321-4321-210987654321` |
| **Client Secret** | The secret value generated for the app | App registrations → your app → Certificates & secrets → Value column | `Abcd~efGHijklmnop1234567890QRSTUV-xyz` |

> ⚠️ Copy the **Value** column immediately after creation — Azure hides it after you navigate away.

### How to Transmit Credentials Securely

These credentials are **highly sensitive**. Follow these guidelines:

1. **Do not send via unencrypted email** — use your organisation's secure file-sharing or encrypted email tool
2. Use EY's secure credential sharing platform if one has been provided to you
3. Store a copy in your organisation's secret management system (e.g. Azure Key Vault, HashiCorp Vault, AWS Secrets Manager)
4. Never commit these values to version control (Git)
5. Apply the principle of least privilege — only share credentials with personnel who need them

---

## Security Best Practices

1. **Credential Rotation:**
   - Rotate secrets/tokens every 12–24 months (or per your organisation's policy)
   - Update the connector configuration in SAM Studio whenever credentials change

2. **Least Privilege:**
   - Grant only the permissions listed in this guide
   - Do not add extra permissions "just in case"

3. **Dedicated Service Identity:**
   - Use a dedicated service account / application credential — never a personal account
   - Document the account purpose and assign at least two owners/administrators

4. **Monitoring:**
   - Monitor sign-in and API access logs in your vendor portal
   - Set alerts for unusual activity or failed authentication attempts

5. **Secret Storage:**
   - Store all credentials in a secrets manager — never in plain text files or code

---

## Frequently Asked Questions

**Q: Will this connector make any changes to my Microsoft 365 data?**
A: No. All permissions are read-only. The connector cannot create, modify, or delete any records.

**Q: How often does the connector run?**
A: The run schedule is configured by EY based on your requirements. Contact your EY team for details.

**Q: Can I revoke access later?**
A: Yes. Disable or delete the application / credentials, deactivate the service account, or revoke the API key/token at any time via your Microsoft 365 portal.

**Q: What if my credentials expire?**
A: Generate new credentials following the steps above and provide the updated values to EY. The connector configuration will be updated promptly.

---

## Support

### For Microsoft 365 / Vendor-Side Issues:
- Refer to your vendor's official documentation and support portal

### For SAM Studio Connector Issues:
- Contact: EY SAM Support Team
- Reference: `microsoft_365` connector
- Please include the response or error message from the verification step above in your support request

---

*Generated by EY SAM Studio Code Generation Pipeline — August 07, 2026*
