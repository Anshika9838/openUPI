import requests

requests.post(
    "http://127.0.0.1:5000/api/payments/receive",
    headers={
        "Content-Type": "application/json",
        "X-API-Key": "DUMMY_API_KEY",
    },
    json={
        "transaction_description": "UPI payment received a/c XX5549 INR 250.00 on 23-04-26 16:33:53 Ref No UTR1234567890"
    },
)
