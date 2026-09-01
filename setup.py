"""
Setup script for Frappe Microservice Framework
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="frappe-microservice",
    version="1.5.0",
    author="Vyogo Technologies",
    author_email="dev@vyogolabs.tech",
    description="A Python framework for building secure, isolated Frappe microservices",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/vyogotech/erpnext-microservices-lib",
    license="MIT",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
    ],
    # PEP 604 union syntax (str | None) is used without
    # `from __future__ import annotations`, so 3.10+ is required.
    python_requires=">=3.10",
    install_requires=[
        "flask>=2.0.0",
        "gunicorn>=22.0",
        "requests>=2.25.0",
        # NOTE: the OpenTelemetry packages below are ALSO exposed as the
        # optional "[otel]" extra. They remain in install_requires for 1.5.0 to
        # avoid breaking transitive installs; they will move out of the base
        # install (extra-only) in 2.0.
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-instrumentation-flask",
        "opentelemetry-exporter-otlp",
        "flasgger>=0.9.5",
        "pyjwt>=2.4.0",
        "redis>=4.0.0",
        "pymysql>=1.0.0",
        "rq>=1.16.0",
    ],
    extras_require={
        "dev": [
            "pytest>=6.0",
            "pytest-cov>=2.0",
            "black>=21.0",
            "flake8>=3.9",
            "behave",
            "allure-pytest",
            "allure-behave",
            "responses",
        ],
        # Optional OpenTelemetry tracing (also in base install for 1.5.0).
        "otel": [
            "opentelemetry-api",
            "opentelemetry-sdk",
            "opentelemetry-instrumentation-flask",
            "opentelemetry-exporter-otlp",
        ],
    },
    entry_points={
        "console_scripts": [
            "frappe-ms=frappe_microservice.cli:main",
        ],
    },
)
