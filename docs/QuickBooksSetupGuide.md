# QuickBooks Online (QBO) Integration Setup Guide

This guide will walk you through setting up QuickBooks Online OAuth 2.0 authentication and API integration for your MER (Monthly Expense Review) agent. This integration allows you to retrieve Chart of Accounts data from QuickBooks to compare with your Google Sheets (MER) data.

## 📋 Table of Contents

1. [Prerequisites](#prerequisites)
2. [Step 1: Register Your App in Intuit Developer Portal](#step-1-register-your-app-in-intuit-developer-portal)
3. [Step 2: Configure Environment Variables](#step-2-configure-environment-variables)
4. [Step 3: Install Dependencies](#step-3-install-dependencies)
5. [Step 4: Understanding the OAuth Flow](#step-4-understanding-the-oauth-flow)
6. [Step 5: Testing the Integration](#step-5-testing-the-integration)
7. [Step 6: Retrieving Chart of Accounts](#step-6-retrieving-chart-of-accounts)
8. [Troubleshooting](#troubleshooting)
9. [Security Best Practices](#security-best-practices)

---

## Prerequisites

Before starting, ensure you have:

- ✅ A QuickBooks Online account (Sandbox account is fine for testing)
- ✅ Access to [Intuit Developer Portal](https://developer.intuit.com/)
- ✅ Your Microsoft Multi-Agent template project set up and running
- ✅ Basic understanding of OAuth 2.0 flow
- ✅ Python 3.8+ installed

---

## Step 1: Register Your App in Intuit Developer Portal

### 1.1 Create a Developer Account

1. Go to [https://developer.intuit.com/](https://developer.intuit.com/)
2. Click **"Sign In"** or **"Create Account"** if you don't have one
3. Sign in with your Intuit account

### 1.2 Create a New App

1. Once logged in, click on **"My Apps"** in the top navigation
2. Click **"Create an app"** button
3. Fill in the app details:
   - **App Name**: Your app name (e.g., "MER Review Agent")
   - **Description**: Brief description of what your app does
   - **Product**: Select **"QuickBooks Online and Payments"**
   - **Development Type**: Select **"Sandbox"** for testing (you can switch to Production later)

4. Click **"Create app"**

### 1.3 Configure OAuth Settings

1. After creating the app, you'll be taken to the app dashboard
2. Navigate to the **"Keys & OAuth"** section (usually in the left sidebar)
3. You'll see:
   - **Client ID**: Copy this (you'll need it later)
   - **Client Secret**: Click "Show" and copy this (you'll need it later)

4. Scroll down to **"Redirect URIs"** section
5. Click **"Add URI"** and add your callback URL:
   - For local development: `http://localhost:8000/api/v4/quickbooks/callback`
   - For production: `https://yourdomain.com/api/v4/quickbooks/callback`

   ⚠️ **IMPORTANT**: The redirect URI must match EXACTLY what you use in your code, including:
   - Protocol (http vs https)
   - Port number
   - Path
   - Trailing slashes (or lack thereof)

6. Click **"Save"**

### 1.4 Configure Scopes (Permissions)

1. In the same **"Keys & OAuth"** section, find **"Scopes"**
2. Make sure the following scope is selected:
   - ✅ `com.intuit.quickbooks.accounting` - This gives access to accounting data including Chart of Accounts

3. Click **"Save"** if you made changes

### 1.5 Get Your Sandbox Company (for Testing)

1. Go to [https://appcenter.intuit.com/app/sandbox](https://appcenter.intuit.com/app/sandbox)
2. Sign in with the same Intuit account
3. You'll see a list of sandbox companies (or you can create a new one)
4. Click on a company to open it in QuickBooks Online Sandbox
5. Note: You can use this sandbox company to test your integration

---

## Step 2: Configure Environment Variables

### 2.1 Locate Your .env File

Your backend uses environment variables stored in a `.env` file. **The `.env` file MUST be located in the backend directory**:
- `src/backend/.env` (for local development)

**Important**: The `.env` file should be in the `src/backend/` directory, NOT in the project root. The application is configured to automatically load it from that location.

If the file doesn't exist, create it in the backend directory:
```bash
cd src/backend
touch .env  # Linux/macOS
# or
New-Item .env  # Windows PowerShell
```

### 2.2 Add QuickBooks Configuration

Open your `.env` file and add the following variables:

```bash
# QuickBooks Online Configuration
QB_CLIENT_ID=your_client_id_here
QB_CLIENT_SECRET=your_client_secret_here
QB_REDIRECT_URI=http://localhost:8000/api/v4/quickbooks/callback
QB_ENVIRONMENT=sandbox
QB_SCOPE=com.intuit.quickbooks.accounting
```

**Replace the values:**
- `QB_CLIENT_ID`: Paste your Client ID from Step 1.3
- `QB_CLIENT_SECRET`: Paste your Client Secret from Step 1.3
- `QB_REDIRECT_URI`: Must match exactly what you set in the Intuit Developer Portal (Step 1.3)
- `QB_ENVIRONMENT`: Use `sandbox` for testing, `production` for live data
- `QB_SCOPE`: Keep as shown (this is the accounting scope)

### 2.3 Example .env File

Here's a complete example of what your `.env` file might look like:

```bash
# Existing Azure/AI configuration...
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4
# ... other existing vars ...

# QuickBooks Online Configuration
QB_CLIENT_ID=ABcdEFgh1234567890iJkLmNoPqRsTuVwXyZ
QB_CLIENT_SECRET=1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r9s0t
QB_REDIRECT_URI=http://localhost:8000/api/v4/quickbooks/callback
QB_ENVIRONMENT=sandbox
QB_SCOPE=com.intuit.quickbooks.accounting
```

⚠️ **Security Note**: Never commit your `.env` file to Git! It should already be in `.gitignore`.

---

## Step 3: Install Dependencies

### 3.1 Navigate to Backend Directory

```bash
cd src/backend
```

### 3.2 Install Python Dependencies

The required packages are already listed in `requirements.txt`. Install them:

```bash
pip install -r requirements.txt
```

Or if you're using a virtual environment (recommended):

```bash
# Create virtual environment (if you haven't already)
python3 -m venv venv

# Activate virtual environment
# On macOS/Linux:
source venv/bin/activate
# On Windows:
# venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3.3 Verify Installation

Verify that the QuickBooks package is installed:

```bash
python3 -c "from intuitlib.client import AuthClient; print('✅ intuit-oauth installed successfully')"
```

If you see an error, try:
```bash
pip install intuit-oauth==1.2.6
```

---

## Step 4: Understanding the OAuth Flow

Here's how the QuickBooks OAuth 2.0 flow works:

### 4.1 The Complete Flow

```
┌─────────┐         ┌──────────────┐         ┌─────────────┐         ┌──────────┐
│  User   │         │ Your Backend │         │ QuickBooks  │         │ QuickBooks│
│ Browser │         │   (FastAPI)  │         │ Auth Server │         │   API     │
└────┬────┘         └──────┬───────┘         └──────┬──────┘         └─────┬─────┘
     │                     │                         │                      │
     │ 1. GET /api/v4/     │                         │                      │
     │    quickbooks/      │                         │                      │
     │    authorize        │                         │                      │
     ├────────────────────>│                         │                      │
     │                     │                         │                      │
     │ 2. Redirect to      │                         │                      │
     │    authorization    │                         │                      │
     │    URL              │                         │                      │
     │<────────────────────┤                         │                      │
     │                     │                         │                      │
     │ 3. User authorizes  │                         │                      │
     │    your app         │                         │                      │
     ├─────────────────────┼────────────────────────>│                      │
     │                     │                         │                      │
     │ 4. Redirect with    │                         │                      │
     │    code & realmId   │                         │                      │
     │<────────────────────┼─────────────────────────┤                      │
     │                     │                         │                      │
     │ 5. GET /api/v4/     │                         │                      │
     │    quickbooks/      │                         │                      │
     │    callback?code=   │                         │                      │
     │    ...&realmId=...  │                         │                      │
     ├────────────────────>│                         │                      │
     │                     │                         │                      │
     │ 6. Exchange code    │                         │                      │
     │    for tokens       │                         │                      │
     │                     ├────────────────────────>│                      │
     │                     │                         │                      │
     │ 7. Receive tokens   │                         │                      │
     │    (access_token,   │                         │                      │
     │     refresh_token)  │                         │                      │
     │                     │<────────────────────────┤                      │
     │                     │                         │                      │
     │ 8. Store tokens     │                         │                      │
     │    securely         │                         │                      │
     │                     │                         │                      │
     │ 9. Use access_token │                         │                      │
     │    for API calls    │                         │                      │
     │                     │                         │                      │
     │ 10. GET Chart of    │                         │                      │
     │     Accounts        │                         │                      │
     │                     ├─────────────────────────┼─────────────────────>│
     │                     │                         │                      │
     │ 11. Return accounts │                         │                      │
     │                     │<────────────────────────┼──────────────────────┤
     │                     │                         │                      │
     │ 12. Display data    │                         │                      │
     │<────────────────────┤                         │                      │
```

### 4.2 Key Concepts

1. **Authorization URL**: The URL where users log in and grant permissions
2. **Authorization Code**: Temporary code returned after user grants permission (valid for ~5 minutes)
3. **Access Token**: Used to make API calls (expires in ~1 hour)
4. **Refresh Token**: Used to get new access tokens without user re-authorization (valid for 101 days)
5. **Realm ID**: The QuickBooks company ID (you need this for all API calls)

---

## Step 5: Testing the Integration

### 5.1 Start Your Backend Server

Make sure your backend is running:

```bash
cd src/backend
python app.py
```

Or if using uvicorn directly:

```bash
uvicorn app:app --reload --port 8000
```

You should see output like:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete.
```

### 5.2 Test the Authorization Endpoint

#### Option A: Using curl (Command Line)

```bash
# Replace YOUR_USER_ID with a test user ID or get it from your auth system
curl -X GET "http://localhost:8000/api/v4/quickbooks/authorize" \
  -H "x-ms-client-principal: <your-auth-header>" \
  -H "Content-Type: application/json"
```

**Note**: You'll need to provide proper authentication headers. Check your `auth_utils.py` to see what headers are expected.

#### Option B: Using Browser

1. Open your browser
2. Navigate to: `http://localhost:8000/api/v4/quickbooks/authorize`
3. You should get a JSON response with `authorization_url`
4. Copy the `authorization_url` and open it in your browser

#### Option C: Using Python requests

Create a test script `test_qbo_auth.py`:

```python
import requests
import json

# Adjust the URL and headers based on your auth setup
url = "http://localhost:8000/api/v4/quickbooks/authorize"
headers = {
    # Add your authentication headers here
    # For example, if you're using Azure AD:
    # "Authorization": "Bearer YOUR_TOKEN"
}

response = requests.get(url, headers=headers)
print(json.dumps(response.json(), indent=2))

# Extract and open the authorization URL
auth_url = response.json()["authorization_url"]
print(f"\n🌐 Open this URL in your browser:\n{auth_url}")
```

### 5.3 Complete the OAuth Flow

1. **Get Authorization URL**: Call the `/api/v4/quickbooks/authorize` endpoint
2. **Authorize the App**: 
   - Open the `authorization_url` in your browser
   - Sign in to QuickBooks (use your sandbox account)
   - Click "Authorize" or "Connect" to grant permissions
3. **Handle the Redirect**: 
   - QuickBooks will redirect to your callback URL: `http://localhost:8000/api/v4/quickbooks/callback?code=...&realmId=...`
   - Your backend will automatically exchange the code for tokens
   - You should see a success message

### 5.4 Test Company Info Endpoint

Once you've completed OAuth, test the company info endpoint:

```bash
curl -X GET "http://localhost:8000/api/v4/quickbooks/company-info" \
  -H "x-ms-client-principal: <your-auth-header>"
```

This should return your QuickBooks company information, confirming the connection is working.

---

## Step 6: Retrieving Chart of Accounts

### 6.1 Understanding Chart of Accounts

The Chart of Accounts is a list of all accounts in QuickBooks, including:
- **Assets**: Cash, Accounts Receivable, Inventory, etc.
- **Liabilities**: Accounts Payable, Loans, Credit Cards, etc.
- **Equity**: Owner's Equity, Retained Earnings, etc.
- **Revenue**: Sales, Service Revenue, etc.
- **Expenses**: Rent, Utilities, Office Supplies, etc.

Each account has:
- **Name**: Account name
- **Type**: Account type (Asset, Liability, etc.)
- **SubType**: More specific classification
- **Balance**: Current balance
- **Account Number**: Optional account number
- **Fully Qualified Name**: Full account path (for sub-accounts)

### 6.2 Call the Chart of Accounts Endpoint

```bash
curl -X GET "http://localhost:8000/api/v4/quickbooks/chart-of-accounts" \
  -H "x-ms-client-principal: <your-auth-header>"
```

### 6.3 Understanding the Response

The response will look like this:

```json
{
  "QueryResponse": {
    "Account": [
      {
        "Name": "Cash and cash equivalents",
        "AccountType": "Bank",
        "AccountSubType": "CashOnHand",
        "CurrentBalance": 50000.00,
        "FullyQualifiedName": "Cash and cash equivalents",
        "Id": "1",
        "SyncToken": "0"
      },
      {
        "Name": "Accounts Receivable",
        "AccountType": "Accounts Receivable",
        "CurrentBalance": 25000.00,
        "FullyQualifiedName": "Accounts Receivable",
        "Id": "2",
        "SyncToken": "0"
      }
      // ... more accounts
    ],
    "maxResults": 100,
    "startPosition": 1
  },
  "time": "2025-01-XXT..."
}
```

### 6.4 Compare with MER (Google Sheets)

Now you can:

1. **Retrieve QBO Chart of Accounts** using the endpoint above
2. **Retrieve MER data** from Google Sheets (you already have this functionality)
3. **Match accounts** by name or account number
4. **Compare balances** and identify discrepancies

Example comparison logic (pseudo-code):

```python
# Get QBO accounts
qbo_accounts = await qbo_service.get_chart_of_accounts(user_id)

# Get MER accounts from Google Sheets
mer_accounts = get_mer_accounts_from_sheets()

# Create mapping by account name
qbo_map = {acc["Name"]: acc for acc in qbo_accounts["QueryResponse"]["Account"]}

# Compare
discrepancies = []
for mer_account in mer_accounts:
    qbo_account = qbo_map.get(mer_account["name"])
    if qbo_account:
        if abs(mer_account["balance"] - qbo_account["CurrentBalance"]) > 0.01:
            discrepancies.append({
                "account": mer_account["name"],
                "mer_balance": mer_account["balance"],
                "qbo_balance": qbo_account["CurrentBalance"],
                "difference": mer_account["balance"] - qbo_account["CurrentBalance"]
            })
```

---

## Troubleshooting

### Issue: "QuickBooks AuthClient not initialized"

**Cause**: Environment variables not set or incorrect.

**Solution**:
1. Check that your `.env` file exists in `src/backend/`
2. Verify all `QB_*` variables are set correctly
3. Restart your backend server after changing `.env`
4. Check for typos in variable names (they're case-sensitive)

### Issue: "Redirect URI mismatch"

**Cause**: The redirect URI in your code doesn't match what's registered in Intuit Developer Portal.

**Solution**:
1. Go to Intuit Developer Portal → Your App → Keys & OAuth
2. Check the exact redirect URI registered (including protocol, port, path)
3. Make sure `QB_REDIRECT_URI` in your `.env` matches EXACTLY
4. Common mistakes:
   - `http` vs `https`
   - Missing port number (`:8000`)
   - Trailing slash differences
   - Different paths

### Issue: "Invalid authorization code" or "Code expired"

**Cause**: Authorization codes expire after ~5 minutes, or the code was already used.

**Solution**:
1. Start the OAuth flow again from the beginning
2. Complete the authorization quickly (within 5 minutes)
3. Make sure you're not reusing old codes

### Issue: "401 Unauthorized" when calling API

**Cause**: Access token expired (they expire after ~1 hour).

**Solution**:
- The service should automatically refresh tokens, but if not:
1. Check that refresh tokens are being stored correctly
2. Manually trigger a refresh or re-authenticate
3. Check token expiration times

### Issue: "No tokens found for user"

**Cause**: Tokens weren't saved after OAuth completion.

**Solution**:
1. Complete the OAuth flow again
2. Check that the `_save_tokens` method is working (currently a placeholder)
3. Implement proper token storage (see Security Best Practices below)

### Issue: "ModuleNotFoundError: No module named 'intuitlib'"

**Cause**: The `intuit-oauth` package isn't installed.

**Solution**:
```bash
pip install intuit-oauth==1.2.6
```

---

## Security Best Practices

### 1. Token Storage

⚠️ **IMPORTANT**: The current implementation has placeholder token storage. For production, you MUST implement secure token storage.

**Options**:

#### Option A: Database Storage (Recommended)
Store tokens in your Cosmos DB with encryption:

```python
async def _save_tokens(self, user_id: str, token_data: Dict[str, Any]) -> None:
    """Save encrypted tokens to database."""
    # Encrypt sensitive data
    encrypted_data = encrypt_tokens(token_data)
    
    # Store in database
    await self.memory_store.save_user_tokens(
        user_id=user_id,
        service="quickbooks",
        tokens=encrypted_data
    )
```

#### Option B: Azure Key Vault (Most Secure)
Store tokens in Azure Key Vault:

```python
from azure.keyvault.secrets import SecretClient
from azure.identity import DefaultAzureCredential

async def _save_tokens(self, user_id: str, token_data: Dict[str, Any]) -> None:
    """Save tokens to Azure Key Vault."""
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url="https://your-vault.vault.azure.net/", credential=credential)
    
    secret_name = f"qbo-tokens-{user_id}"
    client.set_secret(secret_name, json.dumps(token_data))
```

### 2. Environment Variables

- ✅ Never commit `.env` files to Git
- ✅ Use Azure Key Vault or Azure App Service Configuration for production
- ✅ Rotate Client Secrets regularly
- ✅ Use different Client IDs/Secrets for sandbox vs production

### 3. HTTPS in Production

- ✅ Always use HTTPS in production (never HTTP)
- ✅ Update redirect URI to use HTTPS
- ✅ Use valid SSL certificates

### 4. Token Refresh

- ✅ Always implement automatic token refresh
- ✅ Handle refresh token expiration (users need to re-authenticate after 101 days)
- ✅ Log token refresh events for monitoring

### 5. Error Handling

- ✅ Don't expose sensitive information in error messages
- ✅ Log errors securely (without tokens)
- ✅ Implement rate limiting to prevent abuse

---

## Next Steps

Now that you have QuickBooks integration working, you can:

1. ✅ **Implement Token Storage**: Update `_save_tokens()` and `_load_tokens()` methods to use your database
2. ✅ **Build Comparison Logic**: Create functions to match and compare MER accounts with QBO accounts
3. ✅ **Add More Endpoints**: Retrieve invoices, bills, transactions, etc.
4. ✅ **Create MER Review Agent**: Integrate this into your agent workflow
5. ✅ **Add Error Recovery**: Handle token expiration, network errors, etc.
6. ✅ **Production Deployment**: Move from sandbox to production environment

---

## API Reference

### Endpoints

#### GET `/api/v4/quickbooks/authorize`
Get QuickBooks OAuth authorization URL.

**Response**:
```json
{
  "authorization_url": "https://appcenter.intuit.com/connect/oauth2?...",
  "state": "uuid-string",
  "message": "Visit the authorization_url to connect your QuickBooks account"
}
```

#### GET `/api/v4/quickbooks/callback`
Handle OAuth callback and exchange code for tokens.

**Query Parameters**:
- `code` (required): Authorization code from QuickBooks
- `realmId` (required): Company ID from QuickBooks
- `state` (optional): CSRF state token

**Response**:
```json
{
  "status": "success",
  "message": "QuickBooks account connected successfully",
  "realm_id": "123456789",
  "expires_in": 3600
}
```

#### GET `/api/v4/quickbooks/company-info`
Get QuickBooks company information.

**Response**: QuickBooks CompanyInfo object

#### GET `/api/v4/quickbooks/chart-of-accounts`
Get Chart of Accounts from QuickBooks.

**Response**: QuickBooks QueryResponse with Account array

---

## Additional Resources

- [QuickBooks API Documentation](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0)
- [QuickBooks Query Language (QBQL) Guide](https://developer.intuit.com/app/developer/qbo/docs/develop/explore-the-quickbooks-online-api/data-queries)
- [Intuit Developer Community](https://intuitdeveloper.github.io/)
- [QuickBooks API Reference](https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/account)

---

## Support

If you encounter issues:
1. Check the Troubleshooting section above
2. Review QuickBooks API documentation
3. Check application logs for detailed error messages
4. Verify your Intuit Developer Portal app configuration

---

**Last Updated**: January 2025
**Maintained By**: Your Development Team
