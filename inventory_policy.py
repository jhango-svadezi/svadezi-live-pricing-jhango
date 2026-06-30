#!/usr/bin/env python3
"""One-off: set every ACTIVE product's variants to
   - inventory tracked = true (track quantity)
   - inventory policy = CONTINUE (continue selling when out of stock)
"""
import os, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

STORE = os.environ["SHOPIFY_STORE"]; TOKEN = os.environ["SHOPIFY_ACCESS_TOKEN"]
URL = f"https://{STORE}/admin/api/2024-10/graphql.json"
H = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

def gql(q, v=None):
    for a in range(1, 12):
        try:
            r = requests.post(URL, json={"query": q, "variables": v or {}}, timeout=90)
        except requests.RequestException:
            time.sleep(min(3 * a, 30)); continue
        if r.status_code in (429, 401, 403, 500, 502, 503, 520):
            time.sleep(min(3 * a, 30)); continue
        r.raise_for_status(); d = r.json()
        if d.get("errors"):
            if any("throttl" in str(e).lower() for e in d["errors"]): time.sleep(2**a); continue
            raise RuntimeError(d["errors"])
        return d["data"]
    raise RuntimeError("retries exhausted")

Q = """query($c:String){ productVariants(first:100, after:$c){ pageInfo{hasNextPage endCursor}
  nodes{ id product{ id status } } } }"""
MUT = """mutation($pid:ID!,$v:[ProductVariantsBulkInput!]!){
  productVariantsBulkUpdate(productId:$pid, variants:$v){ userErrors{field message} } }"""

print("Fetching variants...")
by_prod = {}; cur=None; n=0
while True:
    d = gql(Q, {"c": cur})["productVariants"]
    for node in d["nodes"]:
        if node["product"]["status"] != "ACTIVE": continue
        by_prod.setdefault(node["product"]["id"], []).append(node["id"])
        n += 1
    if d["pageInfo"]["hasNextPage"]: cur = d["pageInfo"]["endCursor"]
    else: break
    time.sleep(0.4)  # pace fetch
print(f"{n} active variants across {len(by_prod)} products")

ok = err = done = 0; lock = threading.Lock()
def work(pid, ids):
    global ok, err
    try:
        for i in range(0, len(ids), 250):
            variants = [{"id": vid, "inventoryPolicy": "CONTINUE", "inventoryItem": {"tracked": True}} for vid in ids[i:i+250]]
            res = gql(MUT, {"pid": pid, "v": variants})["productVariantsBulkUpdate"]
            if res["userErrors"]:
                with lock: err += 1
                print("  ERR", pid, res["userErrors"][:1]); return
        with lock: ok += 1
    except Exception as e:
        with lock: err += 1; print("  FAIL", pid, e)

with ThreadPoolExecutor(max_workers=3) as ex:
    futs = [ex.submit(work, p, v) for p, v in by_prod.items()]
    for _ in as_completed(futs):
        done += 1
        if done % 25 == 0 or done == len(futs): print(f"  {done}/{len(futs)} ok={ok} err={err}")
print(f"DONE: {ok} products updated, {err} errors")
