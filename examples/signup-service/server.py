"""
Minimal example microservice built with frappe-microservice.

Run in a container off the base image (see Containerfile) which sets
SERVICE_PATH / SERVICE_APP and starts this app via the library entrypoint,
or locally with `python server.py` for the Flask dev server.
"""

from frappe_microservice import create_microservice

# `secure_route` automatically authenticates the caller (Central Site) and
# resolves the tenant via get_user_tenant_id(user) → g.tenant_id. You do NOT
# need to call app.set_tenant_id() manually in the common case.
app = create_microservice("signup-service")


@app.secure_route("/hello", methods=["GET"])
def hello(user):
    """Authenticated user is injected as the first argument."""
    return {"message": f"Hello {user}!"}


@app.secure_route("/whoami", methods=["GET"])
def whoami(user):
    # tenant_id is already resolved by secure_route and available on tenant_db.
    return {"user": user, "tenant_id": app.tenant_db.get_tenant_id()}


# Auto-CRUD for a DocType (all routes are tenant-scoped + authenticated):
#   GET/POST/PUT/DELETE /api/resource/signup-request
app.register_resource("Signup Request")


if __name__ == "__main__":
    app.run()
