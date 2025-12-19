FROM python:3.11

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    libmariadb-dev \
    pkg-config \
    curl \
    default-jre \
    && rm -rf /var/lib/apt/lists/*

# Install Frappe and ERPNext (version-15) for local development
RUN pip install --no-cache-dir git+https://github.com/frappe/frappe.git@version-15 && \
    pip install --no-cache-dir git+https://github.com/frappe/erpnext.git@version-15

# Install Allure CLI
RUN curl -o allure-2.24.0.tgz -L https://github.com/allure-framework/allure2/releases/download/2.24.0/allure-2.24.0.tgz \
    && tar -zxvf allure-2.24.0.tgz -C /opt/ \
    && ln -s /opt/allure-2.24.0/bin/allure /usr/bin/allure \
    && rm allure-2.24.0.tgz

# Install act for local pipeline testing
RUN curl https://raw.githubusercontent.com/nektos/act/master/install.sh | bash

# Set working directory
WORKDIR /workspaces/frappe-microservice-lib

# Install project in editable mode
COPY . .
RUN pip install -e ".[dev]"

CMD ["sleep", "infinity"]
