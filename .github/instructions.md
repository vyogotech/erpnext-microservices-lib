**Tasks**
You're an expert full stack developer with depe knowledge in Python, MS architecture and ERPNext. You are creating a microservices framework and services for a Single site multi tenant Saas application.
Now,

**Issue**

1. For the signup service we are getting 500 during user record creation and needs to be fixed by you urgently.

**Analysis**

Failure may be because of user password not set or doctypes not available to the microservice. I am unable to debug because both the MS service framework and signup service doesn’t have enough logging and error handling. Signup service doesn’t have end to end transaction management when creating tenant, company and user.

**Tasks**

1. We need to move forward by:

1. Add enough logging in MS lib and signup services
1. Add error handling.
1. Start Fixing the core issue:
   a. Possible root causes - user password is not set. If the user password is not set, we cannot run e2e_test_simple.py
   b. If the Frappe ORM is not working, then let’s switch to raw sql for now to move forward. Using raw sql may improve performance but we need to wary of sql injection attacks and perm issues.
   c. Once the signup is working, we need to test the order service also.

**Testing Instructions:**

1. Build the image for Microservices Lib framework
2. Build the image for respective service.
3. Don’t touch the central site container.
4. Use ‘podman compose ‘ to start the compose and not docker.
5. Use the Podman-compose.yml file.
6. Use e2e_test_simply.py to test end to end.

**important**

1. Don’t make major code changes or random ones to just make things work. Modified code should be production ready always.
2. Stick the core principles of micro services architecture and central site.

**Reference commands**
cd /Users/varkrish/personal/frappe_ms_poc/frappe-microservice-lib && podman build --no-cache -t frappe-microservice-base:latest .
cd /Users/varkrish/personal/frappe_ms_poc/signup-service && podman build -f Containerfile -t signup-service:latest .
cd /Users/varkrish/personal/frappe_ms_poc/orders-service && podman build -t localhost/order-service:latest .
cd /Users/varkrish/personal/frappe_ms_poc/
podman compose -f podman-compose.yml down && podman compose -f podman-compose.yml up
