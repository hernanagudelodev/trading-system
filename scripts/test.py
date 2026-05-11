import requests

API_KEY = "LlDb7F7KZDgqhl8KtdNMx1SSv8yx4pMf"

# Endpoint que SÍ funciona en plan gratis
url = f"https://financialmodelingprep.com/api/v3/profile/AAPL?apikey={API_KEY}"
r = requests.get(url)
print(r.status_code)
print(r.json())