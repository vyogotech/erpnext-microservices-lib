import requests
import concurrent.futures
import uuid
import time

SIGNUP_URL = "http://localhost:8100/signup/tenant"

def make_signup_request(i):
    subdomain = f"loadtest-{uuid.uuid4().hex[:8]}"
    payload = {
        "tenant_name": f"Load Test Company {i} {uuid.uuid4().hex[:4]}",
        "subdomain": subdomain,
        "admin_email": f"admin@{subdomain}.com",
        "admin_password": "SecurePassword123!"
    }
    try:
        start_time = time.time()
        response = requests.post(SIGNUP_URL, json=payload, timeout=30)
        duration = time.time() - start_time
        return i, response.status_code, duration
    except Exception as e:
        return i, str(e), 0

def run_load_test(concurrency=10, total_requests=50):
    print(f"Starting load test: {total_requests} requests with concurrency {concurrency}...")
    
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_req = {executor.submit(make_signup_request, i): i for i in range(total_requests)}
        for future in concurrent.futures.as_completed(future_to_req):
            results.append(future.result())
            if len(results) % 5 == 0:
                print(f"Completed {len(results)}/{total_requests} requests...")

    # Summary
    success_count = sum(1 for _, status, _ in results if status == 201)
    fail_count = total_requests - success_count
    
    print("\n--- Load Test Summary ---")
    print(f"Total Requests: {total_requests}")
    print(f"Successful: {success_count}")
    print(f"Failed: {fail_count}")
    
    if results:
        avg_time = sum(dur for _, _, dur in results) / len(results)
        print(f"Average latency: {avg_time:.2f}s")

    for i, status, dur in results:
        if status != 201:
            print(f"Request {i} failed with status {status}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Load test signup service')
    parser.add_argument('--requests', type=int, default=30, help='Total requests')
    parser.add_argument('--concurrency', type=int, default=15, help='Concurrency Level')
    args = parser.parse_args()
    
    run_load_test(concurrency=args.concurrency, total_requests=args.requests)
