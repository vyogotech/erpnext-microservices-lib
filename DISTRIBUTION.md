# Distribution Guide

This guide details how to package and distribute the `frappe-microservice` library.

## Prerequisites

Ensure you have the necessary build tools installed:

```bash
pip install --upgrade build twine
```

## 1. Preparing the Package

Before building, ensure your `setup.py` version is correct for the new release.

```python
# setup.py
setup(
    ...
    version="1.0.0",  # Update this for new releases
    ...
)
```

## 2. Building the Artifacts

Run the build command to create your source distribution (`.tar.gz`) and wheel (`.whl`):

```bash
python -m build
```

This will create a `dist/` directory containing the distribution files.

## 3. Validating the Build

Check that the package description will render correctly on PyPI:

```bash
twine check dist/*
```

## 4. Publishing

### Option A: Publish to PyPI (Public)

1.  **TestPyPI (Recommended):** First, upload to the testing index to verify everything looks right.

    ```bash
    twine upload --repository testpypi dist/*
    ```

    You can install from TestPyPI to verify:
    ```bash
    pip install --index-url https://test.pypi.org/simple/ --no-deps frappe-microservice
    ```

2.  **Production PyPI:** Once verified, upload to the main index.

    ```bash
    twine upload dist/*
    ```

### Option B: Private Distribution (Git)

You can use the library directly from a Git repository without publishing to PyPI.

**pip install via Git:**

```bash
pip install git+https://github.com/your-org/frappe-microservice-lib.git
```

**requirements.txt:**

```text
frappe-microservice @ git+https://github.com/your-org/frappe-microservice-lib.git@v1.0.0
```

### Option C: Private PyPI

If you use a private package index (like AWS CodeArtifact, Google Artifact Registry, or wrapper-pypi), you can configure twine to upload there.

## 5. Local Development

For developing locally while using the library in another project:

```bash
cd /path/to/frappe-microservice-lib
pip install -e .
```

## 6. Adding to a Container (Without Git)

If your code is not in a remote Git repository (e.g., local development or private source), you can bundle the library directly into your Docker image.

### Method 1: Copy and Install Source (Simpler)

Copy the entire library folder into your container and install it.

**Dockerfile**
```dockerfile
# ... previous steps ...

# Copy the library source
COPY ./frappe-microservice-lib /tmp/frappe-microservice-lib

# Install from source
RUN pip install /tmp/frappe-microservice-lib

# (Optional) Cleanup
RUN rm -rf /tmp/frappe-microservice-lib

# ... rest of Dockerfile ...
```

### Method 2: Build Wheel and Install (Cleaner)

Build a `.whl` file locally first, then copy only that single file into the container. This keeps your image smaller and build context cleaner.

1.  **Build the wheel locally:**
    ```bash
    python -m build
    # This creates dist/frappe_microservice-1.0.0-py3-none-any.whl
    ```

2.  **Copy and install in Docker:**

    **Dockerfile**
    ```dockerfile
    # ... previous steps ...
    
    # Copy only the wheel file
    COPY ./dist/frappe_microservice-1.0.0-py3-none-any.whl /tmp/
    
    # Install the wheel
    RUN pip install /tmp/frappe_microservice-1.0.0-py3-none-any.whl
    
    # ... rest of Dockerfile ...
    ```
