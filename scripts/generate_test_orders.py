import requests
import time
import random
import os
from dotenv import load_dotenv
from faker import Faker

load_dotenv()
fake = Faker()

SHOP = os.getenv("SHOPIFY_STORE")
TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_VERSION = "2024-10"

url = f"https://{SHOP}/admin/api/{API_VERSION}/orders.json"

headers = {
    "X-Shopify-Access-Token": TOKEN,
    "Content-Type": "application/json"
}

# Realistic product catalog: (name, price)
PRODUCTS = [
    ("Wireless Bluetooth Earbuds", 39.99),
    ("Stainless Steel Water Bottle", 24.50),
    ("Organic Cotton T-Shirt", 19.99),
    ("Leather Laptop Sleeve", 45.00),
    ("Ceramic Coffee Mug Set", 28.75),
    ("Yoga Mat Non-Slip", 32.00),
    ("Portable Phone Charger 10000mAh", 21.99),
    ("Scented Soy Candle", 16.50),
    ("Minimalist Desk Lamp", 54.99),
    ("Canvas Tote Bag", 18.00),
    ("Noise Cancelling Headphones", 89.99),
    ("Stainless Steel Kitchen Knife Set", 65.00),
    ("Running Shoes - Men's", 74.99),
    ("Running Shoes - Women's", 74.99),
    ("Bamboo Cutting Board", 22.30),
]

def create_test_order(i):
    num_items = random.randint(1, 3)
    line_items = []
    for _ in range(num_items):
        title, price = random.choice(PRODUCTS)
        line_items.append({
            "title": title,
            "price": str(price),
            "quantity": random.randint(1, 2)
        })

    first_name = fake.first_name()
    last_name = fake.last_name()
    email = f"{first_name.lower()}.{last_name.lower()}{random.randint(1,999)}@{fake.free_email_domain()}"

    payload = {
        "order": {
            "line_items": line_items,
            "customer": {
                "first_name": first_name,
                "last_name": last_name,
                "email": email
            },
            "billing_address": {
                "first_name": first_name,
                "last_name": last_name,
                "address1": fake.street_address(),
                "city": fake.city(),
                "province": fake.state(),
                "country": "United States",
                "zip": fake.postcode()
            },
            "financial_status": "paid"
        }
    }
    resp = requests.post(url, json=payload, headers=headers)
    if resp.status_code == 201:
        order_id = resp.json()["order"]["id"]
        print(f"[{i}] Created order {order_id} for {first_name} {last_name}")
    else:
        print(f"[{i}] Failed: {resp.status_code} {resp.text}")

if __name__ == "__main__":
    TOTAL_MINUTES = 5
    ORDERS_PER_MINUTE = 4
    interval = 60 / ORDERS_PER_MINUTE

    count = 0
    end_time = time.time() + TOTAL_MINUTES * 60

    while time.time() < end_time:
        count += 1
        create_test_order(count)
        time.sleep(interval)

    print(f"Done. Created {count} test orders.")